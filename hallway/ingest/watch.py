"""Local Desk intake only: final text files → Band case; never an agent orchestrator.

The upload producer must publish complete .txt files by atomic rename. Desk runs
on the presenting laptop; no audio is read or transmitted by this module.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from band.client.rest import DEFAULT_REQUEST_OPTIONS
from band.runtime.tools.agent import AgentTools
from band_rest.agent_api_chats import RenameAgentChatRequestChat
from hallway.common.band_cfg import configure_timeouts
from hallway.common.room import raw_messages, room_records, post, recruit
from hallway.ingest.fixture import local_salt, recording_from_text

MARKER='SAFESCRIBE-INGEST/1\n'
MAX_BYTES=1_000_000


def read_final_text(path: Path, inbox: Path) -> bytes:
    """Read one complete regular file under the configured inbox, never a link."""
    root=Path(inbox).resolve()
    requested=Path(path).parent.resolve()/Path(path).name
    target=root/requested.name
    if requested!=target or target.suffix!='.txt':
        raise ValueError('Only final .txt files directly inside the inbox are accepted')
    fd=os.open(target,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size>MAX_BYTES:
            raise ValueError('Inbox file must be regular text within the 1 MB limit')
        data=stream.read(MAX_BYTES+1)
        after=os.fstat(stream.fileno())
    fingerprint=lambda s:(s.st_dev,s.st_ino,s.st_mtime_ns,s.st_size)
    if fingerprint(before)!=fingerprint(after) or len(data)>MAX_BYTES:
        raise ValueError('Inbox file changed during intake; retry after atomic publication')
    if not data.strip():
        raise ValueError('Inbox transcript is empty')
    return data


def _checkpoint_records(messages,desk_id):
    records=[]
    for message in messages:
        text=message.get('content','')
        if message.get('sender_id')!=desk_id or not isinstance(text,str):
            continue
        matches=list(re.finditer(r'(?m)^SAFESCRIBE-INGEST/1$',text))
        if len(matches)!=1:
            continue
        try:
            value=json.loads(text[matches[0].end():])
        except ValueError:
            continue
        if isinstance(value,dict) and value.get('kind') in ('LOCAL_CASE_CREATED','LOCAL_CASE_SENT'):
            records.append(value)
    return records


async def _checkpoint(lobby,kind,payload):
    content='**LOCAL INTAKE** — Operator-uploaded text case status.\n\n'+MARKER+json.dumps({'kind':kind,**payload})
    await lobby.send_event(content=content,message_type='task')


async def ingest_text_file(lobby,ids,path: Path,inbox: Path) -> dict:
    """Desk-authorized local intake; charge nurse identity comes from actual Band roster."""
    if os.getenv('ENABLE_LOCAL_INBOX')!='1':
        raise ValueError('Local inbox intake requires ENABLE_LOCAL_INBOX=1')
    human_id=os.getenv('BAND_CHARGE_HUMAN_ID','').strip()
    try:
        human_id=str(UUID(human_id))
    except (ValueError,AttributeError):
        raise ValueError('Configure BAND_CHARGE_HUMAN_ID to the actual lobby user UUID') from None
    configure_timeouts()
    await lobby.get_participants()
    human=next((p for p in lobby.participants if p.get('id')==human_id and str(p.get('type','')).casefold()=='user'),None)
    if not human or ids['desk'] not in {p.get('id') for p in lobby.participants}:
        raise ValueError('Configured charge nurse and Desk must be actual lobby participants')
    data=read_final_text(path,inbox)
    transcript=data.decode('utf-8')
    # Both pseudonym and dedup token use the existing private local salt.
    token=hmac.new(local_salt(),b'local-operator-upload\0'+data,hashlib.sha256).hexdigest()
    checkpoints=[r for r in _checkpoint_records(await raw_messages(lobby),ids['desk']) if r.get('input_token')==token]
    if checkpoints and any(r.get('human_id')!=human_id for r in checkpoints):
        raise ValueError('Local intake checkpoint belongs to a different configured human')
    sent=[r for r in checkpoints if r['kind']=='LOCAL_CASE_SENT']
    if sent:
        return sent[-1]
    created=[r for r in checkpoints if r['kind']=='LOCAL_CASE_CREATED']
    recording=recording_from_text(transcript,source='synthetic local operator upload')
    recording.update(human_id=human_id,human_name=human.get('name'),source_type='local_operator_upload')
    if created:
        checkpoint=created[-1]
        room_id=checkpoint['room_id']
    else:
        room_id=await lobby.create_chatroom()
        if room_id==lobby.room_id:
            raise ValueError('New case must differ from lobby')
        checkpoint={'input_token':token,'room_id':room_id,'human_id':human_id,
                    'at':datetime.now(timezone.utc).isoformat(),'source_type':'local_operator_upload'}
        await _checkpoint(lobby,'LOCAL_CASE_CREATED',checkpoint)
    recording['at']=checkpoint['at']
    case=AgentTools(room_id,lobby.rest,agent_id=ids['desk'])
    await case.get_participants()
    allowed={ids['desk'],ids['scribe'],ids['critic'],human_id}
    if any(p['id'] not in allowed for p in case.participants):
        raise ValueError('Unexpected participant in local intake case')
    await lobby.rest.agent_api_chats.rename_agent_chat(room_id,
        chat=RenameAgentChatRequestChat(title=f'Safe Scribe case {room_id[:8]}'),request_options=DEFAULT_REQUEST_OPTIONS)
    existing=await room_records(case,ids)
    transcripts=[r for r in existing if r['kind']=='TRANSCRIPT']
    if transcripts:
        if any(r.get('recording')!=recording for r in transcripts):
            raise ValueError('Conflicting transcript in local intake checkpoint')
    else:
        await case.add_participant(human_id)
        for role in ('scribe','critic'):
            await recruit(case,role,ids)
        await post(case,'TRANSCRIPT',{'recording':recording},['scribe','critic'],ids)
    await _checkpoint(lobby,'LOCAL_CASE_SENT',checkpoint)
    return dict(checkpoint,status='CASE_SENT')


async def watch_inbox(lobby,ids,*,inbox: Path | None = None,poll_seconds: float = 2.0,stop: asyncio.Event | None = None):
    """Run as a local Desk task after the atomic upload producer is installed."""
    if os.getenv('ENABLE_LOCAL_INBOX')!='1':
        raise ValueError('Local inbox watcher requires explicit opt-in')
    root=Path(inbox or os.getenv('SAFESCRIBE_INBOX','inbox')).resolve()
    root.mkdir(parents=True,exist_ok=True)
    seen={}
    while stop is None or not stop.is_set():
        for path in sorted(root.glob('*.txt')):
            try:
                snapshot=path.lstat()
                fingerprint=(snapshot.st_ino,snapshot.st_mtime_ns,snapshot.st_size)
                if seen.get(path.name)==fingerprint:
                    continue
                await ingest_text_file(lobby,ids,path,root)
                seen[path.name]=fingerprint
            except Exception as exc:
                logging.warning('Local Desk intake paused error=%s',type(exc).__name__)
        await asyncio.sleep(max(0.1,poll_seconds))
