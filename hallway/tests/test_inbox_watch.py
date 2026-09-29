"""Offline local intake authorization, checkpoint and file-boundary tests."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hallway.common.room import decode_messages
from hallway.ingest import fixture,watch
from hallway.tests.test_spine import FakeBand,IDS,RECORDING

HUMAN='10000000-0000-4000-8000-000000000001'


class LocalBand(FakeBand):
    async def add_participant(self,identifier):
        self.added.append(identifier)
        if not any(p['id']==identifier for p in self.participants):
            self.participants.append({'id':identifier,'handle':'@'+identifier,
                                      'type':'User' if identifier==HUMAN else 'Agent'})
        return {'status':'added'}
    async def create_chatroom(self):
        room=LocalBand();room.messages=[];room.role='desk';room.rest=self;room.rooms=self.rooms
        room.room_id=f'20000000-0000-4000-8000-{len(self.rooms):012d}'
        room.participants=[{'id':IDS['desk'],'handle':'@team/desk','type':'Agent'}]
        self.rooms[room.room_id]=room
        return room.room_id


def bind(room_id,rest,agent_id):
    room=rest.rooms[room_id];room.role='desk';return room


class InboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.inbox=self.root/'inbox';self.inbox.mkdir()
        self.path=self.inbox/'patient-private-name.txt';self.path.write_text(RECORDING['transcript'])
        self.lobby=LocalBand();self.lobby.role='desk';self.lobby.room_id='lobby';self.lobby.messages=[];self.lobby.rooms={'lobby':self.lobby}
        self.lobby.participants=[{'id':IDS['desk'],'handle':'@team/desk','type':'Agent'},
            {'id':HUMAN,'handle':'@real-nurse','type':'User','name':'Actual Charge Nurse'}]
        self.env=patch.dict(os.environ,{'ENABLE_LOCAL_INBOX':'1','BAND_CHARGE_HUMAN_ID':HUMAN,
            'HANDOFF_PSEUDONYM_SALT_FILE':str(self.root/'private-salt')})
        self.env.start();self.addCleanup(self.env.stop)
        self.tools=patch.object(watch,'AgentTools',side_effect=bind);self.tools.start();self.addCleanup(self.tools.stop)
    async def test_actual_lobby_human_and_explicit_local_source_not_forged_reply(self):
        result=await watch.ingest_text_file(self.lobby,IDS,self.path,self.inbox)
        case=self.lobby.rooms[result['room_id']]
        records=decode_messages(case.messages,IDS)
        source=next(r['recording'] for r in records if r['kind']=='TRANSCRIPT')
        self.assertEqual(source['human_id'],HUMAN)
        self.assertEqual(source['human_name'],'Actual Charge Nurse')
        self.assertEqual(source['source_type'],'local_operator_upload')
        self.assertNotIn('initiating_message_id',result)
        self.assertFalse(any(m.get('sender_type')=='User' for m in case.messages))
        self.assertNotIn(self.path.name,json.dumps(self.lobby.messages))
        self.assertTrue(self.path.exists())
        again=await watch.ingest_text_file(self.lobby,IDS,self.path,self.inbox)
        self.assertEqual(again['room_id'],result['room_id']);self.assertEqual(len(self.lobby.rooms),2)
    async def test_opt_in_and_actual_user_required(self):
        with patch.dict(os.environ,{'ENABLE_LOCAL_INBOX':'0'}):
            with self.assertRaisesRegex(ValueError,'ENABLE_LOCAL_INBOX'):
                await watch.ingest_text_file(self.lobby,IDS,self.path,self.inbox)
        self.lobby.participants[-1]['type']='Agent'
        with self.assertRaisesRegex(ValueError,'actual lobby participants'):
            await watch.ingest_text_file(self.lobby,IDS,self.path,self.inbox)
        self.assertEqual(len(self.lobby.rooms),1)
    async def test_retry_after_sent_transcript_uses_checkpoint_without_duplicate(self):
        original=watch._checkpoint
        async def fail_receipt(lobby,kind,payload):
            if kind=='LOCAL_CASE_SENT':raise RuntimeError('simulated lost receipt')
            await original(lobby,kind,payload)
        with patch.object(watch,'_checkpoint',side_effect=fail_receipt):
            with self.assertRaises(RuntimeError):
                await watch.ingest_text_file(self.lobby,IDS,self.path,self.inbox)
        result=await watch.ingest_text_file(self.lobby,IDS,self.path,self.inbox)
        records=decode_messages(self.lobby.rooms[result['room_id']].messages,IDS)
        self.assertEqual(sum(r['kind']=='TRANSCRIPT' for r in records),1)
        self.assertEqual(len(self.lobby.rooms),2)
    async def test_read_rejects_escape_symlink_temporary_and_oversized(self):
        outside=self.root/'outside.txt';outside.write_text('private')
        link=self.inbox/'link.txt';link.symlink_to(outside)
        pending=self.inbox/'partial.tmp';pending.write_text('not complete')
        oversized=self.inbox/'large.txt';oversized.write_bytes(b'x'*(watch.MAX_BYTES+1))
        for path in (outside,link,pending,oversized):
            with self.subTest(path=path.name),self.assertRaises((ValueError,OSError)):
                watch.read_final_text(path,self.inbox)
    async def test_checkpoint_cannot_be_forged_by_scribe(self):
        payload={'kind':'LOCAL_CASE_SENT','input_token':'fake','room_id':'fake'}
        message={'sender_id':IDS['scribe'],'content':watch.MARKER+json.dumps(payload)}
        self.assertEqual(watch._checkpoint_records([message],IDS['desk']),[])
    async def test_shared_pseudonym_matches_fixture_loader_exactly(self):
        fixture_file=self.inbox/'unit.txt';fixture_file.write_text(RECORDING['transcript'])
        with patch.object(fixture,'FIXTURES',self.inbox):
            original=fixture.load_recording('unit')
        uploaded=fixture.recording_from_text(RECORDING['transcript'],'synthetic local operator upload')
        self.assertEqual(original['pseudo_id'],uploaded['pseudo_id'])
        self.assertEqual(original['source'],'synthetic text fixture; not clinical patient data')
    async def test_watcher_consumes_final_txt_and_ignores_temp_file(self):
        (self.inbox/'pending.tmp').write_text('private incomplete upload')
        stop=asyncio.Event()
        original=watch.ingest_text_file
        async def ingest(*args,**kwargs):
            result=await original(*args,**kwargs);stop.set();return result
        with patch.object(watch,'ingest_text_file',side_effect=ingest) as called:
            await watch.watch_inbox(self.lobby,IDS,inbox=self.inbox,poll_seconds=0.1,stop=stop)
        called.assert_awaited_once()
        self.assertEqual(called.call_args.args[2],self.path.resolve())

if __name__=='__main__':unittest.main()
