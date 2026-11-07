"""Frozen climate/drain slot inputs; the shared duration engine owns episodes.

Keep the whole cycle in the mart, including its close-boundary sample. Reads
bound source-naive integer clocks before fetching. Values retain exact Decimal
text and a lossless typed projection; quality never means a business breach.
"""
from datetime import date, datetime
from decimal import Decimal
import json

from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.encoding import canonical_json, decimal_projection, decimal_text, digest, source_clock_us
from greenhouse.clickhouse.journal import execute_write
from greenhouse.clickhouse.repository import Repository
from greenhouse.clickhouse.quality import AGC_QUALITY_DEFINITION, measure_verdict
from greenhouse.clickhouse.water import WaterAdapter
from greenhouse.contracts import CatalogCycle, CycleWindow
from greenhouse.queries.slot_rows import ClimateSlotRow, DrainSlotRow

CLIMATE={'Tair':'tair','CO2air':'co2air','t_heat_sp':'t_heat_sp','t_vent_sp':'t_vent_sp',
         'co2_sp':'co2_sp','t_heat_vip':'t_heat_vip','t_ventlee_vip':'t_ventlee_vip','co2_vip':'co2_vip'}
DRAIN={'EC_drain_PC':'ec_drain','pH_drain_PC':'ph_drain'}
MEASURES={'climate':CLIMATE,'drain':DRAIN}
VERSIONS={'climate':'climate-mart-v1','drain':'drain-mart-v1'}
DDL=(
    '''CREATE TABLE IF NOT EXISTS threshold_header (
        scope_id String, publication_id String, bundle LowCardinality(String), config_version String
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,bundle)''',
    '''CREATE TABLE IF NOT EXISTS threshold_cycle (
        scope_id String, publication_id String, bundle LowCardinality(String),
        cycle_id String, compartment_id String, compartment_name String, start Date32, end Date32
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,bundle,cycle_id)''',
    '''CREATE TABLE IF NOT EXISTS threshold_measure (
        scope_id String, publication_id String, bundle LowCardinality(String),
        cycle_id String, compartment_id String, slot_clock_us Int64, slot_start String,
        revision_id String, logical_record_key String, quality_policy_version String,
        correction_id String, correction_version UInt32, measure_code LowCardinality(String),
        value_exact Nullable(String), value_decimal Nullable(Decimal(76,38)),
        projection_status LowCardinality(String), quality LowCardinality(String),
        applicability LowCardinality(String), reason Nullable(String)
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,bundle,cycle_id,slot_clock_us,measure_code)''',
)


def migrate(client,coordinator):
    with writer_lock(coordinator,client):
        for sql in DDL:
            execute_write(client,coordinator,sql)


def validated_config(config):
    if config['quality_policy_version']!='AGC_QUALITY_V1' or config['parser_version']!='agc-parser-v1':
        raise ValueError('Unsupported threshold semantic configuration')
    if ({'missing':'per_measure','out_of_target_is_quality':False}|config['quality_definition'])!=AGC_QUALITY_DEFINITION:
        raise ValueError('Threshold quality definition drift')
    if config.get('storage_version','threshold-list-v1') not in ('threshold-list-v1','threshold-stream-v2'):
        raise ValueError('Unsupported threshold storage version')
    selection=config.get('climate_window')
    if selection is not None:
        if not isinstance(selection,list) or len(selection)!=2:
            raise ValueError('Threshold source window requires two explicit ISO bounds')
        lo,hi=map(datetime.fromisoformat,selection)
        if lo.tzinfo is not None or hi.tzinfo is not None or lo>=hi or [lo.isoformat(),hi.isoformat()]!=selection:
            raise ValueError('Threshold source window must be naive and half-open')
    ids=set(); comps=set()
    for c in config['cycles']:
        CatalogCycle(c['cycle_id'],c['compartment_id'],c['compartment_name'],date.fromisoformat(c['start']),date.fromisoformat(c['end']))
        if c['cycle_id'] in ids or c['compartment_id'] in comps:
            raise ValueError('Ambiguous threshold cycle mapping')
        ids.add(c['cycle_id']); comps.add(c['compartment_id'])
    if not ids:
        raise ValueError('Threshold configuration requires cycles')
    return config


