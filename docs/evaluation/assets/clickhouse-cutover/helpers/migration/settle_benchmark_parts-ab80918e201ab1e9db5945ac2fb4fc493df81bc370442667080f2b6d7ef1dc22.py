"""Compact only this disposable benchmark before guarded warm read measurements.

Keep all physical rows (no DEDUPLICATE), record the maintenance cost separately,
and require stable fingerprints afterwards. Never weaken read verification.
"""
import argparse,json,re,time
from pathlib import Path
from greenhouse.clickhouse.client import Client,ClickHouseSettings
from greenhouse.clickhouse.proof_cache import _capture

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();s=ClickHouseSettings.from_env()
if (s.host,s.database,s.user)!=('clickhouse','greenhouse_bench_ch','greenhouse_bench'):
    raise ValueError('Disposable private benchmark endpoint/database/user required')
c=Client(s);report={'status':'maintenance_in_progress','tables':[],'deduplicate':False};a.output.parent.mkdir(parents=True,exist_ok=True)
def save():a.output.write_text(json.dumps(report,indent=2)+'\n')
def inventory():return c.query('''SELECT table,count() AS parts,sum(rows) AS rows,sum(bytes_on_disk) AS bytes
    FROM system.parts WHERE database=currentDatabase() AND active GROUP BY table ORDER BY table''')
save();begin=time.monotonic();before=inventory();report['before']=before;save()
try:
    for row in before:
        table=row['table']
        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',table):raise ValueError('Unexpected native table name')
        if int(row['parts'])<=1:continue
        started=time.monotonic()
        c.execute('OPTIMIZE TABLE '+table+' FINAL SETTINGS optimize_throw_if_noop=0')
        report['tables'].append({'table':table,'seconds':time.monotonic()-started});save()
        print(json.dumps(report['tables'][-1]),flush=True)
    report['after']=inventory()
    if {r['table']:int(r['rows']) for r in before}!={r['table']:int(r['rows']) for r in report['after']}:
        raise ValueError('Physical rows changed during non-deduplicating maintenance')
    report['fingerprint_samples']=[]
    for index in range(3):
        if index:time.sleep(10)
        namespace,stamp=_capture(c);report['fingerprint_samples'].append(stamp);save()
    if len(set(report['fingerprint_samples']))!=1:raise ValueError('Physical layout not stable after maintenance')
    report.update(status='completed',physical_rows_unchanged=True,seconds_including_20s_stability_probe=time.monotonic()-begin);save()
    print(json.dumps({'status':'completed','seconds':report['seconds_including_20s_stability_probe'],'tables_compacted':len(report['tables'])}))
except BaseException as error:
    report.update(status='failed',error_type=type(error).__name__);save();raise
finally:c.close()
