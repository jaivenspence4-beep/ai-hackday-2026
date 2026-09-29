import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from hallway.tests.test_spine import FakeBand,IDS,BASIC
from hallway.common.brief import Brief
from hallway.common.room import submit_brief,review,decode_messages
from hallway.common.runtime import make_tools

class ResearchWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_scribe_publishes_before_requesting_drug_room(self):
        band=FakeBand();calls=[]
        async def request(tools,ids,brief,recording):
            self.assertTrue(any(r['kind']=='BRIEF' for r in decode_messages(tools.messages,ids)))
            calls.append(brief);return {'status':'pending'}
        with patch.dict(os.environ,{'ENABLE_DRUG_RESEARCH':'1'}),patch('hallway.common.research_room.request_research',side_effect=request):
            await submit_brief(band,IDS,Brief.model_validate(BASIC))
        self.assertEqual(len(calls),1)
    async def test_critic_waits_for_research_then_includes_sourced_facts(self):
        band=FakeBand();brief=copy.deepcopy(BASIC);brief['follow_ups']=[]
        await submit_brief(band,IDS,Brief.model_validate(brief));band.role='critic'
        fact={'drug':'warfarin','url':'https://medical.test/guideline','title':'Guideline','snippet':'Sourced context','mock':False,'source':'brave'}
        with patch.dict(os.environ,{'ENABLE_DRUG_RESEARCH':'1','ENABLE_APPROVED_ROOM':'0'}),patch('hallway.common.research_room.research_for_review',return_value={'status':'pending','facts':[]}):
            self.assertEqual((await review(band,IDS,True,[]))['status'],'WAITING_FOR_RESEARCH')
        self.assertFalse(any(r['kind']=='APPROVAL' for r in decode_messages(band.messages,IDS)))
        with patch.dict(os.environ,{'ENABLE_DRUG_RESEARCH':'1','ENABLE_APPROVED_ROOM':'0'}),patch('hallway.common.research_room.research_for_review',return_value={'status':'ready','facts':[fact]}):
            await review(band,IDS,True,[])
        approval=[r for r in decode_messages(band.messages,IDS) if r['kind']=='APPROVAL'][-1]
        self.assertEqual(approval['enrichment'],[fact])
    async def test_research_room_dispatch_and_tools_have_no_model_routing_inputs(self):
        band=FakeBand();holder={'agent':SimpleNamespace(runtime=SimpleNamespace(link=SimpleNamespace(rest=band)))}
        with patch.dict(os.environ,{'ENABLE_DRUG_RESEARCH':'1'}):
            scribe=make_tools('scribe',holder,IDS);researcher=make_tools('researcher',holder,IDS)
        for name,tools in [('band_relay_research',scribe),('band_research_drugs',researcher)]:
            self.assertEqual(next(t for t in tools if t.name==name).args,{})
        read=next(t for t in scribe if t.name=='band_read_case')
        with patch('hallway.common.runtime.AgentTools',return_value=band),patch.dict(os.environ,{'ENABLE_DRUG_RESEARCH':'1'}),patch('hallway.common.research_room.decode_research',return_value=[{'kind':'RESEARCH_REQUEST'}]),patch('hallway.common.research_room.read_research',new=AsyncMock(return_value={'drugs':['warfarin']})):
            result=await read.ainvoke({},config={'configurable':{'thread_id':'research-room'}})
        self.assertEqual(result,{'room_kind':'research','request':{'drugs':['warfarin']}})
    async def test_resume_after_publish_failure_does_not_create_revision(self):
        band=FakeBand();holder={'agent':SimpleNamespace(runtime=SimpleNamespace(link=SimpleNamespace(rest=band)))}
        with patch.dict(os.environ,{'ENABLE_DRUG_RESEARCH':'1'}),patch('hallway.common.research_room.request_research',new=AsyncMock(side_effect=[RuntimeError('transport'),{'status':'pending'}])) as request:
            with self.assertRaises(RuntimeError):
                await submit_brief(band,IDS,Brief.model_validate(BASIC))
            resume=next(t for t in make_tools('scribe',holder,IDS) if t.name=='band_start_research')
            self.assertEqual(resume.args,{})
            with patch('hallway.common.runtime.AgentTools',return_value=band):
                self.assertEqual(await resume.ainvoke({},config={'configurable':{'thread_id':'case'}}),{'status':'pending'})
            self.assertEqual(request.await_count,2)
        self.assertEqual(len([r for r in decode_messages(band.messages,IDS) if r['kind']=='BRIEF']),1)
