"""Typed BR-02 mart, sealed bundle verification and unchanged query contracts."""
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path

from greenhouse.clickhouse.encoding import canonical_json, decimal_projection, decimal_text, digest
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.quality import measure_verdict, AGC_QUALITY_DEFINITION
from greenhouse.clickhouse.publication import Publications
from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.journal import execute_write
from greenhouse.contracts import (
    Applicability, BasisStatus, CatalogCycle, CatalogPublication, CycleWindow,
    IrrigationDay, IrrigationRuleProfile, MeasureValue, PublicationRef, QualityStatus,
    Versions, WATER_UNIT, RESOURCE_WATER_UNIT, LineageResult, MeasureUnit, from_dict,
)

BUNDLE='irrigation-mart-v1'
CODE_VERSION='clickhouse-water-v1'
DDL=(
    '''CREATE TABLE IF NOT EXISTS water_header (
        scope_id String, publication_id String, config_version String, snapshot_id String
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id)''',
    '''CREATE TABLE IF NOT EXISTS water_cycle (
        scope_id String, publication_id String, cycle_id String, compartment_id String,
        compartment_name String, start Date32, end Date32
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,cycle_id)''',
    '''CREATE TABLE IF NOT EXISTS water_measure (
        scope_id String, publication_id String, cycle_id String, compartment_id String,
        resource_date Date32, measure_code LowCardinality(String), revision_id String,
        logical_record_key String, correction_id String, correction_version UInt32,
        value_exact Nullable(String), value_decimal Nullable(Decimal(76,38)),
        projection_status LowCardinality(String), quality LowCardinality(String),
        applicability LowCardinality(String), reason Nullable(String), source_unit String,
        basis_metadata String
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,cycle_id,resource_date,measure_code)''',
)


def migrate(client, coordinator):
    with writer_lock(coordinator,client):
        for sql in DDL:
            execute_write(client,coordinator,sql)


def validated_config(payload):
    if payload.get('quality_policy_version')!='AGC_QUALITY_V1':
        raise ValueError('Unsupported quality policy')
    policy={'missing':'per_measure','out_of_target_is_quality':False} | payload.get('quality_definition',{})
    if policy!=AGC_QUALITY_DEFINITION:
        raise ValueError('AGC_QUALITY_V1 definition drift')
    if payload.get('source_unit')!=RESOURCE_WATER_UNIT:
        raise ValueError('Unexpected water source unit')
    ids=set(); compartments=set()
    for cycle in payload['cycles']:
        typed=CatalogCycle(cycle['cycle_id'],cycle['compartment_id'],cycle['compartment_name'],
            date.fromisoformat(cycle['start']),date.fromisoformat(cycle['end']))
        if typed.cycle_id in ids or typed.compartment_id in compartments:
            raise ValueError('Repeated cycle or ambiguous compartment mapping')
        ids.add(typed.cycle_id); compartments.add(typed.compartment_id)
    if not ids:
        raise ValueError('Water configuration requires explicit cycle mapping')
    for basis in payload['bases']:
        if basis['compartment_id'] not in compartments or basis['measure_code'] not in ('Irr','Drain'):
            raise ValueError('Basis reference outside scope')
        if date.fromisoformat(basis['start'])>=date.fromisoformat(basis['end']):
            raise ValueError('Invalid basis window')
        decode_measure('0','accepted','applicable',None,basis['metadata'])
    for left in payload['bases']:
        for right in payload['bases']:
            if left is right:
                continue
            if (left['compartment_id'],left['measure_code'])==(right['compartment_id'],right['measure_code']) and max(left['start'],right['start'])<min(left['end'],right['end']):
                raise ValueError('Overlapping selected water bases')
    return payload


def decode_measure(value, quality, applicability, reason, basis):
    return from_dict(MeasureValue,dict(value=value,unit=WATER_UNIT,quality=quality,
        applicability=applicability,reason_code=reason,source_unit=RESOURCE_WATER_UNIT,
        basis_status='unknown',basis_version_id=None,area_m2=None,basis_kind=None,
        assumption_id=None,assumption_version=None,basis_label=None) | basis)


