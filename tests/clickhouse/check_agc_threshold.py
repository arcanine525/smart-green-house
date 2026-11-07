"""Bounded AGC validation against independent CSV math and SELECT-only PostgreSQL."""
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from decimal import Decimal

sys.path.insert(0,str(Path(__file__).resolve().parent))
from read_postgres_threshold import START,END,demo,metrics,fingerprint,inside_episodes
from threshold_oracle import source_rows,evaluate,CLIMATE,DRAIN
from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.threshold import ClimateAdapter,DrainAdapter
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.schedules import register_rule_schedule
from greenhouse.clickhouse.water_run import AGC_SNAPSHOT
from greenhouse.contracts import QueryContext,RuleBasis
from greenhouse.queries import ClimateService,DrainService


def main():
    client=Client(ClickHouseSettings.from_env());raw=Path('/data/raw/agc-2nd-2019')/AGC_SNAPSHOT.removeprefix('agc-2nd-2019-')
    coordinator=Path('/candidate/coordinator');scope=AGC_SNAPSHOT+':historical'
    reference=json.loads(Path('/evidence/threshold-postgres-reference-final.json').read_text())
    assert reference['read_only'] and reference['status']=='passed'
    a=ClimateAdapter(client,raw_snapshots={AGC_SNAPSHOT:raw});d=DrainAdapter(client,raw_snapshots={AGC_SNAPSHOT:raw})
    pub=a.pin_publication(scope);assert d.publication(pub.publication_id)==pub
    checks=[]
    for cycle in a.catalog_cycles(pub.publication_id):
        oracle=source_rows(raw/cycle.compartment_name/'GreenhouseClimate.csv',START,END)
        q=QueryContext(scope,pub.publication_id,AGC_SNAPSHOT,cycle.cycle_id,(cycle.compartment_id,),START.date(),END.date())
        for kind,adapter,service_type,bases in (('climate',a,ClimateService,(RuleBasis.REQUESTED,RuleBasis.REALIZED)),('drain',d,DrainService,(RuleBasis.MEASURED,))):
            rule=demo(scope,cycle.compartment_id,kind,datetime.combine(cycle.start,datetime.min.time()),datetime.combine(cycle.end,datetime.min.time()))
            register_rule_schedule(client,coordinator,rule)
            read=adapter.read_climate_slots if kind=='climate' else adapter.read_drain_slots
            rows=read(q,pub,window_start=START,window_end=END)
            assert len(rows)==len(oracle)==576
            codes=CLIMATE if kind=='climate' else DRAIN
            fields=('tair','co2air','t_heat_sp','t_vent_sp','co2_sp','t_heat_vip','t_ventlee_vip','co2_vip') if kind=='climate' else ('ec_drain','ph_drain')
            for row in rows:
                expected=oracle[row.slot_start]
                for code,field in zip(codes,fields):
                    assert getattr(row,field)==expected['values'][code],(cycle.compartment_name,row.slot_start,code)
                    assert row.measure_quality[code]==expected['quality'][code]
            service=service_type(adapter)
            lineage=service.get_lineage(pub,revision_id=rows[0].revision_id)
            assert lineage.raw_hash_verified
            for basis in bases:
                ev=service.get_evaluation(q,rule_version=rule.rule_version,use_cache=False,**({'basis':basis} if kind=='climate' else {}))
                expected_metrics,expected_episodes=evaluate(oracle,START,END,kind,basis.value)
                actual=metrics(ev);inside=inside_episodes(ev)
                assert actual==expected_metrics,(cycle.compartment_name,kind,basis,actual,expected_metrics)
                assert inside==expected_episodes,(cycle.compartment_name,kind,basis,'raw episodes differ')
                pg=next(r for r in reference['results'] if (r['compartment'],r['kind'],r['basis'])==(cycle.compartment_name,kind,basis.value))
                assert actual==pg['metrics']
                assert fingerprint(rows,kind)==pg['slot_fingerprint']
                assert inside==pg['inside_episodes'],(cycle.compartment_name,kind,basis,'PG interior episodes differ')
                assert [lineage.source_record_id,lineage.raw_hash,lineage.line_number]==pg['lineage_source']
                checks.append({'compartment':cycle.compartment_name,'kind':kind,'basis':basis.value,'rows':len(rows),'slot_fingerprint':fingerprint(rows,kind),'metrics':actual,'interior_episodes_compared':len(inside),'lineage_verified':True,'raw_oracle_match':True,'postgres_match':True})
    assert len(checks)==18
    print(json.dumps({'status':'passed','publication':pub.publication_id,'postgres_publication':reference['publication'],'scope':scope,'window':[START.isoformat(),END.isoformat()],
        'checks':checks,'limits':['Source-window rehearsal only, not full backfill','Only episodes strictly inside the selected source window are compared on AGC; complete original spans across the window are covered by hand-calculated fixtures, full AGC spans deferred to T38','Explicit demo deltas/bounds are validation assumptions, not agronomic calibration']},indent=2))


if __name__=='__main__': main()
