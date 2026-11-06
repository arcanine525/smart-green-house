"""Read-only inventories for a stopped-writer rehearsal; no secret settings printed."""
import hashlib
import json
from pathlib import Path
import sys


def file_inventory(root):
    root = Path(root)
    result = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Unexpected symlink: {path}')
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        result.append({'path': str(path.relative_to(root)), 'bytes': path.stat().st_size,
                       'sha256': digest.hexdigest()})
    return result


def database_inventory():
    import psycopg
    from psycopg import sql
    from greenhouse.settings import Settings
    result = []
    with psycopg.connect(**Settings.from_env().connection_kwargs()) as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        tables = connection.execute("""SELECT n.nspname,c.relname,c.oid FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r'
            AND n.nspname NOT IN ('pg_catalog','information_schema')
            AND n.nspname NOT LIKE 'pg_toast%' ORDER BY 1,2""").fetchall()
        for schema, table, oid in tables:
            primary = connection.execute('''SELECT a.attname FROM pg_index i
                CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ord)
                JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
                WHERE i.indrelid=%s AND i.indisprimary ORDER BY k.ord''', (oid,)).fetchall()
            order = sql.SQL(',').join(sql.Identifier(row[0]) for row in primary) if primary else sql.SQL('row_to_json(t)::text')
            query = sql.SQL("SELECT encode(sha256(convert_to(row_to_json(t)::text,'UTF8')),'hex') FROM {}.{} t ORDER BY {}").format(
                sql.Identifier(schema), sql.Identifier(table), order)
            digest, count = hashlib.sha256(), 0
            with connection.cursor(name='inventory') as cursor:
                cursor.execute(query)
                for row in cursor:
                    digest.update(row[0].encode('utf-8') + b'\n')
                    count += 1
            result.append({'table': f'{schema}.{table}', 'rows': count, 'sha256': digest.hexdigest()})
        for schema, sequence in connection.execute("SELECT schemaname,sequencename FROM pg_sequences ORDER BY 1,2").fetchall():
            value, called = connection.execute(sql.SQL('SELECT last_value,is_called FROM {}.{}').format(
                sql.Identifier(schema),sql.Identifier(sequence))).fetchone()
            result.append({'sequence':f'{schema}.{sequence}','last_value':value,'is_called':called})
    return result


if __name__ == '__main__':
    if sys.argv[1] == 'files':
        result = file_inventory(sys.argv[2])
    elif sys.argv[1] == 'database':
        result = database_inventory()
    elif sys.argv[1] == 'compare':
        left, right = (json.loads(Path(p).read_text()) for p in sys.argv[2:4])
        if left != right:
            raise AssertionError('Inventory mismatch')
        result = {'match': True, 'entries': len(left)}
    else:
        raise ValueError('Expected files, database or compare')
    print(json.dumps(result, indent=2))
