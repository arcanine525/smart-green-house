import os
from pathlib import Path
import unittest
import test_harvest as fixtures


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_UI_TEST')=='1','UI image and isolated ClickHouse required')
class HarvestUITest(unittest.TestCase):
    setUp=fixtures.HarvestTest.setUp
    prepare=fixtures.HarvestTest.prepare

    def test_production_harvest_ui_preserves_partial_and_quarantine_states(self):
        from streamlit.testing.v1 import AppTest
        self.prepare()
        previous=dict(os.environ)
        self.addCleanup(lambda:(os.environ.clear(),os.environ.update(previous)))
        for key in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD'):
            os.environ.pop(key,None)
        os.environ['GREENHOUSE_BACKEND']='clickhouse'; os.environ['GREENHOUSE_RAW_DIR']=str(self.raw)
        app=AppTest.from_file(str(Path(__file__).resolve().parents[2]/'src/greenhouse/app/home.py'),default_timeout=120).run()
        app.radio(key='screen_choice').set_value('🍅 Thu hoạch (BR-05)').run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        index=next(i for i,label in enumerate(app.selectbox(key='harv_pub_choice').options) if self.scope in label)
        app.selectbox(key='harv_pub_choice').select_index(index).run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertFalse(app.error,[e.value for e in app.error])
        metric=lambda label:next(item.value for item in app.metric if item.label==label)
        self.assertEqual(metric('A observed'),'3,00 kg/m²')
        self.assertEqual(metric('B observed'),'0,75 kg/m²')
        self.assertTrue(any('chưa hoàn chỉnh' in item.value for item in app.markdown))
        self.assertTrue(app.get('download_button'))
        app.selectbox(key='harv_cycle_choice').select_index(1).run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertEqual(metric('A observed'),'— kg/m²')
        self.assertFalse(app.error,[e.value for e in app.error])

        app.button(key='harv-audit-0').click().run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertTrue(any('CSV audit (raw đã xác minh)' in button.label for button in app.get('download_button')))
