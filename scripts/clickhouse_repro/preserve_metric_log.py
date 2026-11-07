"""Retain legacy metric_log parts without background merges in this rehearsal.

Use with local.xml metric_log removal: new servers do not create this optional
wide telemetry table. An existing table keeps its data and original DDL here;
only its automatic-merge setting changes. query_log is never disabled.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path

from common import connect,write_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('Preserve previous maintenance receipt')
    with closing(connect()) as client:
        table=client.query("SELECT create_table_query FROM system.tables WHERE database='system' AND name='metric_log'")
        if not table:
            write_json(args.output,dict(status='passed',legacy_metric_log=False,query_log_unchanged=True))
            return
        original=table[0]['create_table_query']
        client.execute('SYSTEM STOP MERGES system.metric_log')
        def inventory():
            return client.query("SELECT name,rows,hash_of_all_files FROM system.parts WHERE database='system' AND table='metric_log' AND active ORDER BY name")
        before=inventory()
        client.execute('ALTER TABLE system.metric_log MODIFY SETTING max_bytes_to_merge_at_max_space_in_pool=0')
        # Persisted table setting, rather than an in-memory STOP surviving only
        # until restart. New log collection is separately disabled in XML.
        client.execute('SYSTEM START MERGES system.metric_log')
        after=inventory();by_name={row['name']:row for row in after}
        if not all(by_name.get(row['name'])==row for row in before):
            raise ValueError('Legacy metric log parts changed during preservation')
        current=client.query("SELECT create_table_query FROM system.tables WHERE database='system' AND name='metric_log'")[0]['create_table_query']
        if 'max_bytes_to_merge_at_max_space_in_pool = 0' not in current:
            raise ValueError('Legacy telemetry merge setting was not persisted')
        write_json(args.output,dict(status='passed',legacy_metric_log=True,
            original_ddl=original,current_ddl=current,retained_parts=before,
            retained_rows=sum(int(row['rows']) for row in before),
            all_original_parts_and_checksums_preserved=True,query_log_unchanged=True,
            business_tables_changed=False,server_limits_changed=False))
        print(json.dumps(dict(status='passed',retained_parts=len(before),query_log_unchanged=True)),flush=True)


if __name__=='__main__':main()
