"""Raw transport diagnostic only; compare full/compact tuples without weakening conflicts."""
import hashlib,json,time
from datetime import datetime
from greenhouse.clickhouse.client import Client,ClickHouseSettings,urlopen
from greenhouse.clickhouse.encoding import source_clock_us
c=Client(ClickHouseSettings.from_env())
params={'scope':'agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68:historical',
    'pub':'2be7aa20c0ff4fc8b5d8fd0fbc87b635','cycle':'agc-cycle-Automatoes','comp':'Automatoes',
    'start':source_clock_us(datetime(2019,12,16)),'end':source_clock_us(datetime(2020,5,30))}
base='''SELECT slot_clock_us,groupUniqArray(tuple(slot_start,revision_id,logical_record_key,quality_policy_version,correction_id,correction_version)) AS metadata,
    EXPRESSION AS measures FROM threshold_measure WHERE scope_id={scope:String} AND publication_id={pub:String}
    AND bundle='climate' AND cycle_id={cycle:String} AND compartment_id={comp:String}
    AND slot_clock_us>={start:Int64} AND slot_clock_us<{end:Int64} GROUP BY slot_clock_us ORDER BY slot_clock_us
    SETTINGS optimize_aggregation_in_order=1,max_bytes_before_external_group_by=67108864,max_bytes_before_external_sort=67108864'''
expressions={'full':'groupArray(tuple(measure_code,value_exact,value_decimal,projection_status,quality,applicability,reason))',
    'compact':'arrayMap(m -> tuple(m.1,m.2,m.5,m.6,m.7),groupUniqArray(tuple(measure_code,value_exact,value_decimal,projection_status,quality,applicability,reason)))'}
expected=None;result=[]
for name,expression in expressions.items():
    query=base.replace('EXPRESSION',expression)+' FORMAT JSONEachRow'
    begin=time.perf_counter();byte_count=0;rows=0;digest=hashlib.sha256()
    with urlopen(c._request(query,params,None,streaming=True),timeout=c.settings.timeout) as response:
        for line in response:
            byte_count+=len(line);row=json.loads(line);measures={}
            for item in row['measures']:
                if name=='compact':
                    if item[0] in measures:raise ValueError('Conflicting full tuple hidden by projection')
                    measures[item[0]]=item
                else:
                    if item[0] in measures and measures[item[0]]!=item:raise ValueError('Full tuple conflict')
                    measures[item[0]]=item
            if len(row['metadata'])!=1 or len(measures)!=8:raise ValueError('Wrong metadata/grain')
            canonical=[row['slot_clock_us'],row['metadata'],[measures[k] if name=='compact' else [measures[k][j] for j in (0,1,4,5,6)] for k in sorted(measures)]]
            digest.update(json.dumps(canonical,separators=(',',':')).encode()+b'\n');rows+=1
    value={'transport':name,'seconds':time.perf_counter()-begin,'bytes':byte_count,'rows':rows,'fingerprint':digest.hexdigest()}
    if rows!=47808:raise ValueError('Full-cycle cardinality differs')
    if expected is not None and value['fingerprint']!=expected:raise ValueError('Transport projection differs')
    expected=value['fingerprint'];result.append(value)
print(json.dumps({'status':'diagnostic_only','same_full_cycle_value_quality_identity':True,'results':result},indent=2))
