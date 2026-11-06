"""Production home.py AppTest against actual ClickHouse, no PG environment."""
import os
import unittest
from datetime import date
from pathlib import Path
import test_water as fixtures


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_UI_TEST')=='1','UI image and isolated ClickHouse required')
class WaterUITest(unittest.TestCase):
    setUp=fixtures.WaterTest.setUp

    def test_production_water_screen_filters_rule_lineage_and_exports(self):
        from streamlit.testing.v1 import AppTest
        from greenhouse.clickhouse.water_run import run
        run(self.client,self.root,self.raw,self.root/'output',self.scope,self.workflow,self.config)
        from greenhouse.clickhouse.repository import Repository
        from greenhouse.contracts import IrrigationRuleProfile, to_dict
        from decimal import Decimal
        rule=IrrigationRuleProfile('water-demo-'+self.scope,'fixture-water-rule',1,self.scope,'Automatoes',
            Decimal('10'),Decimal('50'),Decimal('0'),Decimal('100'),'synthetic UI demonstration',True,'demo_rule')
        Repository(self.client,self.root).register_configuration('irrigation_rule',self.scope,rule.rule_version,'rule-'+self.scope,to_dict(rule))
        previous=dict(os.environ)
        self.addCleanup(lambda:(os.environ.clear(),os.environ.update(previous)))
        for key in ('PGHOST','PGDATABASE','PGUSER','PGPASSWORD'):
            os.environ.pop(key,None)
        os.environ['GREENHOUSE_BACKEND']='clickhouse'
        os.environ['GREENHOUSE_RAW_DIR']=str(self.raw)
        app=AppTest.from_file(str(Path(__file__).resolve().parents[2]/'src/greenhouse/app/home.py'),default_timeout=120).run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertFalse(app.error,[e.value for e in app.error])
        # Other test scopes may be present: explicitly select this publication.
        index=next(i for i,option in enumerate(app.selectbox(key='pub_choice').options) if self.scope in option)
        app.selectbox(key='pub_choice').select_index(index).run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        metric=lambda label:next(item.value for item in app.metric if item.label==label)
        self.assertEqual(metric('Irrigation observed'),'9,36 L/m²')
        self.assertEqual(metric('Drain observed'),'6,89 L/m²')
        self.assertEqual(metric('Độ phủ paired'),'40,00 %')
        app.date_input(key='window').set_value((date(2020,3,2),date(2020,3,2))).run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertEqual(metric('Drain observed'),'3,89 L/m²')
        self.assertEqual(metric('Độ phủ paired'),'100,00 %')
        self.assertTrue(any('CSV ngày' in button.label for button in app.get('download_button')))
        app.selectbox(key='rule_choice').select_index(1).run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        self.assertTrue(any('demo_rule' in item.value for item in app.markdown))
        self.assertTrue(any('Raw hash xác minh khớp' in item.value for item in app.success))
        self.assertEqual(len(app.get('plotly_chart')),1)
