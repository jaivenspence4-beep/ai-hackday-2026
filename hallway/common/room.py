"""Band is the durable protocol: no shared local state or out-of-band handoffs."""
import asyncio
import json
import logging
import re
import os
from datetime import datetime, timezone
from band.runtime.tools.agent import AgentTools
from hallway.common.brief import Brief, digest, normalize, validate_brief, identifier_fields

PREFIX = 'SAFESCRIBE/1\n'
LEGACY_PREFIX = 'HANDOFF/1\n'
AUTHORS = {'TRANSCRIPT': 'desk', 'BRIEF': 'scribe', 'ENRICHMENT': 'researcher',
           'VERDICT': 'critic', 'APPROVAL': 'critic', 'HANDOFF': 'scribe',
           'CASE_CREATED': 'desk', 'OWNER_REQUEST': 'scribe', 'OUTPUT': None,
           'BOUNDARY_CREATED':'critic','BOUNDARY_SENT':'critic','GRAPH_WRITTEN':'grapher'}


def strip_leading_mentions(content: str, tokens: set[str]) -> str:
    """Remove only exact leading Band mention tokens verified outside the message."""
    if not isinstance(content,str):
        return content
    remainder=content.lstrip()
    while remainder:
        pieces=remainder.split(None,1)
        if len(pieces)!=2 or pieces[0] not in tokens:
            break
        remainder=pieces[1]
    return remainder


def decode_messages(messages: list[dict], ids: dict[str, str]) -> list[dict]:
    records = []
    for message in messages:
        content = message.get('content', '')
        if not isinstance(content, str):
            continue
        markers=list(re.finditer(r'(?m)^(?:SAFESCRIBE/1|HANDOFF/1)\r?$',content))
        # A readable heading is presentation only. Exactly one envelope and its
        # authenticated sender are authoritative, including for legacy messages.
        if len(markers)!=1:
            continue
        try:
            value = json.loads(content[markers[0].end():])
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        author = AUTHORS.get(value.get('kind'))
        if not author or not ids.get(author) or message.get('sender_id') != ids.get(author):
            continue
        value = dict(value, message_id=message.get('id'))
        records.append(value)
    return records


async def raw_messages(tools: AgentTools) -> list[dict]:
    await tools.get_participants()
    tokens=set()
    for participant in tools.participants:
        if participant.get('id'):
            tokens.add(f"@[[{participant['id']}]]")
        handle=participant.get('handle')
        if handle:
            tokens.add(handle)
            tokens.add(handle if handle.startswith('@') else '@'+handle)
    messages = []
    page = 1
    while True:
        data = await tools.fetch_room_context(room_id=tools.room_id, page=page, page_size=100)
        messages.extend(dict(message,content=strip_leading_mentions(message.get('content',''),tokens)) for message in data['data'])
        if page >= data['meta'].get('total_pages', 1):
            break
        page += 1
        if page > 20:
            raise ValueError('Case context exceeded safe size; escalate to human')
    return messages


async def room_records(tools: AgentTools, ids: dict[str, str]) -> list[dict]:
    return decode_messages(await raw_messages(tools), ids)


def case_state(records: list[dict]) -> dict:
    sources = [r for r in records if r['kind'] == 'TRANSCRIPT']
    if not sources:
        raise ValueError('No authenticated Desk transcript in this room')
    source = sources[0]
    if any(r['recording'] != source['recording'] for r in sources):
        raise ValueError('Conflicting source transcripts; human review required')
    briefs = [r for r in records if r['kind'] == 'BRIEF']
    for revision, brief in enumerate(briefs, 1):
        if brief.get('revision') != revision:
            raise ValueError('Invalid or duplicate revision sequence; human review required')
    verdicts = [r for r in records if r['kind'] == 'VERDICT']
    enrichments = [r for r in records if r['kind'] == 'ENRICHMENT']
    return {'source_message_id':source['message_id'], 'recording':source['recording'], 'brief': briefs[-1] if briefs else None,
            'verdicts':verdicts, 'enrichment': enrichments[-1] if enrichments else None,
            'approved': any(r['kind'] == 'APPROVAL' for r in records)}


