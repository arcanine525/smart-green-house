"""Read-only complete DTO fingerprint across six AGC cycles and both bases."""
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
import psycopg
from greenhouse.app import wiring
from greenhouse.contracts import RuleBasis, to_json
from greenhouse.settings import Settings

with psycopg.connect(**Settings.from_env().connection_kwargs()) as connection:
    if connection.execute('show transaction_read_only').fetchone()[0] != 'on':
        raise RuntimeError('read-only required')
    catalog = wiring.catalog_service(connection)
    selected = next(p for p in catalog.publications() if p.publication_id == 'df3e4bcbacab45a5b86fc37d2f53c00a')
    publication = wiring.resolve_publication(wiring.irrigation_service(connection), selected.publication_id)
    climate, drain = wiring.climate_service(connection), wiring.drain_service(connection)
    rows = {}
    for cycle in catalog.cycles(publication):
        context = wiring.build_context(publication, cycle, cycle.start, cycle.end - timedelta(days=1))
        for basis in (RuleBasis.REQUESTED, RuleBasis.REALIZED):
            name = cycle.compartment_id + ':climate:' + basis.value
            result = climate.get_evaluation(context, basis=basis, use_cache=False,
                rule_version=f'climate-demo-b01:{cycle.compartment_id}:v1').result
            rows[name] = {'sha256': hashlib.sha256(to_json(result).encode()).hexdigest(),
                          'episodes': len(result.episodes)}
            print(name, rows[name], flush=True)
        name = cycle.compartment_id + ':drain:measured'
        result = drain.get_evaluation(context, use_cache=False,
            rule_version=f'drain-demo-b03:{cycle.compartment_id}:v1').result
        rows[name] = {'sha256': hashlib.sha256(to_json(result).encode()).hexdigest(),
                      'episodes': len(result.episodes)}
        print(name, rows[name], flush=True)
    if len(rows) != 18:
        raise AssertionError(f'Expected six compartments and three evaluations each: {len(rows)}')
    Path(os.environ['PARITY_OUTPUT']).write_text(json.dumps({'publication_id':publication.publication_id,
        'database_read_only':True,'results':rows},indent=2,sort_keys=True)+'\n')
