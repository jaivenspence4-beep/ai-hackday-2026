import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from hallway.common.runtime import run_agent
from hallway.tests.test_spine import IDS

class InboxRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_desk_never_starts_watcher(self):
        agent=SimpleNamespace(run=AsyncMock())
        with patch.dict(os.environ,{'ENABLE_LOCAL_INBOX':'1'}):
            await run_agent('scribe',agent)
        agent.run.assert_awaited_once()
    async def test_desk_shares_started_rest_and_cancels_watcher(self):
        calls=[];cancelled=asyncio.Event()
        class Agent:
            runtime=SimpleNamespace(link=SimpleNamespace(rest=object()))
            async def __aenter__(self):calls.append('start');return self
            async def __aexit__(self,*args):calls.append('stop')
            async def run_forever(self):await asyncio.sleep(0.01)
        async def watcher(lobby,ids):
            calls.append('watch')
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        agent=Agent()
        env={'ENABLE_LOCAL_INBOX':'1','BAND_LOBBY_ROOM_ID':'00000000-0000-4000-8000-000000000001','BAND_CHARGE_HUMAN_ID':'00000000-0000-4000-8000-000000000002'}
        with patch.dict(os.environ,env),patch('hallway.common.runtime.identities',return_value=IDS),patch('hallway.common.runtime.AgentTools',return_value='lobby') as bound,patch('hallway.ingest.watch.watch_inbox',side_effect=watcher):
            await run_agent('desk',agent)
        self.assertEqual(calls,['start','watch','stop']);self.assertTrue(cancelled.is_set())
        self.assertIs(bound.call_args.args[1],agent.runtime.link.rest)
