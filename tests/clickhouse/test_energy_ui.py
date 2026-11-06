import os
from pathlib import Path
import unittest
import test_energy as fixtures


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_UI_TEST')=='1','UI image required')
class EnergyUITest(unittest.TestCase):
    setUp=fixtures.EnergyTest.setUp
    prepare=fixtures.EnergyTest.prepare

    def test_energy_and_cost_scenario_production_ui_without_postgres(self):
        from streamlit.testing.v1 import AppTest
        _,_,_,_,_,_=self.prepare()
        previous=dict(os.environ)
        self.addCleanup(lambda:(os.environ.clear(),os.environ.update(previous)))
        for key in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD'):os.environ.pop(key,None)
        os.environ.update(GREENHOUSE_BACKEND='clickhouse',GREENHOUSE_RAW_DIR=str(self.raw),GREENHOUSE_CH_COORDINATOR=str(self.root))
        app=AppTest.from_file(str(Path(__file__).resolve().parents[2]/'src/greenhouse/app/home.py'),default_timeout=180).run()
        app.radio(key='screen_choice').set_value('⚡ Năng lượng (BR-04)').run()
        index=next(i for i,v in enumerate(app.selectbox(key='energy_publication').options) if self.scope in v)
        app.selectbox(key='energy_publication').select_index(index).run()
        self.assertFalse(app.exception,[e.message for e in app.exception]);self.assertFalse(app.error,[e.value for e in app.error])
        self.assertEqual(next(m.value for m in app.metric if m.label=='kWh/kg A'),'25.000')
        self.assertTrue(app.get('download_button'))
        app.radio(key='screen_choice').set_value('💶 Chi phí & Kịch bản (BR-06)').run()
        self.assertFalse(app.exception,[e.message for e in app.exception]);self.assertFalse(app.error,[e.value for e in app.error])
        # Same published scope follows the shared context into the cost view.
        self.assertTrue(any('180.88' in m.value or '180,88' in m.value for m in app.metric),[(m.label,m.value) for m in app.metric])
        # Persist through the real UI; default form explicitly sets rates/changes.
        app.button(key='btn_save_scenario').click().run()
        self.assertFalse(app.exception,[e.message for e in app.exception]);self.assertFalse(app.error,[e.value for e in app.error])
        self.assertTrue(any('scenario:' in option for option in app.selectbox(key='cost_scenario_select').options))
        app.run()
        self.assertFalse(app.exception,[e.message for e in app.exception]);self.assertFalse(app.error,[e.value for e in app.error])
        self.assertTrue(app.get('download_button'))
