"""BR04/06 share exact, publication-frozen daily source energy quantities."""
from datetime import date
from decimal import Decimal
import json

from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.encoding import canonical_json,decimal_projection,decimal_text,digest
from greenhouse.clickhouse.journal import execute_write
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.threshold import validated_config as threshold_config
from greenhouse.clickhouse.water import WaterAdapter
from greenhouse.clickhouse.quality import measure_verdict
from greenhouse.contracts import (CatalogCycle,EffectiveAreaBasis,EnergyComponent,EnergyQuantity,CorrectionRef,
    QualityStatus,Applicability,from_dict,to_dict,energy_kwh,exact_product)

BUNDLE='energy-mart-v1'
QUANTITY_VERSION='energy-quantity-v1'
CODES={'heat':'Heat_cons','elec_high':'ElecHigh','elec_low':'ElecLow'}
DDL=(
    '''CREATE TABLE IF NOT EXISTS energy_header (
        scope_id String, publication_id String, config_version String
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id)''',
    '''CREATE TABLE IF NOT EXISTS energy_cycle (
        scope_id String, publication_id String, cycle_id String, compartment_id String,
        compartment_name String, start Date32, end Date32
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,cycle_id)''',
    '''CREATE TABLE IF NOT EXISTS energy_basis (
        scope_id String, publication_id String, compartment_id String, measure_code LowCardinality(String),
        basis_version_id String, area_exact String, area_decimal Nullable(Decimal(76,38)),
        status LowCardinality(String), valid_from Date32, valid_to_exclusive Date32, payload String
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,compartment_id,measure_code,valid_from,basis_version_id)''',
    '''CREATE TABLE IF NOT EXISTS energy_measure (
        scope_id String, publication_id String, cycle_id String, compartment_id String, resource_date Date32,
        component LowCardinality(String), measure_code LowCardinality(String), source_unit String,
        revision_id String, logical_record_key String, correction_id String, correction_version UInt32,
        source_exact Nullable(String), source_decimal Nullable(Decimal(76,38)), projection_status LowCardinality(String),
        quality LowCardinality(String), applicability LowCardinality(String), reason Nullable(String),
        basis_version_id Nullable(String), normalized_kwh_m2 Nullable(String), absolute_source_quantity Nullable(String),
        absolute_kwh Nullable(String)
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,cycle_id,resource_date,component)''',
)


def migrate(client,coordinator):
    with writer_lock(coordinator,client):
        for sql in DDL: execute_write(client,coordinator,sql)


def validated_config(config,scope):
    threshold_config(config)
    bases=[from_dict(EffectiveAreaBasis,b) for b in config['bases']]
    ids=set();compartments={c['compartment_id'] for c in config['cycles']}
    for b in bases:
        if b.scope_id!=scope or b.compartment_id not in compartments or b.measure_code not in (*CODES.values(),'ProdA','ProdB') or b.basis_version_id in ids:
            raise ValueError('Energy basis identity is ambiguous or outside scope/measure')
        ids.add(b.basis_version_id)
        if any(other is not b and (other.compartment_id,other.measure_code)==(b.compartment_id,b.measure_code)
               and max(b.valid_from,other.valid_from)<min(b.valid_to_exclusive,other.valid_to_exclusive) for other in bases):
            raise ValueError('Overlapping energy basis selection')
    return config