def readable_summary(kind: str, payload: dict) -> str:
    """Small judge-facing facts, without copying patient details into headings."""
    revision=payload.get('revision')
    revision=revision if isinstance(revision,int) and not isinstance(revision,bool) else '?'
    facts={
        'TRANSCRIPT': 'Synthetic handoff ready for Scribe and Critic.',
        'BRIEF': f'Revision {revision} ready for Critic review.',
        'OWNER_REQUEST': 'A follow-up needs a human owner; reply to both Scribe and Critic.',
        'APPROVAL': f'Revision {revision} approved in the case room only.',
        'CASE_CREATED': 'New case room created.',
        'ENRICHMENT': 'Research result recorded.',
        'HANDOFF': 'Handoff details recorded.',
        'OUTPUT': 'Worker status recorded.',
        'BOUNDARY_CREATED':'Restricted approved room created.',
        'BOUNDARY_SENT':'Redacted approval sent to Grapher in the approved room.',
        'GRAPH_WRITTEN':'Graph write result recorded; inspect backend status.',
    }
    if kind=='VERDICT':
        verdict=payload.get('verdict')
        facts[kind]=f'{verdict} for revision {revision}.' if verdict in ('VETO','APPROVE') else f'Review recorded for revision {revision}.'
    if kind not in facts:
        raise ValueError('Unknown protocol post kind')
    return f'**{kind}** — {facts[kind]}'


async def post(tools: AgentTools, kind: str, payload: dict, roles: list[str], ids: dict[str, str]):
    content = readable_summary(kind,payload) + '\n\n' + PREFIX + json.dumps({'kind':kind, **payload}, ensure_ascii=False)
    if not roles:
        # SDK messages require recipients; status is a durable Band task event.
        return await tools.send_event(content=content, message_type='task')
    await tools.get_participants()
    participants = {p['id']:p for p in tools.participants}
    mentions = []
    for role in roles:
        participant = participants.get(ids[role])
        if not participant or not participant.get('handle'):
            raise ValueError(f'Missing actual Band participant handle for {role}')
        mentions.append({'id':ids[role], 'handle':participant['handle']})
    return await tools.send_message(content, mentions=mentions)


async def action_event(tools: AgentTools, summary: str):
    await tools.send_event(content=summary, message_type='thought')


async def recruit(tools: AgentTools, role: str, ids: dict[str,str]):
    await tools.send_event(content=json.dumps({'name':'band_add_participant','role':role}), message_type='tool_call')
    result = await tools.add_participant(ids[role])
    await tools.send_event(content=json.dumps({'name':'band_add_participant','role':role,'status':result.get('status')}), message_type='tool_result')
    return result


def transcript_owner_is_explicit(owner: str, quote: str) -> bool:
    """Conservative assignment syntax; mentioning an action's recipient is not ownership."""
    import re
    owner=normalize(owner or '')
    quote=normalize(quote).replace('’', "'")
    if not owner or owner in {'someone','somebody','we','they','unknown','unassigned','it','i'}:
        return False
    if any(phrase in quote for phrase in ('someone should','somebody should','we should probably')):
        return False
    if owner=='receiving nurse':
        return bool(re.search(r"\bthat(?:'s| is) yours\b",quote))
    named=re.escape(owner)
    # Recognized fixture forms: "that's Maria", "that one is on the night
    # nurse", "that order is in under Doctor Patel", or an explicit subject
    # such as "Doctor Patel is following it" / "Maria will check the result".
    assignment=rf"\bthat(?:(?: one| call| task| follow-up))?(?:'s| is) (?:on (?:the )?)?{named}(?=\b|$)"
    order=rf"\bthat order is in under {named}(?=\b|$)"
    subject=rf"(?:^|[.!?;,]\s*|\band\s+)(?:the )?{named}\s+(?:will\b|should\b|must\b|owns\b|is following\b)"
    return any(re.search(pattern,quote) for pattern in (assignment,order,subject))