class SlotMart:
    def __init__(self,client,coordinator,bundle,config_version=None):
        if bundle not in MEASURES:
            raise ValueError('Unknown slot bundle')
        self.client=client; self.coordinator=coordinator; self.bundle=bundle
        self.config_version=config_version; self.repo=Repository(client,coordinator)

    def rows(self,table,scope,pub):
        if table not in ('threshold_header','threshold_cycle','threshold_measure'):
            raise ValueError('Unknown slot table')
        return self.client.query('SELECT DISTINCT * FROM '+table+' WHERE scope_id={scope:String} AND publication_id={pub:String} AND bundle={bundle:String}',
            {'scope':scope,'pub':pub,'bundle':self.bundle})

    def config(self,scope,version):
        pins=[r for r in self.repo.configurations()['rows'] if (r['kind'],r['scope_id'],r['version_id'])==('threshold',scope,version)]
        if len(pins)!=1:
            raise ValueError('Missing threshold configuration')
        return validated_config(json.loads(pins[0]['payload'])),pins[0]

    def expected(self,scope,pub,core,version):
        config,pin=self.config(scope,version)
        cycles=[dict(scope_id=scope,publication_id=pub,bundle=self.bundle,**c) for c in config['cycles']]
        rows=self._measure_rows(scope,pub,core,config,cycles)
        measures=rows if config.get('storage_version')=='threshold-stream-v2' else list(rows)
        return dict(scope_id=scope,publication_id=pub,bundle=self.bundle,config_version=version),cycles,measures,pin

    def _measure_rows(self,scope,pub,core,config,cycles):
        mapping={c['compartment_id']:c for c in cycles}; seen=set();last=None
        streaming=config.get('storage_version')=='threshold-stream-v2'
        for revision in core:
            if revision['snapshot_id']!=config['snapshot_id'] or revision['quality_policy_version']!=config['quality_policy_version']:
                raise ValueError('Threshold source snapshot/policy mismatch')
            if revision['record_type']!='GreenhouseClimate':
                continue
            cycle=mapping.get(revision['compartment_id'])
            if cycle is None:
                raise ValueError('Threshold source lacks cycle mapping')
            record=json.loads(revision['payload']); stamp=record['slot_start']
            when=datetime.fromisoformat(stamp)
            if config.get('climate_window') and not config['climate_window'][0]<=stamp<config['climate_window'][1]:
                raise ValueError('Threshold membership exceeds its explicit source selection')
            if not datetime.fromisoformat(cycle['start'])<=when<=datetime.fromisoformat(cycle['end']):
                raise ValueError('Threshold source outside cycle boundary')
            if when.second or when.microsecond or when.minute%5:
                raise ValueError('Threshold source is not on the five-minute grid')
            key=(cycle['compartment_id'],stamp)
            if streaming:
                if last is not None and key<=last:raise ValueError('Duplicate or unordered threshold source slot')
                last=key
            else:
                if key in seen:raise ValueError('Duplicate threshold slot')
                seen.add(key)
            for code in sorted(MEASURES[self.bundle]):
                value,quality,applicability,reason=measure_verdict(config['quality_definition'],record,code)
                exact,projection,status=decimal_projection(value)
                yield dict(scope_id=scope,publication_id=pub,bundle=self.bundle,
                    cycle_id=cycle['cycle_id'],compartment_id=cycle['compartment_id'],slot_clock_us=source_clock_us(when),
                    slot_start=stamp,revision_id=revision['revision_id'],logical_record_key=cycle['cycle_id']+':'+revision['logical_key'],
                    quality_policy_version=config['quality_policy_version'],correction_id=revision['correction_id'],
                    correction_version=revision['revision']-1,measure_code=code,value_exact=exact,value_decimal=projection,
                    projection_status=status,quality=quality,applicability=applicability,reason=reason)

    def build(self,scope,pub,core):
        if self.config_version is None:
            raise ValueError('Explicit threshold configuration required')
        header,cycles,measures,_=self.expected(scope,pub,core,self.config_version)
        self.repo._insert('threshold_cycle',cycles)
        batch=[]
        for row in measures:
            batch.append(row)
            if len(batch)>=4096:
                self.repo._insert('threshold_measure',batch);batch.clear()
        if batch:self.repo._insert('threshold_measure',batch)
        self.repo._insert('threshold_header',[header])

    def verify(self,scope,pub,core):
        headers=self.rows('threshold_header',scope,pub)
        if len(headers)!=1:
            raise ValueError('Missing/conflicting threshold header')
        header,cycles,measures,pin=self.expected(scope,pub,core,headers[0]['config_version'])
        if json.loads(pin['payload']).get('storage_version')=='threshold-stream-v2':
            return self._verify_stream(scope,pub,header,cycles,measures,pin,headers)
        def normalize(rows):
            return sorted(canonical_json(r|({'value_decimal':decimal_text(r['value_decimal']) if r['value_decimal'] is not None else None,
                'slot_clock_us':int(r['slot_clock_us'])} if 'value_decimal' in r else {})) for r in rows)
        if headers!=[header] or normalize(cycles)!=normalize(self.rows('threshold_cycle',scope,pub)) or normalize(measures)!=normalize(self.rows('threshold_measure',scope,pub)):
            raise ValueError('Threshold mart differs from canonical source/configuration')
        return {'version':VERSIONS[self.bundle],'rows':len(measures)//len(MEASURES[self.bundle]),'cycles':len(cycles),
            'checksum':digest([header,normalize(cycles),normalize(measures)]),
            'configuration_ref':{'kind':'threshold','version':pin['version_id'],'hash':pin['content_hash']}}


    def _verify_stream(self,scope,pub,header,cycles,expected,pin,headers):
        from contextlib import closing
        import hashlib
        from itertools import zip_longest
        normalize=lambda r:canonical_json(r|({'value_decimal':decimal_text(r['value_decimal']) if r['value_decimal'] is not None else None,
            'slot_clock_us':int(r['slot_clock_us'])} if 'value_decimal' in r else {}))
        normalized_cycles=sorted(map(normalize,cycles))
        if headers!=[header] or normalized_cycles!=sorted(map(normalize,self.rows('threshold_cycle',scope,pub))):
            raise ValueError('Threshold header/cycles differ from canonical configuration')
        sql='SELECT * FROM threshold_measure WHERE scope_id={scope:String} AND publication_id={pub:String} AND bundle={bundle:String} ORDER BY compartment_id,slot_clock_us,measure_code SETTINGS max_bytes_before_external_sort=67108864'
        checksum=hashlib.sha256();count=0;missing=object()
        def logical_rows(rows):
            previous=None
            for row in rows:
                if row!=previous:yield row
                previous=row
        with closing(self.client.iter_query(sql,{'scope':scope,'pub':pub,'bundle':self.bundle})) as actual:
            for wanted,stored in zip_longest(expected,logical_rows(actual),fillvalue=missing):
                if wanted is missing or stored is missing:raise ValueError('Threshold mart measure count differs from canonical source')
                encoded=normalize(wanted)
                if encoded!=normalize(stored):raise ValueError('Threshold mart differs from canonical source/configuration')
                checksum.update(encoded.encode()+b'\n');count+=1
        algorithm='ordered-jsonl-sha256-v2'
        return {'version':VERSIONS[self.bundle],'rows':count//len(MEASURES[self.bundle]),'cycles':len(cycles),
            'storage_version':'threshold-stream-v2','checksum_algorithm':algorithm,
            'checksum':digest({'algorithm':algorithm,'header':header,'cycles':normalized_cycles,'measures':checksum.hexdigest(),'count':count}),
            'configuration_ref':{'kind':'threshold','version':pin['version_id'],'hash':pin['content_hash']}}


