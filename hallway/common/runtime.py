"""Construct the documented Band adapter with room-bound, least-privilege tools."""
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from band import Agent
from band.adapters.langgraph import LangGraphAdapter
from band.core.types import Emit
from band.runtime.tools.agent import AgentTools
from band.client.rest import DEFAULT_REQUEST_OPTIONS
from band_rest.agent_api_chats import RenameAgentChatRequestChat
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from hallway.common.band_cfg import configure_timeouts, credentials, identities
from hallway.common.brief import Brief, digest
from hallway.common.llm import llm, InferenceUnavailable
from hallway.common.room import (action_event, approved_payload, case_state, post,
                                recruit, review, room_records, raw_messages, submit_brief, request_owner)


def make_tools(role: str, holder: dict, ids: dict[str,str]) -> list:
    locks = {}

    def bound(config: RunnableConfig) -> AgentTools:
        room_id = config.get('configurable', {}).get('thread_id')
        if not isinstance(room_id, str) or not room_id:
            raise ValueError('Missing Band room execution context')
        agent = holder['agent']
        return AgentTools(room_id, agent.runtime.link.rest, agent_id=ids[role])

    def lock(tools):
        # Process-local serialization prevents duplicate tool calls racing in a room.
        # All durable facts and handoffs remain exclusively in Band.
        return locks.setdefault(tools.room_id, asyncio.Lock())

    @tool
    async def band_read_case(config: RunnableConfig) -> dict:
        """Read authenticated Band protocol records for the CURRENT room. No room argument."""
        tools = bound(config)
        records = await room_records(tools, ids)
        if role in ('grapher','closer'):
            return approved_payload(records,tools.room_id)
        if role in ('scribe','researcher') and os.getenv('ENABLE_DRUG_RESEARCH')=='1':
            from hallway.common.research_room import decode_research,read_research
            research_records=decode_research(await raw_messages(tools),ids)
            if role=='researcher' or any(r['kind']=='RESEARCH_REQUEST' for r in research_records):
                return {'room_kind':'research','request':await read_research(tools,ids)}
        if role=='researcher':
            raise ValueError('Drug research is disabled; no case transcript access tool')
        if role == 'desk':
            return {'records':records}
        state=case_state(records)
        await tools.get_participants()
        humans={p['id'] for p in tools.participants if str(p.get('type','')).casefold()=='user'}
        state['owner_requests']=[r for r in records if r['kind']=='OWNER_REQUEST']
        state['human_replies']=[m for m in await raw_messages(tools) if str(m.get('sender_type','')).casefold()=='user' and m.get('sender_id') in humans]
        return state

    result = [band_read_case]
    if role == 'desk':
        @tool
        async def band_ingest_fixture(fixture: str, config: RunnableConfig) -> dict:
            """Create a Band case from an explicitly requested fixture ID, e.g. handoff_2. The authenticated human request is loaded from Band, not supplied by the model."""
            tools = bound(config)
            if not re.fullmatch(r'[a-zA-Z0-9_-]+(?:\.txt)?',fixture):
                raise ValueError('Unknown fixture ID')
            async with lock(tools):
                await tools.get_participants()
                humans = [p for p in tools.participants if str(p.get('type','')).casefold() == 'user']
                human_ids = {p['id'] for p in humans}
                desk=next((p for p in tools.participants if p['id']==ids['desk']),{})
                desk_handle=desk.get('handle','')
                requests = []
                for message in await raw_messages(tools):
                    if str(message.get('sender_type','')).casefold() != 'user' or message.get('sender_id') not in human_ids:
                        continue
                    content=message.get('content','').strip()
                    if desk_handle and content.startswith(desk_handle+' '):
                        content=content[len(desk_handle):].strip()
                    match = re.fullmatch(r'/ingest fixture:([a-zA-Z0-9_-]+(?:\.txt)?)(?: run:([a-zA-Z0-9_-]{1,100}))?', content)
                    if match:
                        requests.append((message, match))
                if not requests:
                    raise ValueError('No authenticated human /ingest fixture request in this lobby')
                initiating, match = requests[-1]
                if match.group(1) != fixture:
                    raise ValueError('Fixture differs from latest authenticated human request')
                request_id = match.group(2) or initiating['id']
                existing = await room_records(tools, ids)
                matches = [r for r in existing if r['kind']=='CASE_CREATED' and r.get('initiating_message_id')==initiating['id']]
                if matches:
                    return matches[-1]
                human = next(p for p in humans if p['id']==initiating['sender_id'])
                from hallway.ingest.fixture import load_recording
                recording = load_recording(fixture)
                recording['human_id'] = human['id']
                recording['human_name'] = initiating.get('sender_name') or human.get('name')
                await action_event(tools, 'Creating a synthetic-fixture case in Band')
                room_id = await tools.create_chatroom()
                await tools.rest.agent_api_chats.rename_agent_chat(
                    room_id, chat=RenameAgentChatRequestChat(title=f'Safe Scribe case {room_id[:8]}'),
                    request_options=DEFAULT_REQUEST_OPTIONS)
                case = AgentTools(room_id, tools.rest, agent_id=ids['desk'])
                await case.add_participant(human['id'])
                for participant in ('scribe','critic'):
                    await recruit(case, participant, ids)
                await post(case,'TRANSCRIPT',{'recording':recording},['scribe','critic'],ids)
                payload = {'fixture':fixture, 'request_id':request_id, 'initiating_message_id':initiating['id'], 'room_id':room_id}
                await post(tools,'CASE_CREATED',payload,[],ids)
                return payload
        result.append(band_ingest_fixture)
    if role == 'scribe':
        @tool
        async def band_publish_brief(brief: Brief, config: RunnableConfig) -> dict:
            """Publish clinical BRIEF in current case room and request Critic review; no downstream recruitment. Revision is runtime controlled."""
            tools = bound(config)
            async with lock(tools):
                return await submit_brief(tools, ids, brief)
        result.append(band_publish_brief)
    if role == 'critic':
        @tool
        async def band_review_brief(approve: bool, reasons: list[str], config: RunnableConfig) -> dict:
            """Judge CURRENT Scribe brief. Runtime adds non-overridable quote/owner/source checks; passing approval can create an opted-in approved room and deliver redacted evidence to Grapher."""
            tools = bound(config)
            async with lock(tools):
                return await review(tools, ids, approve, reasons)
        result.append(band_review_brief)
    if role == 'scribe':
        @tool
        async def band_request_owner(follow_up_id: str, config: RunnableConfig) -> dict:
            """Ask the actual human charge nurse to assign one current follow-up; returns authentic Band request message ID."""
            tools=bound(config)
            async with lock(tools):
                return await request_owner(tools,ids,follow_up_id)
        result.append(band_request_owner)
    if role in ('scribe','researcher') and os.getenv('ENABLE_DRUG_RESEARCH')=='1':
        if role=='scribe':
            @tool
            async def band_relay_research(config: RunnableConfig) -> dict:
                """Relay a verified drug-only research result to its authenticated linked case; no model routing parameters."""
                from hallway.common.research_room import relay_research
                tools=bound(config)
                async with lock(tools):
                    return await relay_research(tools,ids)
            result.append(band_relay_research)
        else:
            @tool
            async def band_research_drugs(config: RunnableConfig) -> dict:
                """Research only drug names from this room's authenticated Scribe request; report live sources or explicit failure."""
                from hallway.common.research_room import research_drugs
                tools=bound(config)
                async with lock(tools):
                    return await research_drugs(tools,ids)
            result.append(band_research_drugs)
    if role=='grapher':
        @tool
        async def band_write_approved_graph(config: RunnableConfig) -> dict:
            """Write only the authenticated current-room Critic approval to graph; return backend and actual lineage query."""
            tools=bound(config)
            async with lock(tools):
                records=await room_records(tools,ids)
                payload=approved_payload(records,tools.room_id)
                allowed={ids['critic'],ids['grapher'],payload.get('human_id')}
                if any(p['id'] not in allowed for p in tools.participants):
                    raise ValueError('Unexpected participant in restricted graph approved room')
                mocked=os.getenv('MOCK_NEO4J')=='1'
                expected_status='MOCK_GRAPH_WRITTEN' if mocked else 'GRAPH_WRITTEN'
                receipts=[r for r in records if r['kind']=='GRAPH_WRITTEN' and r.get('status')==expected_status and r.get('digest')==payload['digest'] and r.get('revision')==payload['revision']]
                if receipts:
                    return receipts[-1]
                if not mocked and not os.getenv('NEO4J_URI'):
                    raise ValueError('Neo4j URI absent; implicit memory fallback forbidden for live Grapher')
                from hallway.graph import store
                graph_brief=json.loads(json.dumps(payload['brief']))
                # Existing Erik-owned Neo4j writer consumes name; preserve text
                # and quotes while mapping the validated medication item shape.
                for med in graph_brief['meds']:
                    med['name']=med['text']
                manifest=list(payload['manifest'])
                from datetime import datetime,timezone
                manifest.append({'agent':'grapher','field':'brief','purpose':'graph_write',
                                 'ts':datetime.now(timezone.utc).isoformat(),
                                 'source_message_id':payload['message_id'],'room_id':tools.room_id})
                def write_and_query():
                    result=store.write_approved(graph_brief,manifest,payload['brief']['patient']['pseudo_id'],payload['case_id'])
                    return result,store.who_saw_identifiers()
                for attempt in range(3):
                    try:
                        result,lineage=await asyncio.wait_for(asyncio.to_thread(write_and_query),timeout=10)
                        break
                    except asyncio.TimeoutError:
                        logging.error('Neo4j graph write/query timed out; no success receipt')
                        raise ValueError('Graph result unavailable; check backend before retrying') from None
                    except Exception as exc:
                        logging.error('Neo4j graph write/query failed attempt=%s type=%s',attempt+1,type(exc).__name__)
                        if attempt==2:
                            raise ValueError('Graph write failed; no success receipt') from None
                        await asyncio.sleep(0.2*(attempt+1))
                receipt={'status':'MOCK_GRAPH_WRITTEN' if mocked else 'GRAPH_WRITTEN',
                         'revision':payload['revision'],'digest':payload['digest'],'case_id':payload['case_id'],
                         'approved_room_id':tools.room_id,'merged':bool(result.get('merged')),
                         'encounter':result.get('encounter'),'who_saw_identifiers':lineage,
                         'provenance_kind':payload['provenance_kind'],'lineage_query_scope':'global_graph_all_encounters',
                         'lineage_verification':payload.get('lineage_verification','unverified_processing_only')}
                await post(tools,'GRAPH_WRITTEN',receipt,['critic'],ids)
                return receipt
        result.append(band_write_approved_graph)
    return result


def build_agent(role: str, instructions: str):
    configure_timeouts()
    ids = identities()
    holder = {}
    tools = make_tools(role, holder, ids)
    adapter = LangGraphAdapter(llm=llm(role), additional_tools=tools,
        include_tools=[], capabilities=set(), emit={Emit.TOOL_CALLS, Emit.USAGE},
        recursion_limit=16, custom_section=instructions)
    original_on_message=adapter.on_message
    async def on_message_with_pause(msg, tools, history, participants_msg, contacts_msg, *, is_session_bootstrap, room_id):
        try:
            return await original_on_message(msg,tools,history,participants_msg,contacts_msg,
                is_session_bootstrap=is_session_bootstrap,room_id=room_id)
        except InferenceUnavailable:
            await tools.send_event(content='inference unavailable, case paused',message_type='error')
            raise
    adapter.on_message=on_message_with_pause
    agent = Agent.create(adapter=adapter, agent_id=ids[role], api_key=credentials(role)[1])
    holder['agent'] = agent
    return agent


def run(role: str, instructions: str):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    asyncio.run(build_agent(role, instructions).run())
