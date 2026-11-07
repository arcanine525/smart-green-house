"""Cryptographic ordered physical-row inventory for a stopped-writer CH backup.

Parts may merge between reads. Hash row contents rather than part filenames;
include every physical copy, schema definition and table row count. Bounded
external sorting and streaming avoid collecting full tables in the client.
"""
import argparse,hashlib,json,re,time
from contextlib import closing
from pathlib import Path
from common import connect

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();client=connect();report={'status':'in_progress','tables':[]}
a.output.parent.mkdir(parents=True,exist_ok=True)
def save():a.output.write_text(json.dumps(report,indent=2)+'\n')
save()
try:
    tables=client.query("SELECT name,create_table_query FROM system.tables WHERE database=currentDatabase() AND engine='MergeTree' ORDER BY name")
    for table in tables:
        name=table['name']
        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',name):raise ValueError('Unexpected native table name')
        begin=time.monotonic();count=0;checksum=hashlib.sha256()
        query='''SELECT lower(hex(SHA256(toJSONString(tuple(*))))) AS row_hash FROM '''+name+'''
            ORDER BY row_hash SETTINGS max_threads=1,max_bytes_before_external_sort=67108864'''
        with closing(client.iter_query(query)) as rows:
            for row in rows:
                value=row['row_hash']
                if not re.fullmatch('[0-9a-f]{64}',value):raise ValueError('Malformed row digest')
                checksum.update(value.encode()+b'\n');count+=1
        report['tables'].append({'table':name,'rows':count,'sha256':checksum.hexdigest(),
            'schema_sha256':hashlib.sha256(table['create_table_query'].encode()).hexdigest(),
            'seconds':time.monotonic()-begin});save()
        print(json.dumps({'stage':'inventoried','table':name,'rows':count}),flush=True)
    logical=[{k:v for k,v in row.items() if k!='seconds'} for row in report['tables']]
    report.update(status='passed',logical_inventory_sha256=hashlib.sha256(json.dumps(logical,sort_keys=True,separators=(',',':')).encode()).hexdigest())
    save()
except BaseException as error:
    report.update(status='failed',error_type=type(error).__name__);save();raise
finally:client.close()