class EnergyMart:
    def __init__(self,client,coordinator,config_version=None):
        self.client=client;self.coordinator=coordinator;self.config_version=config_version;self.repo=Repository(client,coordinator)

    def rows(self,table,scope,pub):
        if table not in ('energy_header','energy_cycle','energy_basis','energy_measure'): raise ValueError('Unknown energy table')
        return self.client.query('SELECT DISTINCT * FROM '+table+' WHERE scope_id={scope:String} AND publication_id={pub:String}',{'scope':scope,'pub':pub})

    def config(self,scope,version):
        pins=[r for r in self.repo.configurations()['rows'] if (r['kind'],r['scope_id'],r['version_id'])==('energy',scope,version)]
        if len(pins)!=1: raise ValueError('Missing energy configuration')
        return validated_config(json.loads(pins[0]['payload']),scope),pins[0]

    def expected(self,scope,pub,core,version):
        config,pin=self.config(scope,version)
        cycles=[dict(scope_id=scope,publication_id=pub,**c) for c in config['cycles']]
        mapping={c['compartment_id']:c for c in cycles};basis_rows=[];bases=[from_dict(EffectiveAreaBasis,b) for b in config['bases']]
        for b in bases:
            exact,projection,_=decimal_projection(b.area_m2)
            basis_rows.append(dict(scope_id=scope,publication_id=pub,compartment_id=b.compartment_id,measure_code=b.measure_code,
                basis_version_id=b.basis_version_id,area_exact=exact,area_decimal=projection,status=b.status.value,
                valid_from=b.valid_from.isoformat(),valid_to_exclusive=b.valid_to_exclusive.isoformat(),payload=canonical_json(to_dict(b))))
        # A shared basis identity must mean the same thing in the frozen BR05 config.
        from greenhouse.clickhouse.harvest import HarvestMart
        harvest_headers=HarvestMart(self.client,None).rows('harvest_header',scope,pub)
        if len(harvest_headers)!=1: raise ValueError('Energy requires a frozen harvest bundle')
        harvest,_=HarvestMart(self.client,None).config(scope,harvest_headers[0]['config_version'])
        if harvest['cycles']!=config['cycles'] or harvest['snapshot_id']!=config['snapshot_id']:
            raise ValueError('Energy/harvest scope or cycle mapping differs')
        for selected in harvest['bases']:
            meta=selected['metadata']
            matched=[b for b in bases if b.basis_version_id==meta.get('basis_version_id')]
            if selected['measure_code']=='ProdA' and meta.get('basis_version_id') and not matched:
                raise ValueError('Energy configuration lacks frozen Production basis provenance')
            for b in matched:
                if (b.compartment_id,b.measure_code,b.area_m2,b.basis_kind,b.status.value,b.assumption_id,b.assumption_version,b.valid_from.isoformat(),b.valid_to_exclusive.isoformat())!=(selected['compartment_id'],selected['measure_code'],Decimal(meta['area_m2']),meta['basis_kind'],meta['basis_status'],meta.get('assumption_id'),meta.get('assumption_version'),selected['start'],selected['end']):
                    raise ValueError('Shared harvest/energy basis identity has different content')
        measures=[]
        for rev in core:
            if rev['snapshot_id']!=config['snapshot_id'] or rev['quality_policy_version']!=config['quality_policy_version']:
                raise ValueError('Energy source snapshot/policy mismatch')
            if rev['record_type']!='Resources': continue
            cycle=mapping.get(rev['compartment_id']);record=json.loads(rev['payload']);when=record['source_timestamp'][:10]
            if cycle is None or not cycle['start']<=when<cycle['end'] or record['source_timestamp'][10:]!='T00:00:00':
                raise ValueError('Energy source outside daily cycle')
            for component,code in CODES.items():
                value,quality,applicability,reason=measure_verdict(config['quality_definition'],record,code)
                accepted=quality=='accepted' and applicability=='applicable'
                if accepted and value<0:
                    raise ValueError('Energy contract refuses negative accepted quantity; supply audited quality/correction')
                chosen=[b for b in bases if (b.compartment_id,b.measure_code)==(cycle['compartment_id'],code)
                    and b.valid_from<=date.fromisoformat(when)<b.valid_to_exclusive]
                basis=chosen[0] if chosen else None
                absolute=exact_product(value,basis.area_m2) if accepted and basis else None
                exact,projection,status=decimal_projection(value)
                encode=lambda x:decimal_text(x) if x is not None else None
                measures.append(dict(scope_id=scope,publication_id=pub,cycle_id=cycle['cycle_id'],compartment_id=cycle['compartment_id'],
                    resource_date=when,component=component,measure_code=code,source_unit='MJ/m²/day' if component=='heat' else 'kWh/m²/day',
                    revision_id=rev['revision_id'],logical_record_key=cycle['cycle_id']+':'+rev['logical_key'],correction_id=rev['correction_id'],
                    correction_version=rev['revision']-1,source_exact=exact,source_decimal=projection,projection_status=status,
                    quality=quality,applicability=applicability,reason=reason,basis_version_id=basis.basis_version_id if basis else None,
                    normalized_kwh_m2=encode(energy_kwh(value,EnergyComponent(component))) if accepted else None,
                    absolute_source_quantity=encode(absolute),absolute_kwh=encode(energy_kwh(absolute,EnergyComponent(component)))))
        return dict(scope_id=scope,publication_id=pub,config_version=version),cycles,basis_rows,measures,pin

    def build(self,scope,pub,core):
        if self.config_version is None: raise ValueError('Explicit energy configuration required')
        header,cycles,bases,measures,_=self.expected(scope,pub,core,self.config_version)
        for table,rows in (('energy_cycle',cycles),('energy_basis',bases),('energy_measure',measures),('energy_header',[header])):
            for offset in range(0,len(rows),4096): self.repo._insert(table,rows[offset:offset+4096])

    def verify(self,scope,pub,core):
        headers=self.rows('energy_header',scope,pub)
        if len(headers)!=1: raise ValueError('Missing/conflicting energy header')
        header,cycles,bases,measures,pin=self.expected(scope,pub,core,headers[0]['config_version'])
        def normalize(rows):
            result=[]
            for row in rows:
                row=dict(row)
                for field in ('area_decimal','source_decimal'):
                    if row.get(field) is not None:row[field]=decimal_text(row[field])
                result.append(canonical_json(row))
            return sorted(result)
        if headers!=[header] or any(normalize(actual)!=normalize(expected) for actual,expected in
            ((self.rows('energy_cycle',scope,pub),cycles),(self.rows('energy_basis',scope,pub),bases),(self.rows('energy_measure',scope,pub),measures))):
            raise ValueError('Energy mart differs from pinned source/basis')
        return {'version':BUNDLE,'quantity_version':QUANTITY_VERSION,'rows':len(measures),'cycles':len(cycles),
            'checksum':digest([header,normalize(cycles),normalize(bases),normalize(measures)]),
            'configuration_ref':{'kind':'energy','version':pin['version_id'],'hash':pin['content_hash']}}


