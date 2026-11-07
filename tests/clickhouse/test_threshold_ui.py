import os
from datetime import date
from pathlib import Path
import unittest
import test_threshold as fixtures


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_UI_TEST')=='1','UI image required')
class ThresholdUITest(unittest.TestCase):
    setUp=fixtures.ThresholdTest.setUp
    prepare=fixtures.ThresholdTest.prepare

    def test_valid_physical_retry_during_pin_still_renders_climate_filters(self):
        from streamlit.testing.v1 import AppTest
        from unittest.mock import patch
        from greenhouse.clickhouse import proof_cache,registry
        from greenhouse.clickhouse.repository import Repository
        result,*_=self.prepare();previous=dict(os.environ)
        self.addCleanup(lambda:(os.environ.clear(),os.environ.update(previous)))
        for key in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD'):os.environ.pop(key,None)
        os.environ.update(GREENHOUSE_BACKEND='clickhouse',GREENHOUSE_RAW_DIR=str(self.raw),GREENHOUSE_CH_COORDINATOR=str(self.root))
        app=AppTest.from_file(str(Path(__file__).resolve().parents[2]/'src/greenhouse/app/home.py'),default_timeout=180).run()
        app.radio(key='screen_choice').set_value('🌡️ Khí hậu (BR-01)').run()
        index=next(i for i,v in enumerate(app.selectbox(key='clim_pub_choice').options) if self.scope in v)
        proof_cache.clear();loader=registry._envelope;injected=[]
        def retry_after_proof(client,scope,selected):
            envelope=loader(client,scope,selected)
            if scope==self.scope and not injected:
                row=client.query('SELECT * FROM source_measure WHERE scope_id={scope:String} LIMIT 1',{'scope':scope})[0]
                Repository(client,self.root)._insert('source_measure',[row]);injected.append(True)
            return envelope
        with patch.object(registry,'_envelope',side_effect=retry_after_proof):
            app.selectbox(key='clim_pub_choice').select_index(index).run()
        self.assertTrue(injected)
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertFalse(app.error,([e.value for e in app.error],[e.value for e in app.caption]))
        self.assertIsNotNone(app.date_input(key='clim_window'))

    def test_climate_drain_exports_and_saved_profile_with_postgres_unconfigured(self):
        from streamlit.testing.v1 import AppTest
        result,_,_,_,_=self.prepare()
        previous=dict(os.environ)
        self.addCleanup(lambda:(os.environ.clear(),os.environ.update(previous)))
        for key in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD'): os.environ.pop(key,None)
        os.environ.update(GREENHOUSE_BACKEND='clickhouse',GREENHOUSE_RAW_DIR=str(self.raw),GREENHOUSE_CH_COORDINATOR=str(self.root))
        app=AppTest.from_file(str(Path(__file__).resolve().parents[2]/'src/greenhouse/app/home.py'),default_timeout=180).run()
        app.radio(key='screen_choice').set_value('🌡️ Khí hậu (BR-01)').run()
        index=next(i for i,v in enumerate(app.selectbox(key='clim_pub_choice').options) if self.scope in v)
        app.selectbox(key='clim_pub_choice').select_index(index).run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertFalse(app.error,([e.value for e in app.error],[e.value for e in app.caption]))
        app.date_input(key='clim_window').set_value((date(2020,3,3),date(2020,3,3))).run()
        app.selectbox(key='clim_rule_choice').select_index(1).run()
        self.assertFalse(app.exception,[e.message for e in app.exception]);self.assertFalse(app.error,[e.value for e in app.error])
        self.assertTrue(any('demo' in item.value.lower() or 'minh họa' in item.value.lower() for item in list(app.caption)+list(app.markdown)+list(app.warning)))
        self.assertTrue(app.get('download_button'))
        app.radio(key='screen_choice').set_value('🧪 EC/pH drain (BR-03)').run()
        index=next(i for i,v in enumerate(app.selectbox(key='drain_pub_choice').options) if self.scope in v)
        app.selectbox(key='drain_pub_choice').select_index(index).run()
        app.selectbox(key='drain_rule_choice').select_index(1).run()
        self.assertFalse(app.exception,[e.message for e in app.exception]);self.assertFalse(app.error,[e.value for e in app.error])
        self.assertTrue(app.get('download_button'))
        app.text_input(key='drain_profile_evidence').set_value('Engineer fixture evidence')
        app.button(key='drain_save_profile').click().run()
        self.assertFalse(app.exception,[e.message for e in app.exception]);self.assertFalse(app.error,[e.value for e in app.error])
        self.assertIn('drain-user:',app.selectbox(key='drain_rule_choice').value)
        self.assertTrue(any('user_unverified' in v for v in app.selectbox(key='drain_rule_choice').options))
        # New immutable profile is still offered on a fresh rerun.
        app.run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertIn('drain-user:',app.selectbox(key='drain_rule_choice').value)
