"""Prepare the native backup fixture sequentially in one guarded process.

The production proof cache may reuse a proof across actions only after checking
the physical fingerprint and the entire configuration registry. No guard is
disabled. The sensor helper exits last with live SQLite readers to retain WAL.
"""
import runpy
import sys

sys.argv = ['import_postgres_scenarios.py', '--proof', '/state/parity/parity.json']
runpy.run_path('/repro/import_postgres_scenarios.py', run_name='__main__')
sys.argv = ['configure.py']
runpy.run_path('/repro/configure.py', run_name='__main__')
sys.argv = ['agc_sensor.py', 'seed']
runpy.run_path('/repro/agc_sensor.py', run_name='__main__')
