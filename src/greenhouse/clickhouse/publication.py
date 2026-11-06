"""Append-only verified publications; readers pin an immutable complete envelope.

Bundle validators are registered by typed mart modules. Core-only publication is
valid metadata but does not imply a usable irrigation/harvest/etc mart.
"""
from datetime import datetime, timezone
import json
from uuid import uuid4

from greenhouse.clickhouse.coordinator import writer_lock
from greenhouse.clickhouse.encoding import canonical_json, digest
from greenhouse.clickhouse.repository import Repository

STAGES=('PREPARING','WRITING','VERIFIED','COMPLETE')


class Publications:
    def __init__(self, client, coordinator, *, bundles=None):
        self.client=client
        self.repository=Repository(client,coordinator)
        self.coordinator=coordinator
        self.bundles=dict(bundles or {})

    def _states(self, scope, workflow=None):
        parameters={'scope':scope}
        sql='SELECT DISTINCT * FROM publication_state WHERE scope_id={scope:String}'
        if workflow is not None:
            sql+=' AND workflow_id={workflow:String}'
            parameters['workflow']=workflow
        rows=self.client.query(sql,parameters)
        seen=set()
        for row in rows:
            if digest(json.loads(row['payload']))!=row['content_hash'] or row['stage'] not in STAGES:
                raise ValueError('Invalid publication state checksum/stage')
            identity=(row['workflow_id'],row['stage'])
            if identity in seen:
                raise ValueError('Conflicting publication workflow state')
            seen.add(identity)
        workflows={r['workflow_id'] for r in rows}
        for workflow in workflows:
            group=[r for r in rows if r['workflow_id']==workflow]
            stages={r['stage'] for r in group}
            if stages!=set(STAGES[:max(STAGES.index(s) for s in stages)+1]):
                raise ValueError('Publication workflow has missing state transitions')
            if len({(r['publication_id'],r['generation']) for r in group})!=1:
                raise ValueError('Workflow publication identity changed')
            for row in group:
                payload=json.loads(row['payload'])
                if payload['publication_id']!=row['publication_id'] or payload['generation']!=int(row['generation']):
                    raise ValueError('Publication envelope identity mismatch')
        return rows

    def _append(self, scope, workflow, publication, generation, stage, payload):
        self.repository._insert('publication_state',[dict(scope_id=scope,workflow_id=workflow,
            publication_id=publication,generation=generation,stage=stage,
            content_hash=digest(payload),payload=canonical_json(payload))])

    def _verify(self, scope, load, configuration_checksum, publication):
        core=self.repository.load_membership(scope,load)
        core_receipt={'version':'canonical-core-v1','rows':len(core),
            'checksum':digest(sorted((r['revision_id'],r['content_hash']) for r in core))}
        configs=self.repository.configurations()
        # Configuration export covers every scope, but only this scope's pins
        # affect this publication. New immutable versions do not invalidate pins.
        pins=[r for r in configs['rows'] if r['scope_id']==scope]
        selected=configuration_checksum['pins']
        by_identity={(r['kind'],r['version_id']):r for r in pins}
        for pin in selected:
            if by_identity.get((pin['kind'],pin['version_id']))!=pin:
                raise ValueError('Publication configuration pin missing or changed')
        if digest(selected)!=configuration_checksum['checksum']:
            raise ValueError('Publication configuration pin checksum mismatch')
        receipts={'core':core_receipt,'configuration':{'version':'config-envelope-v1',
            'rows':len(selected),'checksum':configuration_checksum['checksum']}}
        for name,validator in sorted(self.bundles.items()):
            if name in receipts:
                raise ValueError('Reserved bundle name')
            receipt=validator(scope,publication,core)
            if not isinstance(receipt,dict) or not {'version','rows','checksum'}<=receipt.keys():
                raise ValueError('Bundle validator must return a versioned count/checksum receipt')
            reference=receipt.get('configuration_ref')
            if reference is not None and not any((p['kind'],p['version_id'],p['content_hash'])==
                    (reference['kind'],reference['version'],reference['hash']) for p in selected):
                raise ValueError('Bundle configuration is not pinned by this publication')
            receipts[name]=receipt
        return receipts

    def publish(self, scope, workflow, load_id, *, build=None, fail_at=None):
        if not all(isinstance(v,str) and v.strip() for v in (scope,workflow,load_id)):
            raise ValueError('Publication requires explicit scope, workflow and load')
        def injected(stage):
            if fail_at==stage:
                raise RuntimeError('Injected publication crash: '+stage)
        with writer_lock(self.coordinator,self.client):
            states=self._states(scope,workflow)
            if states:
                headers={r['payload'] for r in states if r['stage']=='PREPARING'}
                if len(headers)!=1:
                    raise ValueError('Workflow lacks a unique preparation envelope')
                prepared=json.loads(next(iter(headers)))
                if prepared['load_id']!=load_id or prepared['required_bundles']!=sorted(self.bundles):
                    raise ValueError('Workflow retry changed load or required bundles')
                publication=prepared['publication_id']; generation=prepared['generation']
            else:
                generation=max((int(r['generation']) for r in self._states(scope)),default=0)+1
                publication=uuid4().hex
                configs=self.repository.configurations()
                pins=[r for r in configs['rows'] if r['scope_id']==scope]
                prepared={'load_id':load_id,'publication_id':publication,'generation':generation,
                    'configuration':{'pins':pins,'checksum':digest(pins)},
                    'required_bundles':sorted(self.bundles)}
                self._append(scope,workflow,publication,generation,'PREPARING',prepared)
                injected('PREPARING')
            by_stage={r['stage']:r for r in states}
            if 'WRITING' not in by_stage:
                self._append(scope,workflow,publication,generation,'WRITING',prepared)
                injected('WRITING')
            if build is not None and 'COMPLETE' not in by_stage:
                build(scope,publication,self.repository.load_membership(scope,load_id))
            receipts=self._verify(scope,load_id,prepared['configuration'],publication)
            verified=prepared | {'receipts':receipts}
            if 'VERIFIED' in by_stage:
                if json.loads(by_stage['VERIFIED']['payload'])!=verified:
                    raise ValueError('Verified bundle changed while resuming publication')
            else:
                self._append(scope,workflow,publication,generation,'VERIFIED',verified)
                injected('VERIFIED')
            if 'COMPLETE' in by_stage:
                complete=json.loads(by_stage['COMPLETE']['payload'])
                if {k:complete[k] for k in verified}!=verified:
                    raise ValueError('Complete publication differs from verified receipts')
            else:
                complete=verified | {'committed_at':datetime.now(timezone.utc).isoformat()}
                self._append(scope,workflow,publication,generation,'COMPLETE',complete)
                injected('COMPLETE')
            return complete

    def pin(self, scope, *, publication_id=None):
        states=self._states(scope)
        complete=[r for r in states if r['stage']=='COMPLETE'
                  and (publication_id is None or r['publication_id']==publication_id)]
        if not complete:
            raise ValueError('No complete publication available')
        complete.sort(key=lambda r:int(r['generation']))
        selected=complete[-1]
        if len([r for r in complete if r['generation']==selected['generation']])!=1:
            raise ValueError('Conflicting complete generation')
        payload=json.loads(selected['payload'])
        if payload['required_bundles']!=sorted(self.bundles):
            raise ValueError('Publication bundle registry mismatch')
        receipts=self._verify(scope,payload['load_id'],payload['configuration'],payload['publication_id'])
        if receipts!=payload['receipts']:
            raise ValueError('Complete publication data does not match its manifest')
        return payload
