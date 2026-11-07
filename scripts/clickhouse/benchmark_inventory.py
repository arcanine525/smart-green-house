"""Read-only schema, storage and resource inventory for isolated engine benchmark."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend',choices=('postgres','clickhouse'),required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();result={'backend':a.backend,'at_utc':datetime.now(timezone.utc).isoformat()}
    if a.backend=='postgres':
        import psycopg
        from greenhouse.settings import Settings
        s=Settings.from_env()
        if (s.pg_host,s.pg_database,s.pg_user)!=('postgres','greenhouse_bench_pg','greenhouse_bench'):
            raise ValueError('Private benchmark endpoint required')
        with psycopg.connect(**(s.connection_kwargs()|{'options':'-c default_transaction_read_only=on'})) as c:
            assert c.execute('SHOW transaction_read_only').fetchone()[0]=='on'
            result['version']=c.execute('SELECT version()').fetchone()[0]
            result['database_bytes']=c.execute('SELECT pg_database_size(current_database())').fetchone()[0]
            settings=('shared_buffers','effective_cache_size','work_mem','max_parallel_workers_per_gather','jit')
            result['server_settings']={k:c.execute('SELECT current_setting(%s)',(k,)).fetchone()[0] for k in settings}
            rows=c.execute('''SELECT n.nspname,c.relname,pg_relation_size(c.oid),pg_indexes_size(c.oid),pg_total_relation_size(c.oid),c.reltuples::bigint
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r'
                AND n.nspname NOT IN ('pg_catalog','information_schema') ORDER BY n.nspname,c.relname''').fetchall()
            result['tables']=[dict(zip(('schema','table','heap_bytes','index_bytes','total_including_toast_bytes','estimated_rows'),r)) for r in rows]
            result['indexes']=[dict(zip(('schema','table','index','definition'),r)) for r in c.execute("SELECT schemaname,tablename,indexname,indexdef FROM pg_indexes WHERE schemaname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2,3").fetchall()]
            result['storage_note']='pg_database_size includes indexes, TOAST and database metadata; row estimates are not exact counts; excludes WAL and server logs.'
    else:
        from greenhouse.clickhouse.client import Client,ClickHouseSettings
        s=ClickHouseSettings.from_env()
        if (s.host,s.database,s.user)!=('clickhouse','greenhouse_bench_ch','greenhouse_bench'):
            raise ValueError('Private benchmark endpoint required')
        c=Client(s)
        try:
            result['version']=c.query('SELECT version() AS version')[0]['version']
            result['tables']=c.query('''SELECT name,engine,sorting_key,primary_key,partition_key,create_table_query,total_rows,total_bytes
                FROM system.tables WHERE database=currentDatabase() ORDER BY name''')
            result['parts']=c.query('''SELECT table,active,count() AS parts,sum(rows) AS rows,sum(bytes_on_disk) AS bytes_on_disk,
                sum(data_compressed_bytes) AS compressed_bytes,sum(data_uncompressed_bytes) AS uncompressed_bytes
                FROM system.parts WHERE database=currentDatabase() GROUP BY table,active ORDER BY table,active''')
            result['active_part_bytes']=sum(int(r['bytes_on_disk']) for r in result['parts'] if int(r['active']))
            result['inactive_part_bytes']=sum(int(r['bytes_on_disk']) for r in result['parts'] if not int(r['active']))
            result['background_merges']=c.query('SELECT table,elapsed,progress,num_parts,total_size_bytes_compressed,memory_usage FROM system.merges WHERE database=currentDatabase()')
            result['server_settings']=c.query("SELECT name,value FROM system.settings WHERE name IN ('max_threads','max_memory_usage','max_bytes_before_external_group_by','max_bytes_before_external_sort') ORDER BY name")
            result['storage_note']='Active/inactive MergeTree parts include part indexes; system logs and coordinator/ready files excluded. Record Docker volume allocated bytes separately.'
        finally:c.close()
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2,default=str)+'\n')
    print(json.dumps({'status':'inventoried','backend':a.backend,'tables':len(result['tables'])}))


if __name__=='__main__':main()
