"""Candidate revision and configuration storage; no publication or KPI claim.

Load receipts gate ingestion completion. Publication verification follows at T42.
Callers reconcile ambiguous server writes before allowing another attempt.
"""
import json
from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.encoding import canonical_json, decimal_projection, digest, source_clock_us


class Repository:
    def __init__(self, client, coordinator):
        self.client, self.coordinator = client, coordinator

    def _insert(self, table, rows, *, query_id=None):
        if not rows:
            return
        if not table.replace('_','').isalnum() or not table.islower():
            raise ValueError('Invalid internal table')
        from greenhouse.clickhouse.journal import execute_write
        execute_write(self.client,self.coordinator,'INSERT INTO '+table+' FORMAT JSONEachRow\n'+
            '\n'.join(canonical_json(row) for row in rows),query_id=query_id)

    def register_configuration(self, kind, scope, version, request, payload):
        if not all(isinstance(v,str) and v.strip() for v in (kind,scope,version,request)):
            raise ValueError('Configuration requires explicit identity')
        content = canonical_json(payload)
        hashed = digest(payload)
        with writer_lock(self.coordinator, self.client):
            existing = self.client.query('''SELECT DISTINCT kind,scope_id,version_id,request_id,content_hash,payload
                FROM configuration_envelope WHERE (kind={kind:String} AND scope_id={scope:String}
                AND version_id={version:String}) OR request_id={request:String}''',
                {'kind':kind,'scope':scope,'version':version,'request':request})
            expected = {'kind':kind,'scope_id':scope,'version_id':version,'request_id':request,
                        'content_hash':hashed,'payload':content}
            if existing:
                if len(existing)!=1 or existing[0]!=expected:
                    raise ValueError('Configuration identity or request reused with different content')
                return False
            self._insert('configuration_envelope',[expected | {'schema_version':1}])
        return True

    def configurations(self):
        rows = self.client.query('SELECT DISTINCT * FROM configuration_envelope ORDER BY kind,scope_id,version_id')
        identities, requests = set(),set()
        for row in rows:
            key = (row['kind'],row['scope_id'],row['version_id'])
            if key in identities or row['request_id'] in requests:
                raise ValueError('Conflicting configuration rows')
            identities.add(key); requests.add(row['request_id'])
            if digest(json.loads(row['payload'])) != row['content_hash']:
                raise ValueError('Configuration checksum mismatch')
        return {'format':'greenhouse-config-v1','rows':rows,'checksum':digest(rows)}

    def restore_configurations(self, exported):
        if exported['format']!='greenhouse-config-v1' or digest(exported['rows'])!=exported['checksum']:
            raise ValueError('Configuration export checksum mismatch')
        for row in exported['rows']:
            payload=json.loads(row['payload'])
            if row['schema_version']!=1 or digest(payload)!=row['content_hash']:
                raise ValueError('Unsupported/corrupt configuration envelope')
        for row in exported['rows']:
            self.register_configuration(row['kind'],row['scope_id'],row['version_id'],row['request_id'],json.loads(row['payload']))

    def append_record(self, scope, record, *, revision=1, correction_id='', correction=None, quality_policy='AGC_QUALITY_V1'):
        if not scope or not isinstance(revision,int) or isinstance(revision,bool) or revision<1:
            raise ValueError('Explicit scope and positive revision required')
        if revision>1:
            # Like PG correction_input: audited typed corrections clear lexical
            # numeric issues; immutable raw audit remains in load_issue.
            record=dict(record,issues=[],disposition='accepted')
        if not core_eligible(record):
            raise ValueError('Record has no eligible typed core values')
        if revision>1 and (not correction_id or not isinstance(correction,dict)
                or set(correction)!={'actor','evidence','previous_revision'}
                or not all(isinstance(v,str) and v.strip() for v in correction.values())):
            raise ValueError('Correction requires identity, actor, evidence and previous revision')
        if revision==1 and (correction_id or correction):
            raise ValueError('Initial source revision cannot carry correction metadata')
        if not quality_policy or not record['logical_key']:
            raise ValueError('Quality policy and logical key are required')
        kind=record['source_file'].split('/')[-1].removesuffix('.csv')
        compartment=record['source_file'].split('/')[0]
        # run_id is operational provenance, not part of immutable source content.
        payload={k:v for k,v in record.items() if k!='run_id'}
        if correction is not None:
            payload['correction_audit'] = correction | {'correction_id':correction_id}
        hashed=digest(payload)
        identity=record_identity(record)
        key={'scope':scope,'kind':kind,'compartment':compartment,'logical':identity}
        with writer_lock(self.coordinator, self.client):
            rows=self.client.query('''SELECT DISTINCT revision,revision_id,content_hash,payload,quality_policy_version,first_run_id FROM source_revision
                WHERE scope_id={scope:String} AND record_type={kind:String} AND compartment_id={compartment:String}
                AND logical_key={logical:String} ORDER BY revision''',key)
            ordinals={}
            for row in rows:
                if row['revision'] in ordinals and ordinals[row['revision']]!=row:
                    raise ValueError('Conflicting stored revision ordinal')
                ordinals[row['revision']]=row
            if revision in ordinals:
                old=ordinals[revision]
                if old['content_hash']!=hashed or old['quality_policy_version']!=quality_policy:
                    raise ValueError('Source/revision conflict; correction needs new ordinal')
                return old['revision_id'],False
            if revision!=max(ordinals,default=0)+1:
                raise ValueError('Revision must follow previous ordinal')
            if revision>1:
                previous=ordinals[revision-1]
                original=json.loads(previous['payload'])
                if correction['previous_revision']!=previous['revision_id']:
                    raise ValueError('Stale correction previous revision')
                immutable=set(payload)-{'values','null_reasons','correction_audit','issues','disposition'}
                if (any(payload[k]!=original.get(k) for k in immutable)
                        or set(payload['values'])!=set(original['values'])
                        or payload['values']['time']!=original['values']['time']
                        or payload['null_reasons'].get('time')!=original['null_reasons'].get('time')):
                    raise ValueError('Correction cannot change source identity, time or measure set')
                from greenhouse.clickhouse.encoding import decimal_text
                normalize=lambda v: None if v is None else decimal_text(v)
                if {k:normalize(v) for k,v in payload['values'].items()}=={k:normalize(v) for k,v in original['values'].items()}:
                    raise ValueError('Correction must change a measure')
                reused=self.client.query('SELECT DISTINCT revision_id FROM source_revision WHERE scope_id={scope:String} AND correction_id={correction:String}',
                    {'scope':scope,'correction':correction_id})
                if reused:
                    raise ValueError('Correction ID already belongs to another revision')
            revision_id=digest([scope,kind,compartment,identity,revision,hashed])
            measures=[]
            for measure,value in record['values'].items():
                if measure=='time':
                    continue
                exact,projection,status=decimal_projection(value)
                measures.append({'scope_id':scope,'revision_id':revision_id,'measure_code':measure,
                    'value_exact':exact,'value_decimal':projection,'projection_status':status,
                    'null_reason':record['null_reasons'].get(measure,'')})
            # Write measures first: an interrupted attempt has no revision row.
            self._insert('source_measure',measures)
            self._insert('source_revision',[dict(scope_id=scope,snapshot_id=record['snapshot_id'],first_run_id=record['run_id'] if revision==1 else ordinals[1]['first_run_id'],record_type=kind,
                compartment_id=compartment,logical_key=identity,source_record_id=record['source_record_id'],
                revision=revision,revision_id=revision_id,content_hash=hashed,source_file=record['source_file'],
                file_checksum=record['file_checksum'],line_number=record['line_number'],line_end=record['line_end'],
                raw_hash=record['raw_hash'],source_serial=str(record['values']['time']),
                source_timestamp=record['source_timestamp'],source_clock_us=source_clock_us(record['source_timestamp']),
                correction_id=correction_id,parser_version=record['parser_version'],quality_policy_version=quality_policy,
                is_close_boundary=record['is_close_boundary'],payload=canonical_json(payload))])
        return revision_id,True


    def canonical_records(self, scope, *, revision_ids=None):
        """Validate exact logical rows and measures, before choosing latest revisions.

        Physical identical retries collapse here, never by waiting for merges.
        Explicit IDs let future publication readers retain old membership.
        """
        from decimal import Decimal
        rows=self.client.query('SELECT DISTINCT * FROM source_revision WHERE scope_id={scope:String} ORDER BY record_type,compartment_id,logical_key,revision', {'scope':scope})
        by_key,seen_ids={},{}
        for row in rows:
            payload=json.loads(row['payload'])
            if digest(payload)!=row['content_hash']:
                raise ValueError('Stored revision checksum mismatch')
            for column in ('snapshot_id','source_record_id','source_file','file_checksum','raw_hash',
                           'source_timestamp','parser_version','is_close_boundary'):
                if row[column]!=payload[column]:
                    raise ValueError('Stored revision metadata does not match source payload')
            if row['logical_key']!=record_identity(payload) or row['source_serial']!=str(payload['values']['time']):
                raise ValueError('Stored revision source identity mismatch')
            for column in ('line_number','line_end'):
                if int(row[column])!=payload[column]:
                    raise ValueError('Stored revision source line mismatch')
            clock=row['source_clock_us']
            if (int(clock) if clock is not None else None)!=source_clock_us(payload['source_timestamp']):
                raise ValueError('Stored revision source clock mismatch')
            if row['record_type']!=payload['source_file'].split('/')[-1].removesuffix('.csv') or row['compartment_id']!=payload['source_file'].split('/')[0]:
                raise ValueError('Stored revision record type/compartment mismatch')
            if row['revision_id']!=digest([scope,row['record_type'],row['compartment_id'],row['logical_key'],row['revision'],row['content_hash']]):
                raise ValueError('Stored revision ID mismatch')
            key=(row['record_type'],row['compartment_id'],row['logical_key'])
            revisions=by_key.setdefault(key,{})
            if row['revision'] in revisions:
                raise ValueError('Conflicting logical revision rows')
            if row['revision_id'] in seen_ids:
                raise ValueError('Revision ID reused across logical keys')
            revisions[row['revision']]=row
            seen_ids[row['revision_id']]=row
        for revisions in by_key.values():
            if sorted(revisions)!=list(range(1,max(revisions)+1)):
                raise ValueError('Revision history has gaps')
        if revision_ids is None:
            selected=[revisions[max(revisions)] for revisions in by_key.values()]
        else:
            if len(set(revision_ids))!=len(revision_ids) or any(i not in seen_ids for i in revision_ids):
                raise ValueError('Unknown or repeated pinned revision')
            selected=[seen_ids[i] for i in revision_ids]
            keys=[(r['record_type'],r['compartment_id'],r['logical_key']) for r in selected]
            if len(set(keys))!=len(keys):
                raise ValueError('Pinned membership repeats logical key')
        for row in selected:
            measures=self.client.query('SELECT DISTINCT measure_code,value_exact,value_decimal,projection_status,null_reason FROM source_measure WHERE scope_id={scope:String} AND revision_id={id:String} ORDER BY measure_code',{'scope':scope,'id':row['revision_id']})
            payload=json.loads(row['payload'])
            expected=[]
            for code,value in sorted(payload['values'].items()):
                if code=='time':
                    continue
                exact,projection,status=decimal_projection(value)
                expected.append({'measure_code':code,'value_exact':exact,'value_decimal':projection,
                    'projection_status':status,'null_reason':payload['null_reasons'].get(code,'')})
            comparable=lambda values:[r | {'value_decimal':Decimal(r['value_decimal']) if r['value_decimal'] is not None else None} for r in values]
            if comparable(measures)!=comparable(expected):
                raise ValueError('Revision measures incomplete or conflicting')
        return selected


    def load_membership(self, scope, load_id):
        receipts=self.client.query('SELECT DISTINCT * FROM load_receipt WHERE scope_id={scope:String} AND load_id={load:String}',{'scope':scope,'load':load_id})
        if len(receipts)!=1:
            raise ValueError('Unknown, incomplete or conflicting load receipt')
        rows=self.client.query('SELECT DISTINCT revision_id FROM load_member WHERE scope_id={scope:String} AND load_id={load:String} ORDER BY revision_id',{'scope':scope,'load':load_id})
        ids=[r['revision_id'] for r in rows]
        receipt=receipts[0]
        if len(ids)!=int(receipt['membership_count']) or digest(ids)!=receipt['membership_checksum']:
            raise ValueError('Load membership checksum/count mismatch')
        return self.canonical_records(scope,revision_ids=ids)


    def load_audit(self, scope, load_id):
        receipt=self.client.query('SELECT DISTINCT audit_checksum FROM load_receipt WHERE scope_id={scope:String} AND load_id={load:String}',{'scope':scope,'load':load_id})
        if len(receipt)!=1 or not receipt[0]['audit_checksum']:
            raise ValueError('Load lacks frozen ingestion audit')
        files=self.client.query('SELECT DISTINCT source_file,metadata FROM load_file WHERE scope_id={scope:String} AND load_id={load:String} ORDER BY source_file',{'scope':scope,'load':load_id})
        issues=self.client.query('SELECT DISTINCT source_record_id,source_file,disposition,payload FROM load_issue WHERE scope_id={scope:String} AND load_id={load:String} ORDER BY source_record_id,disposition,payload',{'scope':scope,'load':load_id})
        if digest({'files':files,'issues':issues})!=receipt[0]['audit_checksum']:
            raise ValueError('Load ingestion audit checksum mismatch')
        if len({r['source_file'] for r in files})!=len(files):
            raise ValueError('Conflicting file ingestion metadata')
        return {'files':files,'issues':issues,'checksum':receipt[0]['audit_checksum']}


def core_eligible(record):
    """Match baseline: per-measure parse errors retain unaffected typed measures."""
    return (record['disposition']=='accepted' and not record['issues']) or (
        record['disposition']=='quarantine' and bool(record['logical_key'])
        and bool(record['issues']) and all(i['code']=='invalid_numeric' for i in record['issues']))


def record_identity(record):
    return record['source_record_id'] if record['source_file'].endswith('/Production.csv') else record['logical_key']