class SlotAdapter(WaterAdapter):
    LINEAGE_TABLE='threshold_measure'

    def __init__(self,client,*,raw_snapshots=None):
        super().__init__(client,raw_snapshots=raw_snapshots)
        self.mart=SlotMart(client,None,self.BUNDLE)

    @property
    def database_key(self):
        s=self.client.settings
        return ('clickhouse',s.host,str(s.port),s.database,s.user)

    def publication(self,publication_id):
        from greenhouse.clickhouse.registry import reference
        envelope=self._envelope(publication_id)
        if self.BUNDLE not in envelope['receipts']:
            raise ValueError('Publication lacks '+self.BUNDLE+' mart')
        return reference(self.client,publication_id,envelope)

    def catalog_cycles(self,publication_id):
        ref=self.publication(publication_id)
        return tuple(CatalogCycle(r['cycle_id'],r['compartment_id'],r['compartment_name'],date.fromisoformat(r['start']),date.fromisoformat(r['end']))
            for r in sorted(self.mart.rows('threshold_cycle',ref.scope_id,publication_id),key=lambda r:r['cycle_id']))

    def schedule(self,rule_version):
        from greenhouse.clickhouse.schedules import read_rule_schedule
        revision=read_rule_schedule(self.client,rule_version)
        if revision is None:
            raise ValueError('Unknown schedule revision')
        return revision

    def _read(self,context,publication,window_start,window_end):
        context.matches(publication)
        if self.publication(publication.publication_id)!=publication:
            raise ValueError('Pinned threshold metadata differs')
        cycle=self.cycle_window(context)
        start=max(datetime.combine(cycle.start,datetime.min.time()),window_start or datetime.min)
        end=min(datetime.combine(cycle.end,datetime.min.time()),window_end or datetime.max)
        from contextlib import closing
        sql='''SELECT * FROM threshold_measure WHERE scope_id={scope:String}
            AND publication_id={pub:String} AND bundle={bundle:String} AND cycle_id={cycle:String}
            AND compartment_id={comp:String} AND slot_clock_us>={start:Int64} AND slot_clock_us<{end:Int64}
            ORDER BY slot_clock_us,measure_code SETTINGS max_bytes_before_external_sort=67108864'''
        parameters={'scope':context.scope_id,'pub':publication.publication_id,'bundle':self.BUNDLE,
            'cycle':context.cycle_id,'comp':context.compartment_ids[0],
            'start':source_clock_us(start),'end':source_clock_us(end)}
        def assemble(stamp,measures):
            if set(measures)!=set(MEASURES[self.BUNDLE]):
                raise ValueError('Incomplete threshold measure verdicts')
            first=next(iter(measures.values()))
            keys=('revision_id','logical_record_key','quality_policy_version','correction_id','correction_version')
            if any(any(r[k]!=first[k] for k in keys) for r in measures.values()):
                raise ValueError('Mixed threshold revisions')
            values={field:Decimal(measures[code]['value_exact']) if measures[code]['value_exact'] is not None else None
                    for code,field in MEASURES[self.BUNDLE].items()}
            quality={code:{'status':r['quality'],'applicability':r['applicability'],'reason':r['reason']} for code,r in measures.items()}
            row_type=ClimateSlotRow if self.BUNDLE=='climate' else DrainSlotRow
            return row_type(slot_start=datetime.fromisoformat(stamp),revision_id=first['revision_id'],
                logical_key=first['logical_record_key'],quality_policy_version=first['quality_policy_version'],
                correction_id=first['correction_id'] or None,correction_version=first['correction_version'] or None,
                measure_quality=quality,**values)
        result=[];stamp=None;measures={}
        with closing(self.client.iter_query(sql,parameters)) as rows:
            for row in rows:
                if stamp is not None and row['slot_start']!=stamp:
                    result.append(assemble(stamp,measures));measures={}
                stamp=row['slot_start'];code=row['measure_code']
                if code in measures and measures[code]!=row:raise ValueError('Conflicting threshold slot measure')
                measures[code]=row
            if stamp is not None:result.append(assemble(stamp,measures))
        return tuple(result)


class ClimateAdapter(SlotAdapter):
    BUNDLE='climate'
    LINEAGE_TYPE='climate'
    LINEAGE_UNITS=tuple((code,'ppm' if code in ('CO2air','co2_sp','co2_vip') else '°C') for code in sorted(CLIMATE))

    def read_climate_slots(self,context,publication,*,window_start=None,window_end=None):
        return self._read(context,publication,window_start,window_end)


class DrainAdapter(SlotAdapter):
    BUNDLE='drain'
    LINEAGE_TYPE='drain'
    LINEAGE_UNITS=(('EC_drain_PC','dS/m'),('pH_drain_PC','1'))

    def read_drain_slots(self,context,publication,*,window_start=None,window_end=None):
        return self._read(context,publication,window_start,window_end)
