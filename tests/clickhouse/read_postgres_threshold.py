"""SELECT-only reference on an existing AGC publication; stdout is summary evidence."""
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import psycopg
from greenhouse.settings import Settings
from greenhouse.contracts import QueryContext,RuleBasis,RuleScheduleRevision,RuleScheduleSegment,rule_segment_sort_key
from greenhouse.db.catalog import PostgresCatalogAdapter
from greenhouse.db.climate_adapter import PostgresClimateQueryAdapter
from greenhouse.db.drain_adapter import PostgresDrainQueryAdapter
from greenhouse.queries import ClimateService,DrainService

START=datetime(2020,3,2);END=datetime(2020,3,4)


def demo(scope,comp,kind,start,end):
    segments=[]
    if kind=='climate':
        for basis in (RuleBasis.REQUESTED,RuleBasis.REALIZED):
            for metric,delta in (('temperature','0.5'),('co2','50')):
                segments.append(RuleScheduleSegment(metric,'climate',basis,start,end,delta=Decimal(delta)))
    else:
        for metric,lo,hi in (('ec','2','5'),('ph','5.5','6.5')):
            segments.append(RuleScheduleSegment(metric,'drain_process_computer',RuleBasis.MEASURED,start,end,lower=Decimal(lo),upper=Decimal(hi)))
    family='threshold-agc-demo:'+kind+':'+comp
    return RuleScheduleRevision(family+':v1',family,1,scope,comp,'Synthetic demo; BR01/03 documented examples, not an AGC agronomic standard',True,'demo_rule',tuple(sorted(segments,key=rule_segment_sort_key)))


def metrics(evaluation):
    return [{k:str(getattr(r,k)) for k in ('metric','expected_minutes','valid_minutes','unknown_minutes','out_minutes','suspect_minutes','out_low_minutes','out_high_minutes')} for r in evaluation.result.metric_results]


def fingerprint(rows,kind):
    names=('tair','co2air','t_heat_sp','t_vent_sp','co2_sp','t_heat_vip','t_ventlee_vip','co2_vip') if kind=='climate' else ('ec_drain','ph_drain')
    payload=[[r.slot_start.isoformat(),*[str(getattr(r,n).normalize()) if getattr(r,n) is not None else None for n in names],r.measure_quality] for r in rows]
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def inside_episodes(evaluation):
    return [[e.metric,e.original_start.isoformat(),e.original_end.isoformat(),str(e.original_minutes)] for e in evaluation.result.episodes if START<e.original_start and e.original_end<END]


def main():
    output=[]
    kwargs=Settings.from_env().connection_kwargs()|{'options':'-c default_transaction_read_only=on'}
    with psycopg.connect(**kwargs) as connection:
        assert connection.execute('SHOW transaction_read_only').fetchone()[0]=='on'
        catalog=PostgresCatalogAdapter(connection)
        selected=[r for r in catalog.catalog_publications() if r.snapshot_id.startswith('agc-2nd-2019-') and r.climate_usable and r.drain_usable]
        assert len(selected)==1
        chosen=selected[0]
        for cycle in catalog.catalog_cycles(chosen.publication_id):
            q=QueryContext(chosen.scope_id,chosen.publication_id,chosen.snapshot_id,cycle.cycle_id,(cycle.compartment_id,),START.date(),END.date())
            for kind,base,service_type,bases in (('climate',PostgresClimateQueryAdapter,ClimateService,(RuleBasis.REQUESTED,RuleBasis.REALIZED)),('drain',PostgresDrainQueryAdapter,DrainService,(RuleBasis.MEASURED,))):
                rule=demo(chosen.scope_id,cycle.compartment_id,kind,datetime.combine(cycle.start,datetime.min.time()),datetime.combine(cycle.end,datetime.min.time()))
                class Adapter(base):
                    def schedule(self,version):
                        assert version==rule.rule_version
                        return rule
                adapter=Adapter(connection);pub=adapter.publication(chosen.publication_id)
                read=adapter.read_climate_slots if kind=='climate' else adapter.read_drain_slots
                rows=read(q,pub,window_start=START,window_end=END)
                service=service_type(adapter)
                for basis in bases:
                    ev=service.get_evaluation(q,rule_version=rule.rule_version,use_cache=False,**({'basis':basis} if kind=='climate' else {}))
                    output.append({'compartment':cycle.compartment_name,'kind':kind,'basis':basis.value,'slots':len(rows),'slot_fingerprint':fingerprint(rows,kind),'metrics':metrics(ev),'inside_episodes':inside_episodes(ev),
                        'lineage_source':connection.execute('SELECT r.source_record_id,s.raw_hash,s.line_number FROM core.record_revision r JOIN meta.source_record s USING(source_record_id) WHERE r.revision_id=%s',(rows[0].revision_id,)).fetchone()})
        print(json.dumps({'status':'passed','engine':'postgres','read_only':True,'publication':chosen.publication_id,'window':[START.isoformat(),END.isoformat()],'results':output},indent=2))


if __name__=='__main__': main()