class WaterMart:
    def __init__(self,client,coordinator,config_version=None):
        self.client=client; self.coordinator=coordinator; self.config_version=config_version
        self.repo=Repository(client,coordinator)

    def config(self,scope,version):
        rows=[r for r in self.repo.configurations()['rows']
              if (r['kind'],r['scope_id'],r['version_id'])==('water',scope,version)]
        if len(rows)!=1:
            raise ValueError('Missing water configuration version')
        return validated_config(json.loads(rows[0]['payload'])),rows[0]

    def expected(self,scope,publication,core,version):
        config,envelope=self.config(scope,version)
        cycles=[dict(scope_id=scope,publication_id=publication,**c) for c in config['cycles']]
        mapping={c['compartment_id']:c for c in cycles}
        measures=[]
        for revision in core:
            if revision['snapshot_id']!=config['snapshot_id'] or revision['quality_policy_version']!=config['quality_policy_version']:
                raise ValueError('Water source snapshot/policy mismatch')
            if revision['record_type']!='Resources':
                continue
            cycle=mapping.get(revision['compartment_id'])
            if cycle is None:
                raise ValueError('Resource source has no explicit cycle mapping')
            record=json.loads(revision['payload']); when=record['source_timestamp'][:10]
            if not cycle['start']<=when<cycle['end'] or record['source_timestamp'][10:]!='T00:00:00':
                raise ValueError('Resource source outside whole-day cycle')
            for code in ('Irr','Drain'):
                value,quality,applicability,reason=measure_verdict(config['quality_definition'],record,code)
                exact,projection,status=decimal_projection(value)
                bases=[b for b in config['bases'] if b['compartment_id']==cycle['compartment_id']
                    and b['measure_code']==code and b['start']<=when<b['end']]
                basis=bases[0]['metadata'] if bases else {}
                decode_measure(exact,quality,applicability,reason,basis)
                measures.append(dict(scope_id=scope,publication_id=publication,cycle_id=cycle['cycle_id'],
                    compartment_id=cycle['compartment_id'],resource_date=when,measure_code=code,
                    revision_id=revision['revision_id'],logical_record_key=cycle['cycle_id']+':'+revision['logical_key'],
                    correction_id=revision['correction_id'],correction_version=revision['revision']-1,
                    value_exact=exact,value_decimal=projection,projection_status=status,quality=quality,
                    applicability=applicability,reason=reason,source_unit=RESOURCE_WATER_UNIT,
                    basis_metadata=canonical_json(basis)))
        header=dict(scope_id=scope,publication_id=publication,config_version=version,snapshot_id=config['snapshot_id'])
        return header,cycles,measures,envelope

    def build(self,scope,publication,core):
        if self.config_version is None:
            raise ValueError('Explicit water configuration required for build')
        header,cycles,measures,_=self.expected(scope,publication,core,self.config_version)
        self.repo._insert('water_cycle',cycles)
        self.repo._insert('water_measure',measures)
        self.repo._insert('water_header',[header])

    def rows(self,table,scope,publication):
        if table not in ('water_header','water_cycle','water_measure'):
            raise ValueError('Unknown water table')
        return self.client.query('SELECT DISTINCT * FROM '+table+' WHERE scope_id={scope:String} AND publication_id={pub:String}',{'scope':scope,'pub':publication})

    def verify(self,scope,publication,core):
        headers=self.rows('water_header',scope,publication)
        if len(headers)!=1:
            raise ValueError('Water header absent or conflicting')
        header,cycles,measures,envelope=self.expected(scope,publication,core,headers[0]['config_version'])
        def normalize(rows):
            output=[]
            for row in rows:
                row=dict(row)
                if 'value_decimal' in row and row['value_decimal'] is not None:
                    row['value_decimal']=decimal_text(row['value_decimal'])
                output.append(canonical_json(row))
            return sorted(output)
        actual_cycles=self.rows('water_cycle',scope,publication)
        actual_measures=self.rows('water_measure',scope,publication)
        if headers!=[header] or normalize(actual_cycles)!=normalize(cycles) or normalize(actual_measures)!=normalize(measures):
            raise ValueError('Water bundle differs from canonical source/config')
        return {'version':BUNDLE,'rows':len(measures)//2,'cycles':len(cycles),
            'checksum':digest([header,normalize(cycles),normalize(measures)]),
            'configuration_ref':{'kind':'water','version':envelope['version_id'],'hash':envelope['content_hash']}}

    def publications(self):
        return Publications(self.client,self.coordinator,bundles={'water':self.verify})


