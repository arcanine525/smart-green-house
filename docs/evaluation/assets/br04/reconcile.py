"""Independent original-CSV oracle versus sealed quantities/BR04 service.

Run inside processing Docker; raw mount read-only. No main-owned oracle imports.
Source values/qualities/counts compare exactly. Derived division/sums compare
within1e-40 because the published contract rounds each heat bucket to50digits,
whereas this oracle divides whole source sums at100digits. No tolerance is
applied to original source values, accepted population or revision identities.
"""
import csv
from datetime import date,timedelta
from decimal import Decimal,InvalidOperation,localcontext
import hashlib,json,os
from pathlib import Path
import psycopg
from greenhouse.app import wiring
from greenhouse.contracts import QueryContext,to_json
from greenhouse.db.loader import AGC_SCOPE
from greenhouse.settings import Settings

EXPECTED=os.environ.get('EXPECTED_PUBLICATION','892b4cd58cdf4d099510974d91fa64ab')
RAW=Path('/data/raw/agc-2nd-2019/c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68')
OUT=Path('/evidence')
TOL=Decimal('1e-40')
EPOCH=date(1899,12,30)
CODES={'heat':'Heat_cons','elec_high':'ElecHigh','elec_low':'ElecLow'}


def total(values):
    with localcontext() as ctx:
        ctx.prec=100
        return sum(values,Decimal(0))


def value(token):
    if token.strip().lower() in ('','nan','na','n/a'): return None,'missing'
    try:
        result=Decimal(token.strip())
        if not result.is_finite(): raise InvalidOperation
        return result,'accepted'
    except InvalidOperation:
        return None,'invalid'


def source(path):
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest_files[path.relative_to(RAW).as_posix()]==checksum
    with path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.reader(f); headers=[h.strip() for h in next(reader)]
        for line,row in enumerate(reader,2):
            serial=Decimal(row[0]); assert serial==int(serial)
            yield EPOCH+timedelta(days=int(serial)),line,dict(zip(headers,row))


def close(a,b):
    return a is not None and b is not None and abs(a-b)<=TOL