def ownership_errors(brief: Brief, messages: list[dict], ids: dict[str,str], human_ids: set[str], reply_handles: tuple[str,...] = ()) -> list[str]:
    """Only server-authenticated human replies to an actual Scribe request count."""
    records=decode_messages(messages,ids)
    requests={r['message_id']:r for r in records if r['kind']=='OWNER_REQUEST'}
    positions={m['id']:i for i,m in enumerate(messages)}
    by_id={m['id']:m for m in messages}
    reasons=[]
    for item in brief.follow_ups:
        transcript_owned=transcript_owner_is_explicit(item.owner,item.quote)
        if item.status=='pending' and transcript_owned and not item.owner_message_id:
            continue
        request=requests.get(item.request_message_id)
        if not request or request.get('follow_up_id') != item.id:
            reasons.append(f'follow-up {item.id}: explicit human owner request required')
            continue
        if item.status=='unresolved':
            if item.owner_message_id:
                reasons.append(f'follow-up {item.id}: unresolved item cannot claim an owner reply')
            continue
        message=by_id.get(item.owner_message_id,{})
        if (str(message.get('sender_type','')).casefold()!='user' or message.get('sender_id') not in human_ids
            or positions.get(item.owner_message_id,-1)<=positions.get(item.request_message_id,10**9)):
            reasons.append(f'follow-up {item.id}: owner provenance is not an authenticated human reply')
            continue
        content=message.get('content','').strip()
        # Strip verified recipient tokens only at the outer edges. Interior
        # owner text and unknown handles/UUIDs must remain part of the reply.
        content=strip_leading_mentions(content,set(reply_handles))
        while content:
            tokens=content.rsplit(None,1)
            if len(tokens)!=2 or tokens[-1] not in reply_handles:
                break
            content=tokens[0]
        content=normalize(content).replace('’', "'").rstrip('.!')
        owner=None
        if content in ("i'll own it",'i will own it'):
            preceding=[r for r in records if r['kind']=='OWNER_REQUEST' and positions.get(r['message_id'],10**9)<positions[message['id']]]
            if preceding and preceding[-1]['message_id']==item.request_message_id:
                owner=message.get('sender_name')
        else:
            import re
            match=re.fullmatch(r'/own ([a-z0-9_-]+) (.+)',content)
            if match and match.group(1)==normalize(item.id):
                owner=match.group(2)
        if not owner or not item.owner or normalize(owner)!=normalize(item.owner):
            reasons.append(f'follow-up {item.id}: named owner does not match human reply; use /own ID Full Name or I will own it')
    return reasons


async def request_owner(tools: AgentTools, ids: dict[str,str], follow_up_id: str) -> dict:
    records=await room_records(tools,ids)
    state=case_state(records)
    if state['approved'] or not state['brief']:
        raise ValueError('An unapproved brief is required before asking for an owner')
    if not any(item['id']==follow_up_id for item in state['brief']['brief']['follow_ups']):
        raise ValueError('Unknown current follow-up ID')
    old=[r for r in records if r['kind']=='OWNER_REQUEST' and r['follow_up_id']==follow_up_id]
    if old:
        return old[-1]
    human_id=state['recording'].get('human_id')
    if not human_id:
        raise ValueError('Authenticated charge nurse missing')
    await tools.get_participants()
    handles={p['id']:p.get('handle') for p in tools.participants}
    scribe_handle=handles.get(ids['scribe']);critic_handle=handles.get(ids['critic'])
    if not scribe_handle or not critic_handle:
        raise ValueError('Missing Scribe/Critic handles for verifiable human reply')
    payload={'follow_up_id':follow_up_id,'revision':state['brief']['revision'],
             'prompt':f'Please name an owner with /own {follow_up_id} Full Name, or reply I will own it. Address your reply to both {scribe_handle} and {critic_handle}. If unassigned, this remains unresolved.'}
    await post(tools,'OWNER_REQUEST',payload,['human','critic'],dict(ids,human=human_id))
    updated=await room_records(tools,ids)
    matches=[r for r in updated if r['kind']=='OWNER_REQUEST' and r['follow_up_id']==follow_up_id]
    if not matches:
        raise ValueError('Owner request not yet visible in Band; retry read before revising')
    return matches[-1]



def processing_evidence(state: dict, role: str, purpose: str) -> dict:
    return {'source_message_id':state['source_message_id'],'role':role,'purpose':purpose,
            'fields':identifier_fields(state['recording']['transcript']),
            'kind':'runtime_field_processing_not_read_receipt'}


