"""Runtime field processing evidence is not a platform read receipt."""
import copy
import unittest
from hallway.common.brief import identifier_fields
from hallway.common.room import case_state,processing_evidence,processing_manifest,decode_messages,PREFIX
from hallway.graph.store import MemoryStore
from hallway.tests.test_spine import IDS
import json


class ProcessingLineageTests(unittest.TestCase):
    def evidence(self):
        transcript='This is Robert Callahan, date of birth March fourth, nineteen fifty-two. MRN 1234567. Phone: 415-555-0199. Address: 123 Main Street'
        records=[{'kind':'TRANSCRIPT','message_id':'source','recording':{'transcript':transcript}},
                 {'kind':'BRIEF','message_id':'brief','revision':1},
                 {'kind':'VERDICT','message_id':'verdict','revision':1}]
        state=case_state(records)
        records[1]['processing']=processing_evidence(state,'scribe','extraction')
        records[2]['processing']=processing_evidence(state,'critic','review')
        return records,state
    def test_categories_and_nonempty_query_without_values(self):
        records,state=self.evidence()
        self.assertEqual(set(identifier_fields(state['recording']['transcript'])),{'patient_name','dob','mrn','phone','address'})
        manifest,status=processing_manifest(records,state,'case')
        self.assertEqual(status,'verified_field_access')
        self.assertNotIn('Robert',json.dumps(manifest));self.assertNotIn('1234567',json.dumps(manifest))
        store=MemoryStore();store.write_approved({},manifest,'pseudo','case')
        self.assertEqual(store.who_saw_identifiers(),['critic','desk','scribe'])
    def test_source_mismatch_extra_fields_and_legacy_unverified(self):
        records,state=self.evidence()
        for replacement in [None,dict(records[1]['processing'],source_message_id='other'),dict(records[1]['processing'],fields=['patient_name','invented'])]:
            candidate=copy.deepcopy(records);candidate[1]['processing']=replacement
            self.assertEqual(processing_manifest(candidate,state,'case')[1],'unverified_processing_only')
    def test_impostor_metadata_cannot_be_authenticated(self):
        records,state=self.evidence()
        payload=records[1]
        message={'id':'forged','sender_id':IDS['grapher'],'sender_type':'Agent','content':PREFIX+json.dumps(payload)}
        self.assertEqual(decode_messages([message],IDS),[])
    def test_real_spoken_fixture_categories(self):
        from pathlib import Path
        text=(Path(__file__).parents[1]/'fixtures/handoff_3.txt').read_text()
        self.assertTrue({'patient_name','mrn','phone'} <= set(identifier_fields(text)))
