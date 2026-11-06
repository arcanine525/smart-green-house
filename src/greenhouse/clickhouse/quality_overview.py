"""Quality decisions over the selected immutable core membership and load audit."""
from collections import Counter
from datetime import datetime,timedelta
import json

from greenhouse.clickhouse.quality import measure_verdict,AGC_QUALITY_DEFINITION
from greenhouse.clickhouse.registry import publication_envelope,reference
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.threshold import CLIMATE,DRAIN
from greenhouse.clickhouse.water import WaterAdapter

GROUPS={'GreenhouseClimate':(('climate',tuple(CLIMATE)),('drain',tuple(DRAIN))),
    'Resources':(('resource',('Heat_cons','ElecHigh','ElecLow','CO2_cons','Irr','Drain')),),
    'Production':(('harvest',('ProdA','ProdB')),)}


def _inputs(client,publication_id,cycle_id,start,end):
    if start>end:raise ValueError('Quality window must be ordered')
    envelope=publication_envelope(client,publication_id);pub=reference(client,publication_id,envelope)
    cycles=[c for c in WaterAdapter(client).catalog_cycles(publication_id) if c.cycle_id==cycle_id]
    if len(cycles)!=1:raise ValueError('Quality cycle absent or ambiguous in pinned publication')
    return envelope,pub,cycles[0],datetime.combine(start,datetime.min.time()),datetime.combine(end+timedelta(days=1),datetime.min.time())


def _decisions(client,publication_id,cycle_id,start,end):
    envelope,pub,cycle,lo,hi=_inputs(client,publication_id,cycle_id,start,end)
    for rev in Repository(client,None).load_membership(pub.scope_id,envelope['load_id']):
        if rev['compartment_id']!=cycle.compartment_id:continue
        record=json.loads(rev['payload']);source_time=record.get('slot_start') or record['source_timestamp']
        if source_time is None:continue
        when=datetime.fromisoformat(source_time)
        if not lo<=when<hi:continue
        for kind,codes in GROUPS.get(rev['record_type'],()):
            for code in codes:
                _,status,app,reason=measure_verdict(AGC_QUALITY_DEFINITION,record,code)
                yield dict(observed_at=when,record_type=kind,measure_code=code,status=status,applicability_status=app,reason_code=reason,
                    revision_id=rev['revision_id'],relative_path=rev['source_file'],line_number=int(rev['line_number']),line_end=int(rev['line_end']))


def measures(client,publication_id,cycle_id,start,end):
    fields=('record_type','measure_code','status','applicability_status','reason_code')
    counts=Counter(tuple(r[k] for k in fields) for r in _decisions(client,publication_id,cycle_id,start,end))
    return [dict(zip(fields,key),decisions=count) for key,count in sorted(counts.items(),key=lambda pair:tuple('' if v is None else str(v) for v in pair[0]))]


def excluded(client,publication_id,cycle_id,start,end):
    return sorted((r for r in _decisions(client,publication_id,cycle_id,start,end) if r['status']!='accepted' or r['applicability_status']!='applicable'),
        key=lambda r:(r['observed_at'],r['revision_id'],r['measure_code']))


def quarantined(client,publication_id,cycle_id,start,end):
    envelope,pub,cycle,lo,hi=_inputs(client,publication_id,cycle_id,start,end)
    audit=Repository(client,None).load_audit(pub.scope_id,envelope['load_id']);result=[]
    for row in audit['issues']:
        source=json.loads(row['payload'])
        if source['source_file'].split('/')[0]!=cycle.compartment_id:continue
        stamp=datetime.fromisoformat(source['source_timestamp']) if source['source_timestamp'] else None
        if stamp is not None and not lo<=stamp<hi:continue
        codes=sorted({i['code'] for i in source['issues']})
        policies=sorted({i.get('policy_version',source['time_policy_version'] if 'time' in i['code'] else source['parser_version']) for i in source['issues']})
        result.append(dict(source_timestamp=stamp,disposition=row['disposition'],reason_code=';'.join(codes) or row['disposition'],policy_version=';'.join(policies),
            relative_path=row['source_file'],line_number=source['line_number'],line_end=source['line_end'],source_record_id=row['source_record_id']))
    return sorted(result,key=lambda r:(r['source_timestamp'] is None,r['source_timestamp'] or datetime.min,r['source_record_id']))