def processing_manifest(records: list[dict], state: dict, room_id: str) -> tuple[list[dict],str]:
    """Records must already pass sender authentication through decode_messages."""
    manifest=[]
    verified=bool(identifier_fields(state['recording']['transcript']))
    for kind,role,purpose,fallback in [('TRANSCRIPT','desk','intake','transcript'),('BRIEF','scribe','extraction','brief'),('VERDICT','critic','review','brief')]:
        evidence=[r for r in records if r['kind']==kind and (kind=='TRANSCRIPT' or r.get('revision')==state['brief']['revision'])]
        if not evidence:
            raise ValueError('Missing authenticated processing-provenance evidence')
        record=evidence[-1]
        expected=processing_evidence(state,role,purpose)
        valid=(record['message_id']==state['source_message_id']) if role=='desk' else record.get('processing')==expected
        verified=verified and valid
        fields=expected['fields'] if valid else [fallback]
        for field in fields or [fallback]:
            manifest.append({'agent':role,'field':field,'purpose':purpose,
                             'source_message_id':record['message_id'],'transcript_message_id':state['source_message_id'],'room_id':room_id})
    return manifest, 'verified_field_access' if verified else 'unverified_processing_only'


async def submit_brief(tools: AgentTools, ids: dict[str,str], brief: Brief) -> dict:
    state=case_state(await room_records(tools,ids))
    if state['approved']:
        raise ValueError('Case already approved; start a new case to revise it')
    if not any(getattr(brief,key) for key in ('meds','allergies','pending_results','findings','follow_ups')):
        raise ValueError('Empty brief is not a faithful extraction')
    previous=state['brief']
    revision=previous['revision']+1 if previous else 1
    removed=[]
    if previous:
        matching=[v for v in state['verdicts'] if v['revision']==previous['revision']]
        if not matching or matching[-1]['verdict']!='VETO':
            raise ValueError('A revision requires Critic VETO of the current brief')
        if revision>3:
            raise ValueError('Two repair rounds exhausted; human intervention required')
        old=Brief.model_validate(previous['brief'])
        for item in old.follow_ups:
            original_quote=normalize(item.quote)
            backed=original_quote and original_quote in normalize(state['recording']['transcript'])
            if backed:
                from hallway.common.brief import identifier_violations
                candidates=[new for new in brief.follow_ups if normalize(new.quote)==original_quote]
                if not candidates and identifier_violations({'quote':item.quote},state['recording']):
                    candidates=[new for new in brief.follow_ups if normalize(new.quote) and normalize(new.quote) in original_quote]
                if not candidates:
                    raise ValueError('Repair must preserve the original source-backed follow-up quote, not merely its ID')
            elif not any(new.id==item.id for new in brief.follow_ups):
                removed.append(item.id)
    payload={'revision':revision,'brief':brief.model_dump(),'digest':digest(brief.model_dump()),
             'removed_unsupported_follow_ups':removed,'processing':processing_evidence(state,'scribe','extraction')}
    await action_event(tools,f'Publishing HANDOFF brief revision {revision}; case-room-only phase 1')
    await post(tools,'BRIEF',payload,['critic'],ids)
    if os.getenv('ENABLE_DRUG_RESEARCH')=='1':
        from hallway.common.research_room import request_research
        payload['research']=await request_research(tools,ids,brief,state['recording'])
    return payload