class WaterAdapter:
    LINEAGE_TABLE='water_measure'
    LINEAGE_TYPE='resource'
    LINEAGE_UNITS=(('Irr',RESOURCE_WATER_UNIT),('Drain',RESOURCE_WATER_UNIT))
    def __init__(self,client,*,raw_snapshots=None):
        # Read methods do not acquire writer locks or require coordinator mounts.
        self.client=client; self.mart=WaterMart(client,None)
        self.raw_snapshots={k:Path(v) for k,v in (raw_snapshots or {}).items()}
        self._pinned={}

    def _envelope(self,publication_id):
        if publication_id not in self._pinned:
            from greenhouse.clickhouse.registry import publication_envelope
            self._pinned[publication_id]=publication_envelope(self.client,publication_id)
        return self._pinned[publication_id]

    def publication(self,publication_id):
        from greenhouse.clickhouse.registry import reference
        envelope=self._envelope(publication_id)
        if 'water' not in envelope['receipts']:
            raise ValueError('Publication lacks irrigation mart bundle')
        return reference(self.client,publication_id,envelope)

    def pin_publication(self,scope_id):
        from greenhouse.clickhouse.registry import envelope
        pinned=envelope(self.client,scope_id)
        self._pinned[pinned['publication_id']]=pinned
        return self.publication(pinned['publication_id'])

    def catalog_cycles(self,publication_id):
        ref=self.publication(publication_id)
        return tuple(CatalogCycle(r['cycle_id'],r['compartment_id'],r['compartment_name'],
            date.fromisoformat(r['start']),date.fromisoformat(r['end']))
            for r in sorted(self.mart.rows('water_cycle',ref.scope_id,publication_id),key=lambda r:r['cycle_id']))

    def cycle_window(self,context):
        context.matches(self.publication(context.publication_id))
        cycles=[c for c in self.catalog_cycles(context.publication_id)
            if (c.cycle_id,c.compartment_id)==(context.cycle_id,context.compartment_ids[0])]
        if len(cycles)!=1:
            raise ValueError('Cycle/compartment absent from pinned publication')
        c=cycles[0]
        return CycleWindow(c.cycle_id,c.compartment_id,c.start,c.end)

    def read_irrigation_days(self,context,publication):
        context.matches(self.publication(context.publication_id))
        cycle=self.cycle_window(context)
        rows=self.client.query('''SELECT DISTINCT * FROM water_measure WHERE scope_id={scope:String}
            AND publication_id={pub:String} AND cycle_id={cycle:String} AND compartment_id={comp:String}
            AND resource_date>={start:Date32} AND resource_date<{end:Date32} ORDER BY resource_date,measure_code''',
            {'scope':context.scope_id,'pub':context.publication_id,'cycle':context.cycle_id,
             'comp':context.compartment_ids[0],'start':max(context.start,cycle.start),'end':min(context.end,cycle.end)})
        grouped={}
        for row in rows:
            values=grouped.setdefault(row['resource_date'],{})
            if row['measure_code'] in values or row['source_unit']!=RESOURCE_WATER_UNIT:
                raise ValueError('Water grain conflict or unsupported unit')
            values[row['measure_code']]=row
        result=[]
        for when,measures in sorted(grouped.items()):
            if set(measures)!= {'Irr','Drain'}:
                raise ValueError('Water day lacks measure metadata')
            left,right=measures['Irr'],measures['Drain']
            if any(left[k]!=right[k] for k in ('revision_id','logical_record_key','correction_id','correction_version')):
                raise ValueError('Mixed revision water day')
            decoded=[decode_measure(r['value_exact'],r['quality'],r['applicability'],r['reason'],json.loads(r['basis_metadata'])) for r in (left,right)]
            result.append(IrrigationDay(publication,context.cycle_id,context.compartment_ids[0],date.fromisoformat(when),
                *decoded,left['revision_id'],left['logical_record_key'],left['correction_id'] or None,
                left['correction_version'] or None))
        return tuple(result)

    def catalog_rules(self,scope_id,compartment_id):
        repo=Repository(self.client,None)
        return tuple(from_dict(IrrigationRuleProfile,json.loads(r['payload'])) for r in repo.configurations()['rows']
            if r['kind']=='irrigation_rule' and r['scope_id']==scope_id
            and json.loads(r['payload'])['compartment_id']==compartment_id)

    def irrigation_rule(self,context):
        if context.rule_version is None:
            return None
        rules=[r for r in self.catalog_rules(context.scope_id,context.compartment_ids[0]) if r.rule_version==context.rule_version]
        if len(rules)!=1:
            raise ValueError('Unknown irrigation rule in scope/compartment')
        rules[0].matches(context)
        return rules[0]

    def catalog_publications(self):
        scopes=self.client.query('SELECT DISTINCT h.scope_id FROM water_header h INNER JOIN publication_state p ON h.publication_id=p.publication_id AND h.scope_id=p.scope_id WHERE p.stage=\'COMPLETE\' ORDER BY h.scope_id')
        result=[]
        for scope in scopes:
            states=self.mart.publications()._states(scope['scope_id'])
            completed=[r for r in states if r['stage']=='COMPLETE']
            latest=max(completed,key=lambda r:int(r['generation']))
            candidate=json.loads(latest['payload'])
            if 'water' not in candidate['required_bundles']:
                snapshot_pins=[r for r in candidate['configuration']['pins'] if r['kind']=='snapshot']
                if len(snapshot_pins)!=1:
                    raise ValueError('Publication lacks unambiguous snapshot identity')
                result.append(CatalogPublication(scope['scope_id'],json.loads(snapshot_pins[0]['payload'])['snapshot_id'],
                    candidate['publication_id'],datetime.fromisoformat(candidate['committed_at']),'complete','clickhouse-core-v1'))
                continue
            from greenhouse.clickhouse.registry import envelope as read_envelope
            envelope=read_envelope(self.client,scope['scope_id'])
            ref=self.publication(envelope['publication_id']); receipt=envelope['receipts']['water']
            result.append(CatalogPublication(ref.scope_id,ref.snapshot_id,ref.publication_id,ref.committed_at,
                ref.finality_status,ref.versions.code_version,BUNDLE,receipt['rows'],receipt['cycles'],
                harvest_bundle_version=envelope['receipts'].get('harvest',{}).get('version'),
                climate_bundle_version=envelope['receipts'].get('climate',{}).get('version'),
                drain_bundle_version=envelope['receipts'].get('drain',{}).get('version'),
                energy_bundle_version=envelope['receipts'].get('energy',{}).get('version') if envelope['receipts'].get('energy',{}).get('quantity_version')=='energy-quantity-v1' else None))
        return tuple(result)


    def read_lineage(self,publication,*,revision_id=None,record_key=None):
        if (revision_id is None)==(record_key is None):
            raise ValueError('Select exactly one revision or record key')
        context=self._envelope(publication.publication_id)
        if self.publication(publication.publication_id)!=publication:
            raise ValueError('Publication metadata mismatch')
        rows=self.client.query('SELECT DISTINCT revision_id,cycle_id,compartment_id,logical_record_key FROM '+self.LINEAGE_TABLE+' WHERE publication_id={pub:String}',{'pub':publication.publication_id})
        rows=[r for r in rows if (r['revision_id']==revision_id if revision_id else r['logical_record_key']==record_key)]
        if len(rows)!=1:
            raise ValueError('Revision/key absent or ambiguous in pinned water membership')
        selected=rows[0]
        core=self.mart.repo.load_membership(publication.scope_id,context['load_id'])
        revision=next(r for r in core if r['revision_id']==selected['revision_id'])
        payload=json.loads(revision['payload'])
        raw=self.raw_snapshots.get(publication.snapshot_id)
        if raw is None:
            raise ValueError('Raw snapshot mount not configured')
        import hashlib
        from greenhouse.clickhouse.raw_identity import source_path
        target=source_path(raw,publication.snapshot_id,payload)
        with target.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=revision['file_checksum']:
                raise ValueError('Raw file checksum mismatch')
        with target.open(encoding='utf-8-sig',newline='') as stream:
            text=''.join(line for n,line in enumerate(stream,1) if int(revision['line_number'])<=n<=int(revision['line_end']))
        verified=hashlib.sha256(text.encode()).hexdigest()==revision['raw_hash']
        if not verified:
            raise ValueError('Raw record checksum mismatch')
        return LineageResult(publication,revision['revision_id'],self.LINEAGE_TYPE,
            tuple(MeasureUnit(*unit) for unit in self.LINEAGE_UNITS),
            selected['logical_record_key'],selected['cycle_id'],selected['compartment_id'],revision['revision'],
            revision['source_record_id'],revision['first_run_id'],revision['source_file'],revision['file_checksum'],
            payload['snapshot_sha'],int(revision['line_number']),int(revision['line_end']),payload['raw_reference'],
            revision['raw_hash'],True,Decimal(revision['source_serial']),datetime.fromisoformat(revision['source_timestamp']),
            revision['correction_id'] or None,revision['revision']-1 if revision['correction_id'] else None)


    def catalog_harvest_rules(self,scope_id,compartment_id):
        from greenhouse.contracts import HarvestRuleProfile
        rows=self.mart.repo.configurations()['rows']
        return tuple(from_dict(HarvestRuleProfile,json.loads(r['payload'])) for r in rows if
            r['kind']=='harvest_rule' and r['scope_id']==scope_id and json.loads(r['payload'])['compartment_id']==compartment_id)

    def catalog_schedule_rules(self,scope_id,compartment_id,*,measurement_context,metrics,bases):
        from greenhouse.clickhouse.schedules import catalog_rules
        return catalog_rules(self.client,scope_id,compartment_id,measurement_context=measurement_context,metrics=metrics,bases=bases)
