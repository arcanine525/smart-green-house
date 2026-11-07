import json
from greenhouse.clickhouse.client import Client,ClickHouseSettings
c=Client(ClickHouseSettings.from_env())
parameters={'start':'2026-11-07 02:07:00'}
rows=c.query('''SELECT event_time_microseconds,query_duration_ms,read_rows,memory_usage,
    ProfileEvents['OSCPUWaitMicroseconds'] AS cpu_wait_us,ProfileEvents['RealTimeMicroseconds'] AS real_us,
    ProfileEvents['UserTimeMicroseconds'] AS user_us,ProfileEvents['SystemTimeMicroseconds'] AS system_us,
    ProfileEvents['OSIOWaitMicroseconds'] AS io_wait_us,ProfileEvents['DiskReadElapsedMicroseconds'] AS disk_read_us,
    ProfileEvents['ReadBufferFromFileDescriptorReadBytes'] AS disk_read_bytes,
    left(query,180) AS sql FROM system.query_log WHERE current_database=currentDatabase()
    AND type=2 AND event_time>={start:DateTime} AND query_duration_ms>500 ORDER BY query_duration_ms DESC LIMIT 20''',parameters)
merges=c.query('SELECT database,table,elapsed,progress,memory_usage FROM system.merges')
print(json.dumps({'slow_queries':rows,'all_background_merges':merges},indent=2))