async def review(tools: AgentTools, ids: dict[str,str], approve: bool, judgment_reasons: list[str]) -> dict:
    messages=await raw_messages(tools)
    state=case_state(decode_messages(messages,ids))
    current=state['brief']
    if not current:
        raise ValueError('No authenticated Scribe brief')
    if state['approved']:
        if os.getenv('ENABLE_APPROVED_ROOM')=='1':
            approvals=[r for r in decode_messages(messages,ids) if r['kind']=='APPROVAL']
            return await publish_approved_boundary(tools,ids,state,approvals[-1])
        return {'status':'already approved'}
    prior=[v for v in state['verdicts'] if v['revision']==current['revision']]
    if prior and prior[-1]['verdict']=='VETO':
        return {'status':'already reviewed','verdict':prior[-1]}
    brief=Brief.model_validate(current['brief'])
    if digest(brief.model_dump()) != current['digest']:
        raise ValueError('Current brief digest mismatch')
    await tools.get_participants()
    human_ids={p['id'] for p in tools.participants if str(p.get('type','')).casefold()=='user'}
    reasons=validate_brief(brief,state['recording'],[])
    reply_handles=[]
    for participant in tools.participants:
        if participant['id'] not in {ids['scribe'],ids['critic']}:
            continue
        reply_handles.append(f"@[[{participant['id']}]]")
        handle=participant.get('handle')
        if handle:
            reply_handles.extend((handle,handle if handle.startswith('@') else '@'+handle))
    reasons += ownership_errors(brief,messages,ids,human_ids,tuple(reply_handles))
    if not approve:
        reasons.extend(judgment_reasons or ['Critic judgment rejected unsupported interpretation'])
    if reasons:
        verdict={'verdict':'VETO','revision':current['revision'],'reasons':reasons,'escalate':current['revision']>=3,'processing':processing_evidence(state,'critic','review')}
        recipients=['scribe']
        if verdict['escalate']:
            human_id=state['recording'].get('human_id')
            if not human_id:
                raise ValueError('Human escalation recipient missing; room remains blocked')
            ids=dict(ids,human=human_id);recipients.append('human')
        await action_event(tools,'Veto: evidence, ownership or identifier check failed; no outbound delivery')
        await post(tools,'VERDICT',verdict,recipients,ids)
        return verdict
    facts=[]
    if os.getenv('ENABLE_DRUG_RESEARCH')=='1':
        from hallway.common.research_room import research_for_review
        research=research_for_review(messages,ids,brief,state['recording'])
        if research['status']=='pending':
            return {'status':'WAITING_FOR_RESEARCH','revision':current['revision']}
        facts=research['facts']
        # Source/identifier checks cover the exact enrichment entering approval.
        errors=validate_brief(brief,state['recording'],facts)
        if errors:
            raise ValueError('Research evidence failed approval checks; no boundary delivery')
    unresolved=[item.id for item in brief.follow_ups if item.status=='unresolved']
    payload={'revision':current['revision'],'digest':current['digest'],'brief':current['brief'],
             'enrichment':facts,'unresolved_follow_ups':unresolved,'scope':'case_only_phase1'}
    # Recheck the exact envelope, not merely its clinical sub-object. Raw transcript,
    # direct identifier list, human reply text and local salt never enter this object.
    from hallway.common.brief import identifier_violations
    if identifier_violations(payload,state['recording']):
        raise ValueError('Approval envelope contains a direct identifier; blocked')
    if not prior:
        await post(tools,'VERDICT',{'verdict':'APPROVE','revision':current['revision'],
                                  'reasons':[],'unresolved_follow_ups':unresolved,'processing':processing_evidence(state,'critic','review')},['scribe'],ids)
    await action_event(tools,'Approved exact revision in case room only; boundary-room integration remains pending')
    await post(tools,'APPROVAL',payload,['scribe'],ids)
    if os.getenv('ENABLE_APPROVED_ROOM')=='1':
        return await publish_approved_boundary(tools,ids,state,payload)
    return {'status':'APPROVED','revision':current['revision'],'unresolved_follow_ups':unresolved,'scope':'case_only_phase1'}


