"""BR-05 typed record mart and frozen source completeness, no inferred harvests."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import json

from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.journal import execute_write
from greenhouse.clickhouse.encoding import canonical_json, decimal_projection, decimal_text, digest
from greenhouse.clickhouse.quality import measure_verdict, AGC_QUALITY_DEFINITION
from greenhouse.clickhouse.repository import Repository
from greenhouse.contracts import (
    CatalogCycle, CycleWindow, HarvestCompleteness, HarvestRecord, HarvestRuleProfile,
    HarvestUnresolvedIssue, MeasureValue, HARVEST_SOURCE_UNIT, HARVEST_UNIT, from_dict, to_dict,
)

BUNDLE='harvest-mart-v3'
DDL=(
    '''CREATE TABLE IF NOT EXISTS harvest_header (
        scope_id String, publication_id String, config_version String, load_id String
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id)''',
    '''CREATE TABLE IF NOT EXISTS harvest_cycle (
        scope_id String, publication_id String, cycle_id String, compartment_id String,
        compartment_name String, start Date32, end Date32
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,cycle_id)''',
    '''CREATE TABLE IF NOT EXISTS harvest_measure (
        scope_id String, publication_id String, cycle_id String, compartment_id String,
        harvest_date Date32, source_record_id String, revision_id String,
        logical_record_key String, correction_id String, correction_version UInt32,
        measure_code LowCardinality(String), value_exact Nullable(String),
        value_decimal Nullable(Decimal(76,38)), projection_status LowCardinality(String),
        quality LowCardinality(String), applicability LowCardinality(String),
        reason Nullable(String), source_unit String, basis_metadata String
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,cycle_id,harvest_date,source_record_id,measure_code)''',
    '''CREATE TABLE IF NOT EXISTS harvest_state (
        scope_id String, publication_id String, cycle_id String,
        snapshot_complete Bool, source_resolved Bool,
        accepted_records UInt64, paired_records UInt64, completeness String
    ) ENGINE=MergeTree ORDER BY (scope_id,publication_id,cycle_id)''',
)


def migrate(client,coordinator):
    with writer_lock(coordinator,client):
        for sql in DDL:
            execute_write(client,coordinator,sql)


def decode_measure(value,quality,applicability,reason,basis):
    return from_dict(MeasureValue,dict(value=value,unit=HARVEST_UNIT,quality=quality,
        applicability=applicability,reason_code=reason,source_unit=HARVEST_SOURCE_UNIT,
        basis_status='unknown',basis_version_id=None,area_m2=None,basis_kind=None,
        assumption_id=None,assumption_version=None,basis_label=None)|basis)


def validated_config(config):
    if config['source_unit']!=HARVEST_SOURCE_UNIT or config['quality_policy_version']!='AGC_QUALITY_V1':
        raise ValueError('Unsupported harvest unit/quality policy')
    if ({'missing':'per_measure','out_of_target_is_quality':False}|config['quality_definition'])!=AGC_QUALITY_DEFINITION:
        raise ValueError('Harvest quality definition drift')
    seen=set(); ids=set()
    for c in config['cycles']:
        CatalogCycle(c['cycle_id'],c['compartment_id'],c['compartment_name'],date.fromisoformat(c['start']),date.fromisoformat(c['end']))
        if c['compartment_id'] in seen or c['cycle_id'] in ids:
            raise ValueError('Ambiguous harvest cycle mapping')
        seen.add(c['compartment_id']); ids.add(c['cycle_id'])
    for basis in config['bases']:
        date.fromisoformat(basis['start']); date.fromisoformat(basis['end'])
        if basis['compartment_id'] not in seen or basis['measure_code'] not in ('ProdA','ProdB') or basis['start']>=basis['end']:
            raise ValueError('Invalid harvest basis reference/window')
        decode_measure('0','accepted','applicable',None,basis['metadata'])
        for other in config['bases']:
            if other is not basis and (other['compartment_id'],other['measure_code'])==(basis['compartment_id'],basis['measure_code']) and max(other['start'],basis['start'])<min(other['end'],basis['end']):
                raise ValueError('Overlapping selected harvest basis')
    return config


class HarvestMart:
    def __init__(self,client,coordinator,config_version=None,load_id=None):
        self.client=client; self.coordinator=coordinator
        self.config_version=config_version; self.load_id=load_id
        self.repo=Repository(client,coordinator)

    def rows(self,table,scope,publication):
        if table not in ('harvest_header','harvest_cycle','harvest_measure','harvest_state'):
            raise ValueError('Unknown harvest table')
        return self.client.query('SELECT DISTINCT * FROM '+table+' WHERE scope_id={scope:String} AND publication_id={pub:String}',{'scope':scope,'pub':publication})

    def config(self,scope,version):
        rows=[r for r in self.repo.configurations()['rows'] if (r['kind'],r['scope_id'],r['version_id'])==('harvest',scope,version)]
        if len(rows)!=1:
            raise ValueError('Missing harvest config')
        return validated_config(json.loads(rows[0]['payload'])),rows[0]

    def expected(self,scope,publication,core,version,load):
        config,envelope=self.config(scope,version)
        audit=self.repo.load_audit(scope,load)
        files={r['source_file']:json.loads(r['metadata']) for r in audit['files']}
        cycles=[dict(scope_id=scope,publication_id=publication,**c) for c in config['cycles']]
        mapping={c['compartment_id']:c for c in cycles}
        measures=[]
        paired={}; usable={}; source_revision={}
        for revision in core:
            if revision['record_type']!='Production':
                continue
            if revision['snapshot_id']!=config['snapshot_id'] or revision['quality_policy_version']!=config['quality_policy_version']:
                raise ValueError('Harvest snapshot/policy mismatch')
            cycle=mapping.get(revision['compartment_id'])
            if cycle is None:
                raise ValueError('Unmapped harvest source')
            record=json.loads(revision['payload']); when=record['source_timestamp'][:10]
            if not cycle['start']<=when<cycle['end']:
                raise ValueError('Harvest outside selected cycle')
            accepted={}
            for code in ('ProdA','ProdB'):
                value,quality,applicability,reason=measure_verdict(config['quality_definition'],record,code)
                exact,projection,status=decimal_projection(value)
                bases=[b for b in config['bases'] if b['compartment_id']==cycle['compartment_id'] and b['measure_code']==code and b['start']<=when<b['end']]
                basis=bases[0]['metadata'] if bases else {}
                accepted[code]=decode_measure(exact,quality,applicability,reason,basis).accepted
                measures.append(dict(scope_id=scope,publication_id=publication,cycle_id=cycle['cycle_id'],
                    compartment_id=cycle['compartment_id'],harvest_date=when,source_record_id=revision['source_record_id'],
                    revision_id=revision['revision_id'],logical_record_key=cycle['cycle_id']+':'+revision['source_record_id'],
                    correction_id=revision['correction_id'],correction_version=revision['revision']-1,
                    measure_code=code,value_exact=exact,value_decimal=projection,projection_status=status,
                    quality=quality,applicability=applicability,reason=reason,source_unit=HARVEST_SOURCE_UNIT,basis_metadata=canonical_json(basis)))
            source_revision[revision['source_record_id']]=(cycle,when,accepted)
        states=[]
        for cycle in cycles:
            path=cycle['compartment_id']+'/Production.csv'; file=files.get(path)
            loaded=file is not None and file['status'] in ('loaded','loaded_with_quarantine') and file.get('outputs_consumable',True) and file.get('core_selected',False)
            unresolved={}
            for entry in audit['issues']:
                if entry['source_file']!=path or entry['disposition'] not in ('quarantine','conflict'):
                    continue
                record=json.loads(entry['payload'])
                for issue in record['issues']:
                    code=issue['code']; column=issue.get('column')
                    selected=source_revision.get(entry['source_record_id'])
                    resolved=entry['disposition']=='quarantine' and code=='invalid_numeric' and column in ('ProdA','ProdB') and selected is not None and selected[2][column]
                    if resolved:
                        continue
                    item=HarvestUnresolvedIssue(entry['source_record_id'],entry['disposition'],code,column,
                        path,record['line_number'],Decimal(record['values']['time']) if record['values'].get('time') is not None else None)
                    unresolved[(item.source_record_id,item.reason_code,item.issue_column)]=item
            items=tuple(sorted(unresolved.values(),key=lambda i:(i.relative_path,i.line_number,i.reason_code,i.issue_column or '')))
            quarantine=len({i.source_record_id for i in items if i.disposition=='quarantine'})
            conflicts=len({i.source_record_id for i in items if i.disposition=='conflict'})
            selected=[(when,ok) for c,when,ok in source_revision.values() if c['cycle_id']==cycle['cycle_id']]
            count=len(selected); pairs=sum(all(ok.values()) for _,ok in selected)
            reasons=[]
            if file is None: reasons.append('missing_expected_source_file')
            elif not loaded: reasons.append('ingestion_file_not_loaded')
            if quarantine: reasons.append('unresolved_quarantine_records')
            if conflicts: reasons.append('unresolved_conflict_records')
            if pairs!=count: reasons.append('records_without_paired_measures')
            resolved=loaded and quarantine==0 and conflicts==0
            state=HarvestCompleteness(resolved and count==pairs,resolved,1,int(file is not None),int(loaded),int(not loaded),
                *(file.get(k,0) if file else 0 for k in ('read','accepted','quarantine','duplicate','conflict')),
                quarantine,conflicts,count,pairs,sum(ok['ProdA'] for _,ok in selected),sum(ok['ProdB'] for _,ok in selected),
                len({when for when,ok in selected if ok['ProdA']}),len({when for when,ok in selected if ok['ProdB']}),
                tuple(sorted(reasons)),path,items)
            states.append(dict(scope_id=scope,publication_id=publication,cycle_id=cycle['cycle_id'],
                snapshot_complete=state.snapshot_complete,source_resolved=state.source_resolved,
                accepted_records=count,paired_records=pairs,completeness=canonical_json(to_dict(state))))
        header=dict(scope_id=scope,publication_id=publication,config_version=version,load_id=load)
        return header,cycles,measures,states,envelope,audit

    def build(self,scope,publication,core):
        header,cycles,measures,states,_,_=self.expected(scope,publication,core,self.config_version,self.load_id)
        for table,rows in (('harvest_cycle',cycles),('harvest_measure',measures),('harvest_state',states),('harvest_header',[header])):
            self.repo._insert(table,rows)

    def verify(self,scope,publication,core):
        headers=self.rows('harvest_header',scope,publication)
        if len(headers)!=1:
            raise ValueError('Missing or conflicting harvest header')
        header,cycles,measures,states,envelope,audit=self.expected(scope,publication,core,headers[0]['config_version'],headers[0]['load_id'])
        def normalize(rows):
            output=[]
            for row in rows:
                row=dict(row)
                if row.get('value_decimal') is not None:
                    row['value_decimal']=decimal_text(row['value_decimal'])
                for key in ('accepted_records','paired_records'):
                    if key in row: row[key]=int(row[key])
                output.append(canonical_json(row))
            return sorted(output)
        groups=(('harvest_cycle',cycles),('harvest_measure',measures),('harvest_state',states))
        if headers!=[header] or any(normalize(self.rows(table,scope,publication))!=normalize(expected) for table,expected in groups):
            raise ValueError('Harvest bundle differs from canonical source and frozen audit')
        return {'version':BUNDLE,'rows':len(measures)//2,'cycles':len(cycles),
            'checksum':digest([header,normalize(cycles),normalize(measures),normalize(states),audit['checksum']]),'load_ref':header['load_id'],
            'configuration_ref':{'kind':'harvest','version':envelope['version_id'],'hash':envelope['content_hash']}}


from greenhouse.clickhouse.water import WaterAdapter


class HarvestAdapter(WaterAdapter):
    LINEAGE_TABLE='harvest_measure'
    LINEAGE_TYPE='harvest'
    LINEAGE_UNITS=(('ProdA',HARVEST_SOURCE_UNIT),('ProdB',HARVEST_SOURCE_UNIT))

    def __init__(self,client,*,raw_snapshots=None):
        super().__init__(client,raw_snapshots=raw_snapshots)
        self.harvest_mart=HarvestMart(client,None)

    def publication(self,publication_id):
        from greenhouse.clickhouse.registry import reference
        envelope=self._envelope(publication_id)
        if envelope['receipts'].get('harvest',{}).get('version')!=BUNDLE:
            raise ValueError('Publication lacks harvest mart bundle')
        return reference(self.client,publication_id,envelope)

    def catalog_cycles(self,publication_id):
        publication=self.publication(publication_id)
        return tuple(CatalogCycle(r['cycle_id'],r['compartment_id'],r['compartment_name'],date.fromisoformat(r['start']),date.fromisoformat(r['end']))
            for r in sorted(self.harvest_mart.rows('harvest_cycle',publication.scope_id,publication_id),key=lambda r:r['cycle_id']))

    def read_harvest_records(self,context,publication):
        context.matches(self.publication(context.publication_id)); self.cycle_window(context)
        rows=self.client.query('''SELECT DISTINCT * FROM harvest_measure WHERE scope_id={scope:String}
            AND publication_id={pub:String} AND cycle_id={cycle:String} AND compartment_id={comp:String}
            AND harvest_date>={start:Date32} AND harvest_date<{end:Date32} ORDER BY harvest_date,logical_record_key,measure_code''',
            {'scope':context.scope_id,'pub':context.publication_id,'cycle':context.cycle_id,'comp':context.compartment_ids[0],'start':context.start,'end':context.end})
        grouped={}
        for row in rows:
            if row['source_unit']!=HARVEST_SOURCE_UNIT:
                raise ValueError('Unsupported harvest source unit')
            record=grouped.setdefault(row['logical_record_key'],{})
            if row['measure_code'] in record:
                raise ValueError('Duplicate harvest record/measure grain')
            record[row['measure_code']]=row
        result=[]
        for key,parts in grouped.items():
            if set(parts)!= {'ProdA','ProdB'}:
                raise ValueError('Harvest record lacks paired measure metadata')
            left,right=parts['ProdA'],parts['ProdB']
            if any(left[k]!=right[k] for k in ('revision_id','harvest_date','correction_id','correction_version')):
                raise ValueError('Mixed harvest revision')
            measures=[decode_measure(r['value_exact'],r['quality'],r['applicability'],r['reason'],json.loads(r['basis_metadata'])) for r in (left,right)]
            result.append(HarvestRecord(publication,context.cycle_id,context.compartment_ids[0],date.fromisoformat(left['harvest_date']),
                *measures,left['revision_id'],key,correction_id=left['correction_id'] or None,
                correction_version=left['correction_version'] or None,paired=all(m.accepted for m in measures)))
        return tuple(result)

    def harvest_state(self,context):
        self.cycle_window(context)
        rows=[r for r in self.harvest_mart.rows('harvest_state',context.scope_id,context.publication_id) if r['cycle_id']==context.cycle_id]
        if len(rows)!=1:
            raise ValueError('Missing harvest completeness state')
        return from_dict(HarvestCompleteness,json.loads(rows[0]['completeness']))

    def harvest_rule(self,context):
        if context.rule_version is None:
            return None
        rows=[r for r in self.catalog_harvest_rules(context.scope_id,context.compartment_ids[0]) if r.rule_version==context.rule_version]
        if len(rows)!=1:
            raise ValueError('Unknown harvest rule')
        rows[0].matches(context)
        return rows[0]

    def read_unresolved_source(self,context,publication,unresolved_item,*,rule_grain=None):
        from greenhouse.contracts import UnresolvedSourceAudit
        from greenhouse.ingestion.parser import Source, SourceParser
        from greenhouse.ingestion.run import checksum
        context.matches(publication)
        state=self.harvest_state(context)
        identity=lambda i:(i.source_record_id,i.disposition,i.reason_code,i.issue_column)
        items=[i for i in state.unresolved_items if identity(i)==identity(unresolved_item)]
        if len(items)!=1:
            raise ValueError('Issue is not frozen in this publication/cycle')
        item=items[0]
        payload=dict(source_record_id=item.source_record_id,snapshot_id=publication.snapshot_id,reason_code=item.reason_code,
            disposition=item.disposition,issue_column=item.issue_column,relative_path=item.relative_path,line_number=item.line_number,
            source_time_serial=item.source_time_serial,publication=publication,cycle_id=context.cycle_id,
            compartment_id=context.compartment_ids[0],requested_start=context.start,requested_end=context.end,
            rule_version=context.rule_version,rule_grain=rule_grain)
        raw=self.raw_snapshots.get(publication.snapshot_id)
        if raw is None:
            return UnresolvedSourceAudit(**payload,note='raw-mount-missing')
        envelope=self._envelope(publication.publication_id)
        audit=self.harvest_mart.repo.load_audit(publication.scope_id,envelope['load_id'])
        entries=[json.loads(r['payload']) for r in audit['issues'] if r['source_record_id']==item.source_record_id]
        if len(entries)!=1:
            raise ValueError('Missing or conflicting source audit identity')
        source=entries[0]; relative=Path(source['source_file']); target=raw/relative
        if relative.is_absolute() or '..' in relative.parts or '\\' in str(relative):
            return UnresolvedSourceAudit(**payload,note='raw-identity-mismatch')
        try:
            from greenhouse.clickhouse.raw_identity import source_path
            target=source_path(raw,publication.snapshot_id,source)
            if checksum(target)!=source['file_checksum']:
                raise ValueError('Raw source mismatch')
            parser=SourceParser(target,Source(publication.snapshot_id,source['snapshot_sha'],source['source_file'],source['file_checksum'],source['run_id']))
            record=next(r for r in parser.records() if r['source_record_id']==item.source_record_id)
            if record['raw_hash']!=source['raw_hash'] or record['line_end']!=source['line_end']:
                raise ValueError('Raw record mismatch')
        except FileNotFoundError:
            return UnresolvedSourceAudit(**payload,note='raw-file-missing')
        except OSError:
            return UnresolvedSourceAudit(**payload,note='raw-file-unreadable')
        except (ValueError,StopIteration):
            return UnresolvedSourceAudit(**payload,note='raw-identity-mismatch')
        return UnresolvedSourceAudit(**payload,headers=tuple(parser.header),tokens=tuple(record['raw_tokens']),file_verified=True,record_verified=True)