class EnergyAdapter(WaterAdapter):
    LINEAGE_TABLE='energy_measure';LINEAGE_TYPE='resource'
    LINEAGE_UNITS=(('Heat_cons','MJ/m²/day'),('ElecHigh','kWh/m²/day'),('ElecLow','kWh/m²/day'))

    def __init__(self,client,*,raw_snapshots=None):
        super().__init__(client,raw_snapshots=raw_snapshots);self.mart=EnergyMart(client,None)

    def publication(self,publication_id):
        from greenhouse.clickhouse.registry import reference
        envelope=self._envelope(publication_id)
        receipt=envelope['receipts'].get('energy',{})
        if (receipt.get('version'),receipt.get('quantity_version'))!=(BUNDLE,QUANTITY_VERSION):
            raise ValueError('Publication lacks expected energy quantities')
        return reference(self.client,publication_id,envelope)

    def catalog_cycles(self,publication_id):
        pub=self.publication(publication_id)
        return tuple(CatalogCycle(r['cycle_id'],r['compartment_id'],r['compartment_name'],date.fromisoformat(r['start']),date.fromisoformat(r['end']))
            for r in sorted(self.mart.rows('energy_cycle',pub.scope_id,publication_id),key=lambda r:r['cycle_id']))

    def _bases(self,context):
        pub=self.publication(context.publication_id);context.matches(pub)
        rows=self.mart.rows('energy_basis',context.scope_id,context.publication_id)
        return {r['basis_version_id']:from_dict(EffectiveAreaBasis,json.loads(r['payload'])) for r in rows if r['compartment_id']==context.compartment_ids[0]}

    def read_production_bases(self,context):
        return tuple(b for b in self._bases(context).values() if b.measure_code=='ProdA')

    def read_energy_quantities(self,context,publication):
        context.matches(publication)
        if self.publication(publication.publication_id)!=publication: raise ValueError('Energy pin differs')
        self.cycle_window(context);bases=self._bases(context)
        rows=self.client.query('''SELECT DISTINCT * FROM energy_measure WHERE scope_id={scope:String} AND publication_id={pub:String}
            AND cycle_id={cycle:String} AND compartment_id={comp:String} AND resource_date>={start:Date32} AND resource_date<{end:Date32}
            ORDER BY resource_date,component''',{'scope':context.scope_id,'pub':context.publication_id,'cycle':context.cycle_id,
                'comp':context.compartment_ids[0],'start':context.start,'end':context.end})
        result=[]
        for r in rows:
            dec=lambda field:Decimal(r[field]) if r[field] is not None else None
            result.append(EnergyQuantity(publication,context.cycle_id,context.compartment_ids[0],date.fromisoformat(r['resource_date']),
                EnergyComponent(r['component']),dec('source_exact'),r['source_unit'],QualityStatus(r['quality']),Applicability(r['applicability']),r['reason'],
                r['revision_id'],r['logical_record_key'],CorrectionRef(r['correction_id'],r['correction_version']) if r['correction_id'] else None,
                bases.get(r['basis_version_id']),dec('normalized_kwh_m2'),dec('absolute_source_quantity'),dec('absolute_kwh')))
        return tuple(result)