async def publish_approved_boundary(tools: AgentTools, ids: dict[str,str], state: dict, approval: dict) -> dict:
    """Resume a Critic-owned Band checkpoint; downstream never joins the case."""
    if not ids.get('grapher'):
        raise ValueError('Grapher identity required for approved-room delivery')
    current=state['brief']
    if approval['revision']!=current['revision'] or approval['digest']!=current['digest'] or digest(approval['brief'])!=current['digest']:
        raise ValueError('Approval revision/digest no longer matches current authenticated brief')
    records=await room_records(tools,ids)
    checkpoints=[r for r in records if r['kind']=='BOUNDARY_CREATED']
    if checkpoints:
        checkpoint=checkpoints[-1]
        if checkpoint['revision']!=current['revision'] or checkpoint['digest']!=current['digest']:
            raise ValueError('Conflicting immutable boundary checkpoint')
        room_id=checkpoint['approved_room_id']
    else:
        room_id=await tools.create_chatroom()
        if room_id==tools.room_id:
            raise ValueError('Approved room must differ from case room')
        checkpoint={'case_id':tools.room_id,'approved_room_id':room_id,'revision':current['revision'],'digest':current['digest']}
        # A crash before this checkpoint can orphan an EMPTY room; it cannot
        # leak a transcript or send an unapproved brief to downstream workers.
        await post(tools,'BOUNDARY_CREATED',checkpoint,[],ids)
    if room_id==tools.room_id:
        raise ValueError('Boundary checkpoint points to the case room')
    boundary=AgentTools(room_id,tools.rest,agent_id=ids['critic'])
    await boundary.get_participants()
    allowed={ids['critic'],ids['grapher'],state['recording'].get('human_id')}
    if any(p['id'] not in allowed for p in boundary.participants):
        raise ValueError('Unexpected participant in restricted approved room')
    from band.client.rest import DEFAULT_REQUEST_OPTIONS
    from band_rest.agent_api_chats import RenameAgentChatRequestChat
    await tools.rest.agent_api_chats.rename_agent_chat(room_id,
        chat=RenameAgentChatRequestChat(title=f'Safe Scribe approved {room_id[:8]}'),request_options=DEFAULT_REQUEST_OPTIONS)
    human=state['recording'].get('human_id')
    if human:
        await boundary.add_participant(human)
    await recruit(boundary,'grapher',ids)
    previous=await room_records(boundary,ids)
    sent=[r for r in previous if r['kind']=='APPROVAL']
    if sent:
        approved_payload(previous,room_id)
        if sent[-1]['digest']!=current['digest'] or sent[-1]['revision']!=current['revision']:
            raise ValueError('Approved room already contains a different immutable revision')
    else:
        ts=datetime.now(timezone.utc).isoformat()
        manifest,lineage_verification=processing_manifest(records,state,tools.room_id)
        outbound={'revision':current['revision'],'digest':current['digest'],'brief':current['brief'],
                  'enrichment':approval.get('enrichment',[]),'unresolved_follow_ups':approval.get('unresolved_follow_ups',[]),
                  'scope':'approved_room','case_id':tools.room_id,'approved_room_id':room_id,'human_id':human,
                  'manifest':manifest,'provenance_kind':'runtime_field_processing_not_read_receipts',
                  'lineage_verification':lineage_verification}
        from hallway.common.brief import identifier_violations
        if identifier_violations(outbound,state['recording']):
            raise ValueError('Boundary envelope contains identifier; delivery blocked')
        # Only runtime-generated transport timestamps are added after the DOB
        # heuristic. They are not model/transcript fields and cannot carry text.
        for entry in manifest:
            entry['ts']=ts
        await post(boundary,'APPROVAL',outbound,['grapher'],ids)
    if not any(r['kind']=='BOUNDARY_SENT' and r.get('approved_room_id')==room_id for r in records):
        await post(tools,'BOUNDARY_SENT',checkpoint,['scribe'],ids)
    return {'status':'APPROVED','revision':current['revision'],'approved_room_id':room_id,'scope':'approved_room'}


def approved_payload(records: list[dict], room_id: str | None = None) -> dict:
    if not room_id:
        raise ValueError('Approved-room boundary requires current room context')
    if any(r['kind'] in ('TRANSCRIPT','BRIEF') for r in records):
        raise ValueError('Raw case evidence is forbidden in approved-room context')
    approvals=[r for r in records if r['kind']=='APPROVAL']
    if not approvals:
        raise ValueError('No authenticated Critic approval in this boundary room')
    payload=approvals[-1]
    expected={'kind','message_id','revision','digest','brief','enrichment','unresolved_follow_ups','scope',
              'case_id','approved_room_id','human_id','manifest','provenance_kind','lineage_verification'}
    if set(payload)-expected or payload.get('scope')!='approved_room' or payload.get('approved_room_id')!=room_id or payload.get('case_id')==room_id:
        raise ValueError('Approval envelope does not match restricted boundary room')
    brief=Brief.model_validate(payload['brief'])
    if digest(brief.model_dump())!=payload['digest']:
        raise ValueError('Approval digest mismatch')
    for old in approvals:
        if old.get('digest')!=payload['digest'] or old.get('revision')!=payload['revision'] or old.get('case_id')!=payload['case_id']:
            raise ValueError('Conflicting approved revisions; graph execution blocked')
    if not isinstance(payload.get('manifest'),list):
        raise ValueError('Processing manifest required')
    return payload
