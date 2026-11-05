"""Saved browser exports replay via immutable publication ID, never current-only.

Reads accepted files without changing bytes; writes only byte-parity.json.
Source may now have a newer current publication: EXPECTED_PUBLICATION still
resolves its historical sealed energy/harvest bundles and frozen cycle catalogue.
"""
import hashlib,json,os
from pathlib import Path
import psycopg
from greenhouse.app import wiring,energy_exports
from greenhouse.contracts import QueryContext
from greenhouse.settings import Settings

ROOT=Path('/evidence')
PUB=os.environ.get('EXPECTED_PUBLICATION','892b4cd58cdf4d099510974d91fa64ab')
with psycopg.connect(**Settings.from_env().connection_kwargs(),options='-c default_transaction_read_only=on') as conn:
    svc=wiring.intensity_service(conn)
    pub=svc.energy.adapter.publication(PUB)
    cycles=wiring.catalog_service(conn).cycles(pub)
    contexts={cy.compartment_name:QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cy.cycle_id,(cy.compartment_id,),cy.start,cy.end) for cy in cycles}
    portfolio=svc.get_portfolio(contexts.values())
    result=svc.get_intensity(contexts['AICU']);reference=svc.get_intensity(contexts['Reference'])
    quantities=svc.energy.get_quantities(contexts['AICU'])
    expected={'AICU-summary.csv':energy_exports.summary_csv(result),
        'AICU-quantities.csv':energy_exports.quantities_csv(result,quantities),
        'AICU-harvest.csv':energy_exports.harvest_csv(result),
        'AICU-portfolio.csv':energy_exports.portfolio_csv(portfolio),
        'AICU-result.json':energy_exports.result_json(result),
        'AICU-portfolio.json':energy_exports.portfolio_json(portfolio),
        'Reference-result.json':energy_exports.result_json(reference)}
    session=json.loads((ROOT/'session.json').read_text())
    reports=[]
    for key,value in expected.items():
        compartment,suffix=key.split('-',1)
        files=[f for f in session['downloads'] if f.startswith('energy-'+compartment+'-') and f.endswith('-'+suffix)]
        assert len(files)==1,(key,files)
        name=files[0];data=(ROOT/name).read_bytes()
        assert data==value,name
        checksum=hashlib.sha256(data).hexdigest()
        assert checksum==session['downloads'][name]['sha256']
        reports.append({'file':name,'bytes':len(data),'sha256':checksum,'exact_service_parity':True})
    report={'publication_id':PUB,'immutable_id_lookup':True,'all_match':True,'files':reports}
    (ROOT/'byte-parity.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
