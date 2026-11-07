"""Replay a verified native cutoff/delta into the private restored PostgreSQL.

Original envelopes are immutable archives. Operational scenario publication IDs
are mapped only after full contract parity; transport gets a NEW PG generation.
"""
import argparse
from contextlib import closing
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path[:0]=['/cutover-tools','/migration','/repro']
from state import ROOT,RAW,sealed_read,pg_connect
from common import COORDINATOR,connect,write_json
from read_business_reference import project,fingerprint
from greenhouse.clickhouse import cutover_journal
from greenhouse.clickhouse.encoding import digest
from greenhouse.contracts import (CostScenario,TariffProfile,RuleScheduleRevision,
    IrrigationRuleProfile,HarvestRuleProfile,PublicationRef,from_dict,to_dict)


def archive_native(conn,archive,hashed):
    from psycopg.types.json import Jsonb
    from greenhouse.db.migrate import migrate
    migrate(conn)
    previous=conn.execute('SELECT payload FROM meta.clickhouse_cutover_archive WHERE archive_sha256=%s',(hashed,)).fetchone()
    if previous and previous[0]!=archive:raise ValueError('Native archive identity drift')
    conn.execute('INSERT INTO meta.clickhouse_cutover_archive(archive_sha256,payload) VALUES (%s,%s) ON CONFLICT DO NOTHING',(hashed,Jsonb(archive)))
    registry=archive['configurations']
    if registry['checksum']!=digest(registry['rows']):raise ValueError('Invalid native registry')
    counts={}
    with conn.transaction():
        for row in registry['rows']:
            payload=json.loads(row['payload'])
            if row['schema_version']!=1 or digest(payload)!=row['content_hash']:raise ValueError('Unsupported or changed configuration')
            key=(row['kind'],row['scope_id'],row['version_id'])
            old=conn.execute('SELECT request_id,content_hash,payload FROM meta.clickhouse_configuration_archive WHERE kind=%s AND data_scope_id=%s AND version_id=%s',key).fetchone()
            if old and old!=(row['request_id'],row['content_hash'],payload):raise ValueError('Archived native configuration drift')
            conn.execute('INSERT INTO meta.clickhouse_configuration_archive VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (*key,row['request_id'],row['content_hash'],Jsonb(payload)))
            saved=conn.execute('SELECT request_id,content_hash,payload FROM meta.clickhouse_configuration_archive WHERE kind=%s AND data_scope_id=%s AND version_id=%s',key).fetchone()
            if saved!=(row['request_id'],row['content_hash'],payload):raise ValueError('Native configuration request conflicts with another identity')
            counts[row['kind']]=counts.get(row['kind'],0)+1
    return counts


def configurations(conn,archive):
    from greenhouse.db.cost_adapter import PostgresCostAdapter
    from greenhouse.db.rule_schedule import register_rule_schedule
    from greenhouse.db.irrigation_rules import register_irrigation_rule
    from greenhouse.db.harvest_rules import register_harvest_rule
    from greenhouse.db.catalog import PostgresCatalogAdapter
    cost=PostgresCostAdapter(conn);catalog=PostgresCatalogAdapter(conn);tariff_map={}
    for row in archive['configurations']['rows']:
        payload=json.loads(row['payload']);kind=row['kind']
        if kind=='tariff':
            profile=from_dict(TariffProfile,payload)
            identities=conn.execute('''SELECT tariff_version FROM config.tariff_profile
                WHERE tariff_id=%s AND version=%s AND data_scope_id=%s AND compartment_id IS NOT DISTINCT FROM %s''',
                (profile.tariff_id,profile.version,profile.data_scope_id,profile.compartment_id)).fetchall()
            if identities and identities!=[(profile.tariff_version,)]:
                if len(identities)!=1:raise ValueError('Ambiguous existing logical tariff identity')
                existing=cost.get_tariff(identities[0][0])
                # Only physical version/rate IDs may differ. Every numeric,
                # unit, interval, scope and evidence field must still match.
                adjusted=replace(profile,tariff_version=existing.tariff_version,
                    rates=tuple(replace(r,tariff_version=existing.tariff_version,rate_id=e.rate_id)
                        for r,e in zip(profile.rates,existing.rates)))
                if adjusted!=existing:raise ValueError('Native tariff alias has different semantic content')
                tariff_map[profile.tariff_version]=existing.tariff_version
            else:
                cost.register_tariff(profile);tariff_map[profile.tariff_version]=profile.tariff_version
        elif kind=='rule_schedule':register_rule_schedule(conn,from_dict(RuleScheduleRevision,payload))
        elif kind in ('irrigation_rule','harvest_rule'):
            profile=from_dict(IrrigationRuleProfile if kind=='irrigation_rule' else HarvestRuleProfile,payload)
            stored=(catalog.catalog_rules if kind=='irrigation_rule' else catalog.catalog_harvest_rules)(profile.scope_id,profile.compartment_id)
            matches=[p for p in stored if p.rule_version==profile.rule_version]
            if matches and matches!=[profile]:raise ValueError('Operational rule drift')
            if not matches:(register_irrigation_rule if kind=='irrigation_rule' else register_harvest_rule)(conn,profile)
    write_json(ROOT/'tariff-map.json',tariff_map)
    return tariff_map


def corrections(conn,archive):
    from greenhouse.db.corrections import correct
    results=[]
    for row in archive['corrections']:
        source=json.loads(row['payload']);audit=source['correction_audit']
        kinds={'Resources':['resource'],'Production':['harvest'],'GreenhouseClimate':['climate','drain']}[row['record_type']]
        for kind in kinds:
            fields=[r[0] for r in conn.execute('SELECT measure_code FROM config.measure_definition WHERE record_type=%s',(kind,))]
            latest=conn.execute('SELECT revision_id FROM core.record_revision WHERE data_scope_id=%s AND source_record_id=%s AND record_type=%s ORDER BY revision_number DESC LIMIT 1',
                (row['scope_id'],row['source_record_id'],kind)).fetchone()
            if not latest:raise ValueError('Correction source missing in rollback baseline')
            old={k:None if v is None else str(v) for k,v in conn.execute('SELECT measure_code,value FROM core.measure_value WHERE revision_id=%s',(latest[0],))}
            new={k:source['values'][k] for k in fields}
            cid=row['correction_id']+(':'+kind if len(kinds)>1 else '')
            existing=conn.execute('SELECT new_value FROM meta.correction WHERE correction_id=%s',(cid,)).fetchone()
            if existing:
                from greenhouse.db.pipeline import decimal_key
                if existing[0]['values']!={k:decimal_key(v) for k,v in new.items()}:raise ValueError('Previously imported correction drift')
                results.append(dict(correction_id=cid,reused=True));continue
            from greenhouse.db.pipeline import decimal_key
            if {k:decimal_key(v) for k,v in old.items()}=={k:decimal_key(v) for k,v in new.items()}:continue
            results.append(correct(conn,row['scope_id'],row['source_record_id'],kind,old,new,audit['actor'],audit['evidence'],correction_id=cid))
    return results


def replay(conn,archive):
    from greenhouse.sensor.stream_store import commit_batch
    from greenhouse.sensor.replay_publication import publish_replay,read_publication
    from greenhouse.clickhouse.replay_evaluation import build_replay_rows
    from psycopg.types.json import Jsonb
    import hashlib
    configs={json.loads(r['payload'])['replay_id']:json.loads(r['payload'])
        for r in archive['configurations']['rows'] if r['kind']=='replay'}
    for entry in archive['batches']:
        batch=entry['receipt']
        commit_batch(conn,'rollback:'+batch['stream_id'],'pg-cutover-20261107:'+batch['generation'],int(batch['batch_id']),entry['records'])
    results=[]
    # Import every verified old pin as well as the current replay. Derive a PG
    # publication ID from the shared metadata, preserving original membership.
    for original in sorted(archive['replays'],key=lambda r:(r.get('current',True),r['publication_id'])):
        config=configs[original['replay_id']]
        expected_events={e['event_id']:e for e in original['events']}
        members=[]
        for identity,hashed,event in conn.execute('SELECT identity,payload_hash,envelope FROM sensor_stream.event_variant WHERE replay_id=%s',(config['replay_id'],)):
            if event.get('event_id') in expected_events:
                if event!=expected_events[event['event_id']]:raise ValueError('Old replay pin event differs from transport archive')
                members.append((identity,hashed))
        members.sort()
        if len(members)!=len(expected_events):raise ValueError('Old replay pin members are missing')
        membership=hashlib.sha256(json.dumps(members,separators=(',',':')).encode()).hexdigest()
        metadata={k:config[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')}
        state=original['metadata'][-1];metadata.update(state=state,membership_checksum=membership)
        pub=hashlib.sha256(json.dumps(metadata,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with conn.transaction():
            inserted=conn.execute('''INSERT INTO sensor_stream.publication
                (publication_id,replay_id,data_scope_id,cycle_id,compartment_id,quality_policy_version,cursor_sequence,observed_through,received_through,state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING publication_id''',
                (pub,config['replay_id'],config['scope_id'],config['cycle_id'],config['compartment_id'],config['quality_policy_version'],
                 state['cursor_sequence'],state['observed_through'],state['received_through'],Jsonb(state))).fetchone()
            if inserted:
                for identity,hashed in members:conn.execute('INSERT INTO sensor_stream.publication_event VALUES (%s,%s,%s,%s)',(pub,config['replay_id'],identity,hashed))
                conn.execute('INSERT INTO sensor_stream.publication_seal VALUES (%s,%s,%s,%s)',(pub,config['replay_id'],len(members),membership))
        if original.get('current',True):
            current=publish_replay(conn,**{k:config[k] for k in ('replay_id','scope_id','cycle_id','compartment_id','quality_policy_version')})
            if current!=pub:raise ValueError('Reconciled current replay differs from imported native pin')
        meta,events=read_publication(conn,pub)
        if {e['event_id']:e for e in events}!={e['event_id']:e for e in original['events']}:raise ValueError('Rollback lost/changed original sensor envelopes')
        if meta[-1]!=original['metadata'][-1]:raise ValueError('Rollback replay state/cursor/checksum differs')
        expected=fingerprint(build_replay_rows(original['events'],config));actual=fingerprint(build_replay_rows(events,config))
        if actual!=expected:raise ValueError('Rollback replay business fingerprint differs')
        results.append(dict(replay_id=config['replay_id'],clickhouse_publication=original['publication_id'],postgres_publication=pub,
            events=len(events),original_envelopes_exact=True,state_exact=True,business_fingerprint=actual,
            current=original.get('current',True),new_pg_checkpoint_generation=True))
    return results


def inputs():
    archive,hashed=sealed_read(ROOT/'post-cutover.json')
    journal=archive['journal']
    if journal['mode']!='paused' or any(r['outcome'] is None for r in journal['records']):raise ValueError('Freeze/drain and reconcile before rollback')
    with pg_connect() as conn:
        kinds=archive_native(conn,archive,hashed);configurations(conn,archive)
        changed=corrections(conn,archive);replays=replay(conn,archive)
        write_json(ROOT/'rollback-inputs.json',dict(status='passed',archive_sha256=hashed,configuration_kinds=kinds,
            native_configurations_preserved=sum(kinds.values()),corrections=changed,replays=replays,
            journal_head=journal['head'],journal_cursor=len(journal['records'])))
    print(json.dumps({'stage':'rollback_inputs','status':'passed'}),flush=True)


def rebuild():
    from greenhouse.db.pipeline import run_pipeline
    cutoff,_=sealed_read(ROOT/'cutoff.json')
    with pg_connect() as conn:
        result=run_pipeline(conn,RAW,ROOT/'postgres-output',cutoff['publication']['scope_id'],'AGC_QUALITY_V1',
            workflow='cutover_rollback_20261107',resume=(ROOT/'postgres-output/pipeline/cutover_rollback_20261107').exists(),
            ingestion_run=Path('/state/output/ingestion')/cutoff['envelope']['load_id'])
        write_json(ROOT/'postgres-corrected-publication.json',result)
        print(json.dumps({'stage':'postgres_rebuilt',**result}),flush=True)


def verify():
    from greenhouse.db.cost_adapter import PostgresCostAdapter
    from greenhouse.db.energy_adapter import PostgresEnergyQueryAdapter
    from greenhouse.app import wiring
    archive,hashed=sealed_read(ROOT/'post-cutover.json')
    baseline=json.loads((ROOT/'baseline-map.json').read_text())
    target_id=json.loads((ROOT/'postgres-corrected-publication.json').read_text())['publication_id']
    with pg_connect() as conn:
        adapter=PostgresCostAdapter(conn);target=adapter.publication(target_id)
        actual=project(conn,target_id,RAW)
        write_json(ROOT/'postgres-rollback-business.json',actual)
        if actual['results']!=archive['business']:raise ValueError('Corrected six-BR business contracts differ')
        mapping={baseline['clickhouse']['publication_id']:from_dict(PublicationRef,baseline['postgres']),
            archive['publication']['publication_id']:target}
        imported=[]
        tariff_map=json.loads((ROOT/'tariff-map.json').read_text())
        for original in sorted((from_dict(CostScenario,s) for s in archive['scenarios']),key=lambda s:(s.scenario_id,s.version)):
            if original.publication.publication_id not in mapping:raise ValueError('Unproved scenario publication mapping')
            pub=mapping[original.publication.publication_id]
            context=None if original.analysis_context is None else replace(original.analysis_context,publication_id=pub.publication_id)
            candidate=replace(original,publication=pub,analysis_context=context,
                baseline_tariff_version=tariff_map[original.baseline_tariff_version])
            stored=adapter.register_scenario(candidate)
            # Native legacy versions lacked a created_at. Preserve that fact in
            # the archive and label the PG-generated import timestamp explicitly.
            expected=replace(candidate,created_at=stored.created_at) if candidate.created_at is None else candidate
            if stored!=expected:raise ValueError('Scenario immutable content/audit differs after mapping')
            if adapter.register_scenario(candidate)!=stored:raise ValueError('Scenario import retry differs')
            imported.append(dict(scenario_version=stored.scenario_version,request_id=stored.request_id,
                native_created_at_known=candidate.created_at is not None,author_preserved=stored.author==candidate.author))
        # Explicit post-cutover user scenario survives and produces the same cost.
        command=json.loads((ROOT/'native-commands.json').read_text());sv=command['scenario']['scenario_version']
        scenario=adapter.get_scenario(sv);result=wiring.cost_service(conn).compare_scenarios(scenario.analysis_context,scenario_version=sv)
        if (str(result.baseline.official_total_cost_eur),str(result.scenario.official_total_cost_eur))!=(command['baseline_eur'],command['scenario_eur']):
            raise ValueError('Post-cutover scenario cost changed on rollback')
        old=project(conn,baseline['postgres']['publication_id'],RAW)
        cutoff,_=sealed_read(ROOT/'cutoff.json')
        if old['results']!=cutoff['business']:raise ValueError('Rollback changed old frozen publication')
        write_json(ROOT/'rollback-verification.json',dict(status='passed',archive_sha256=hashed,
            six_brs_business_exact=True,old_publication_unchanged=True,scenario_versions=len(imported),
            scenario_retry_exact=True,post_cutover_scenario_cost_exact=True,scenarios=imported,
            source_publication=archive['publication'],target_publication=to_dict(target)))
    print(json.dumps({'stage':'rollback_verified','status':'passed','scenario_versions':len(imported)}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['inputs','rebuild','verify'])
    globals()[p.parse_args().mode]()
