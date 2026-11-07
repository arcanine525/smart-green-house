"""Packaged entrypoint routes explicit commands without a PostgreSQL import."""
from contextlib import redirect_stdout,redirect_stderr
import copy
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_energy as fixtures
from greenhouse.cli import main


class RoutingTest(unittest.TestCase):
    def test_clickhouse_is_the_default_for_cli_and_ui_without_pg_environment(self):
        from greenhouse.app import runtime
        with patch.dict(os.environ,{},clear=True),patch('greenhouse.clickhouse.cli.command',return_value=0) as native:
            self.assertEqual(runtime.backend(),'clickhouse')
            self.assertEqual(main(['migrate']),0)
            native.assert_called_once_with('migrate',[])

    def test_explicit_postgres_keeps_the_rollback_entrypoint(self):
        with patch.dict(os.environ,{},clear=True),patch('greenhouse.cli.core_command',return_value=0) as legacy:
            self.assertEqual(main(['migrate','--backend','postgres']),0)
            legacy.assert_called_once_with('migrate',[])

    def test_first_namespace_discards_unlabelled_history_and_write_intents(self):
        from greenhouse.app.shared_context import begin_backend
        state={'screen_choice':'cost','_historical_context':{'old':'pin'},'write_intent':'old'}
        begin_backend(state,('clickhouse','db','8123','candidate','demo'))
        self.assertEqual(set(state),{'screen_choice','_warehouse_identity'})
        state['keep']='same namespace'
        begin_backend(state,state['_warehouse_identity'])
        self.assertIn('keep',state)
    def test_stream_backend_flag_is_forwarded_after_top_level_parse(self):
        with patch('greenhouse.sensor.stream_cli.stream_command',return_value=0) as stream:
            self.assertEqual(main(['stream','--backend','clickhouse','--available-now']),0)
            self.assertEqual(stream.call_args.args[0],['--backend','clickhouse','--available-now'])

    def test_invalid_backend_and_credentials_are_safe(self):
        with patch.dict(os.environ,{'CH_PASSWORD':'top-secret','CH_PORT':'top-secret'}):
            output=StringIO()
            with redirect_stderr(output):self.assertEqual(main(['migrate','--backend','clickhouse']),1)
            self.assertNotIn('top-secret',output.getvalue())


@unittest.skipUnless(os.environ.get('GREENHOUSE_CH_INTEGRATION')=='1','isolated ClickHouse required')
class BackendCLITest(unittest.TestCase):
    setUp=fixtures.EnergyTest.setUp
    prepare=fixtures.EnergyTest.prepare

    def test_catalog_discovers_receipts_but_selected_pin_still_verifies_data(self):
        result,*_=self.prepare()
        from greenhouse.clickhouse.water import WaterAdapter,WaterMart
        with patch.object(WaterMart,'verify',side_effect=ValueError('sentinel verification failure')):
            option=next(r for r in WaterAdapter(self.client).catalog_publications() if r.scope_id==self.scope)
            self.assertTrue(option.usable)
            with self.assertRaisesRegex(ValueError,'sentinel verification failure'):
                WaterAdapter(self.client).publication(result['publication_id'])

    def test_core_only_scope_is_discoverable_and_explicitly_not_ready(self):
        from greenhouse.clickhouse.load import load_snapshot
        from greenhouse.clickhouse.publication import Publications
        from greenhouse.clickhouse.water import WaterAdapter
        loaded=load_snapshot(self.client,self.root,self.raw,self.root/'output',self.scope)
        pub=Publications(self.client,self.root).publish(self.scope,self.workflow,loaded['load_id'])
        option=next(r for r in WaterAdapter(self.client).catalog_publications() if r.scope_id==self.scope)
        self.assertEqual(option.publication_id,pub['publication_id'])
        self.assertFalse(option.usable)

    def invoke(self,command,*args,expected=0):
        output=StringIO();error=StringIO()
        with redirect_stdout(output),redirect_stderr(error),patch('greenhouse.settings.Settings.from_env',side_effect=AssertionError('PG settings fallback')):
            code=main([command,'--backend','clickhouse','--coordinator',str(self.root),*args])
        self.assertEqual(code,expected,(output.getvalue(),error.getvalue()))
        return json.loads(output.getvalue()) if output.getvalue() else None

    def test_migrate_pipeline_resume_lineage_explain_correction_and_new_pin(self):
        result,energy,_,_,pub,context=self.prepare(include_climate=True)
        config=self.root/'bundle-config.json'
        config.write_text(json.dumps(dict(water=self.config,harvest=self.harvest,threshold=self.threshold,energy=self.energy_config)))
        self.assertEqual(self.invoke('migrate')['status'],'migrated')
        output=self.root/'output';output.mkdir(exist_ok=True)
        self.assertEqual(self.invoke('setup','--snapshot',str(self.raw),'--output-root',str(output))['status'],'ready')
        common=['--scope-id',self.scope,'--snapshot',str(self.raw),'--output-root',str(output),'--config-json',str(config)]
        resumed=self.invoke('run-pipeline',*common,'--resume',self.workflow)
        self.assertTrue(resumed['resumed']);self.assertEqual(resumed['publication_id'],result['publication_id'])
        facts=self.invoke('explain-core','--scope-id',self.scope,'--compartment','Automatoes','--date','2020-03-02','--measure','Heat_cons')
        self.assertEqual(facts['rows'][0]['value'],'36')
        rid=facts['rows'][0]['revision_id']
        lineage=self.invoke('lineage','--scope-id',self.scope,'--snapshot',str(self.raw),'--publication-id',pub.publication_id,'--revision-id',rid,'--kind','energy')
        self.assertTrue(lineage['raw_hash_verified'])
        self.assertEqual([u['measure_code'] for u in lineage['units']],['CO2_cons','Drain','ElecHigh','ElecLow','Heat_cons','Irr'])
        from greenhouse.clickhouse.repository import Repository
        source=next(r for r in Repository(self.client,None).load_membership(self.scope,result['load_id']) if r['revision_id']==rid)
        values=json.loads(source['payload'])['values'];new=copy.deepcopy(values);new['Heat_cons']='72'
        oldfile=self.root/'old.json';newfile=self.root/'new.json';oldfile.write_text(json.dumps(values));newfile.write_text(json.dumps(new))
        correction=['--scope-id',self.scope,'--source-record-id',source['source_record_id'],'--record-type','resource',
            '--old-value',str(oldfile),'--new-value',str(newfile),'--actor','cli reviewer','--evidence','Synthetic CLI correction','--correction-id','cli:'+self.scope]
        saved=self.invoke('correct-core',*correction)
        self.assertEqual(saved['status'],'corrected')
        self.assertEqual(self.invoke('correct-core',*correction)['status'],'already_applied')
        newer=self.invoke('transform',*common,'--workflow-id',uuid4().hex)
        self.assertNotEqual(newer['publication_id'],pub.publication_id)
        self.assertEqual(str(energy.get_summary(context).official_total_kwh),'4687.5')
        self.invoke('register-agc-tariff','--scope-id',self.scope,'--publication-id',pub.publication_id,expected=1)