manifest=json.loads((RAW/'manifest.json').read_text())
manifest_files={f['path']:f['sha256'] for f in manifest['files']}
reports=[]; results=[]; contexts=[]
with psycopg.connect(**Settings.from_env().connection_kwargs(),options='-c default_transaction_read_only=on') as conn:
    svc=wiring.intensity_service(conn,{manifest['snapshot_id']:RAW})
    pub=svc.energy.adapter.publication(EXPECTED)  # immutable ID, never current-only catalogue
    cycles=wiring.catalog_service(conn).cycles(pub)
    for cy in cycles:
        context=QueryContext(pub.scope_id,pub.publication_id,pub.snapshot_id,cy.cycle_id,(cy.compartment_id,),cy.start,cy.end)
        contexts.append(context)
        r=svc.get_intensity(context); results.append(r)
        q=svc.energy.get_quantities(context)
        raw_resources={day:(line,row) for day,line,row in source(RAW/cy.compartment_name/'Resources.csv') if cy.start<=day<cy.end}
        raw_harvest=[(d,l,row) for d,l,row in source(RAW/cy.compartment_name/'Production.csv')]
        checks=[]
        for x in q:
            line,row=raw_resources[x.resource_date]
            val,status=value(row[CODES[x.component.value]])
            checks.append(x.source_value==val and x.quality.value==status and x.revision_id is not None)
        assert all(checks), cy.compartment_name+' source/quality mismatch'
        assert len(q)==3*len(raw_resources)==3*(cy.end-cy.start).days
        source_sums={code:total(value(row[code])[0] for _,row in raw_resources.values()) for code in CODES.values()}
        for component in r.energy.components:
            assert component.observed_source_per_m2==source_sums[CODES[component.component.value]]
        a_rows=[(d,l,row) for d,l,row in raw_harvest if cy.start<=d<cy.end]
        a_sum=total(value(row['ProdA'])[0] for _,_,row in a_rows if value(row['ProdA'])[1]=='accepted')
        assert len(a_rows)==len(r.harvest_a)
        for contribution,(day,line,row) in zip(r.harvest_a,a_rows):
            assert contribution.record.harvest_date==day
            assert contribution.record.prod_a.value==value(row['ProdA'])[0]
            assert contribution.record.prod_a.quality.value==value(row['ProdA'])[1]
            assert contribution.record.prod_b.value==value(row['ProdB'])[0]
            assert contribution.basis.area_m2==Decimal('62.5')
            assert contribution.basis.status.value=='assumed_unverified'
        with localcontext() as ctx:
            ctx.prec=100
            heat=source_sums['Heat_cons']*Decimal('62.5')/Decimal('3.6')
            high=source_sums['ElecHigh']*Decimal('62.5');low=source_sums['ElecLow']*Decimal('62.5')
            energy=heat+high+low; mass=a_sum*Decimal('62.5')
            expected=None if cy.compartment_name=='Reference' else energy/mass
            sensitive=expected*Decimal('1.536') if expected is not None else None
        assert all(close(a,b) for a,b in zip((heat,high,low),(c.official_absolute_kwh for c in r.energy.components)))
        assert close(r.energy.official_total_kwh,energy) and r.observed_a_kg==mass
        if expected is None:
            assert r.intensity_kwh_kg_a is None and not r.comparison_eligible
            assert r.harvest_state.unresolved_records==1
            assert [str(d) for d,_,_ in raw_harvest if not cy.start<=d<cy.end]==['2019-02-14']
        else:
            assert close(r.intensity_kwh_kg_a,expected) and r.comparison_eligible
            assert close(r.sensitivity_kwh_kg_a,sensitive)
        # Concrete immutable file/line/revision/quality lineage for both fact sets.
        for revision,day,line in ((q[0].revision_id,q[0].resource_date,raw_resources[q[0].resource_date][0]),
                                  (r.harvest_a[0].record.revision_id,a_rows[0][0],a_rows[0][1])):
            lineage=svc.energy.get_lineage(pub,revision_id=revision)
            assert lineage.raw_hash_verified and lineage.line_number==line
            assert lineage.source_timestamp.date()==day
        OUT.joinpath('actual-'+cy.compartment_name+'.json').write_text(to_json(r)+'\n')
        reports.append(dict(compartment=cy.compartment_name,resource_days=len(raw_resources),quantity_rows=len(q),
            harvest_rows=len(a_rows),raw_outside_rows=len(raw_harvest)-len(a_rows),source_sums={k:str(v) for k,v in source_sums.items()},
            observed_a_kg=str(mass),energy_kwh=str(energy),intensity_kwh_kg_a=str(expected) if expected is not None else None,
            sensitivity_kwh_kg_a=str(sensitive) if sensitive is not None else None,all_match=True,lineage_verified=True))
    p=svc.get_portfolio(contexts)
    eligible=[r for r in reports if r['intensity_kwh_kg_a'] is not None]
    with localcontext() as ctx:
        ctx.prec=100
        e=total(Decimal(r['energy_kwh']) for r in eligible); a=total(Decimal(r['observed_a_kg']) for r in eligible)
        oracle=e/a
    assert close(p.intensity_kwh_kg_a,oracle) and p.production_a_kg==a
    assert len(p.included_cycle_ids)==5 and len(p.excluded_cycle_ids)==1
    OUT.joinpath('actual-portfolio.json').write_text(to_json(p)+'\n')
report={'publication_id':EXPECTED,'source_and_quality_exact':True,'derived_tolerance':str(TOL),'cycles':reports,
        'eligible_population':p.included_cycle_ids,'excluded_population':p.excluded_cycle_ids,
        'portfolio_energy_kwh':str(e),'portfolio_a_kg':str(a),'portfolio_intensity':str(oracle),'all_match':True}
OUT.joinpath('raw-reconciliation.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'publication':EXPECTED,'cycles':len(reports),'all_match':True,'eligible':len(eligible),'portfolio':str(oracle)}))
