"""Exercise production Streamlit navigation against a pinned AGC publication.

Requires PGOPTIONS=-c default_transaction_read_only=on and an existing AGC
publication. This is an automated regression walkthrough, not a human study.
"""
import csv
import hashlib
import importlib.resources
import json
import os
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from time import perf_counter

import psycopg
from streamlit.testing.v1 import AppTest

from greenhouse.settings import Settings

publication_id = os.environ['GREENHOUSE_WALKTHROUGH_PUBLICATION']
output = Path(os.environ['GREENHOUSE_WALKTHROUGH_OUTPUT'])
if 'default_transaction_read_only=on' not in os.environ.get('PGOPTIONS', ''):
    raise RuntimeError('An explicitly read-only PostgreSQL session is required')
with psycopg.connect(**Settings.from_env().connection_kwargs()) as connection:
    if connection.execute('SHOW transaction_read_only').fetchone()[0] != 'on':
        raise RuntimeError('PostgreSQL session is not read-only')

window = (date(2020, 3, 2), date(2020, 3, 3))
screens = [
    ('climate', '🌡️ Khí hậu (BR-01)'),
    ('water', '💧 Nước (BR-02)'),
    ('drain', '🧪 EC/pH drain (BR-03)'),
    ('energy', '⚡ Năng lượng (BR-04)'),
    ('harvest', '🍅 Thu hoạch (BR-05)'),
    ('cost', '💶 Chi phí & Kịch bản (BR-06)'),
    ('overview', '🏠 Tổng quan vụ'),
    ('quality', '🛡️ Chất lượng dữ liệu'),
]
# Independent raw CSV oracle: verify manifest hash, then calculate the two-day
# water totals and ratio without importing the dashboard or query formulas.
raw_root = Path(os.environ['GREENHOUSE_RAW_DIR']) / 'agc-2nd-2019' / (
    'c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68')
manifest = json.loads((raw_root / 'manifest.json').read_text())
relative = 'Automatoes/Resources.csv'
source = raw_root / relative
expected_hash = next(item['sha256'] for item in manifest['files'] if item['path'] == relative)
if hashlib.sha256(source.read_bytes()).hexdigest() != expected_hash:
    raise AssertionError('Raw source hash does not match locked manifest')
with source.open(newline='', encoding='utf-8-sig') as stream:
    source_rows = [row for row in csv.DictReader(stream)
                   if window[0] <= date(1899, 12, 30) + timedelta(days=int(Decimal(row['%Time ']))) <= window[1]]
if len(source_rows) != 2:
    raise AssertionError(f'Expected two source days, got {len(source_rows)}')
irrigation = sum((Decimal(row['Irr']) for row in source_rows), Decimal(0))
drain = sum((Decimal(row['Drain']) for row in source_rows), Decimal(0))
ratio = 100 * drain / irrigation
# The documented benchmark assumption and historical rates are explicit;
# they are not claims about verified greenhouse area or actual invoices.
area_m2 = Decimal('62.5')
energy_kwh = sum(((Decimal(row['Heat_cons']) / Decimal('3.6') +
                   Decimal(row['ElecHigh']) + Decimal(row['ElecLow'])) * area_m2
                  for row in source_rows), Decimal(0))
cost_eur = sum(((Decimal(row['Heat_cons']) * Decimal('0.0083') +
                 Decimal(row['ElecHigh']) * Decimal('0.08') +
                 Decimal(row['ElecLow']) * Decimal('0.04')) * area_m2
                for row in source_rows), Decimal(0))

def ui_number(value):
    return str(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)).replace('.', ',')

started = perf_counter()
with importlib.resources.as_file(importlib.resources.files('greenhouse.app') / 'home.py') as path:
    app = AppTest.from_file(str(path), default_timeout=240).run()
    if app.exception:
        raise AssertionError([str(item.value) for item in app.exception])
    if app.selectbox(key='pub_choice').value.publication_id != publication_id:
        raise AssertionError('Default publication is not the requested publication')
    choices = list(app.selectbox(key='cycle_choice').options)
    matching = [i for i, label in enumerate(choices) if 'Automatoes' in str(label)]
    if len(matching) != 1:
        raise AssertionError(f'Expected exactly one Automatoes cycle, found {len(matching)}')
    app.selectbox(key='cycle_choice').select_index(matching[0]).run()
    app.date_input(key='window').set_value(window).run()
    evidence = []
    for name, label in screens:
        start = perf_counter()
        app.radio(key='screen_choice').set_value(label).run()
        if app.exception:
            raise AssertionError(f'{name}: {[str(item.value) for item in app.exception]}')
        errors = [str(item.value) for item in app.error]
        if errors:
            raise AssertionError(f'{name}: {errors}')
        context = app.session_state['_historical_context']
        if context['publication_id'] != publication_id or context['compartment_id'] != 'Automatoes':
            raise AssertionError(f'{name}: incorrect publication or compartment: {context}')
        if tuple(context['window']) != window:
            raise AssertionError(f'{name}: window changed: {context["window"]}')
        metrics = {item.label: item.value for item in app.metric}
        if name == 'water':
            expected = {'Irrigation observed': ui_number(irrigation) + ' L/m²',
                        'Drain observed': ui_number(drain) + ' L/m²',
                        'Tỷ lệ drain (paired)': ui_number(ratio) + ' %',
                        'Độ phủ paired': '100,00 %'}
            if any(metrics.get(key) != value for key, value in expected.items()):
                raise AssertionError(f'Raw water oracle mismatch: {metrics} vs {expected}')
        if name == 'drain':
            if metrics.get('Tưới nguồn (L/m², ngày tính một lần)') != ui_number(irrigation):
                raise AssertionError('Cross-screen irrigation differs from raw oracle')
            if metrics.get('Drain nguồn (L/m², ngày tính một lần)') != ui_number(drain):
                raise AssertionError('Cross-screen drain differs from raw oracle')
        if name == 'energy' and metrics.get('Năng lượng (kWh)') != f'{energy_kwh:,.3f}':
            raise AssertionError('Energy differs from independent raw oracle')
        if name == 'cost' and metrics.get('Tổng chi phí (EUR)') != f'{cost_eur:,.2f} EUR':
            raise AssertionError('Historical tariff cost differs from independent raw oracle')
        evidence.append({'screen': name, 'elapsed_seconds': round(perf_counter() - start, 3),
                         'metrics': metrics,
                         'download_count': len(app.download_button)})
    def choose_cycle(key, name):
        choices = app.selectbox(key=key).options
        app.selectbox(key=key).select_index(next(i for i,v in enumerate(choices) if name in str(v))).run()
        assert not app.exception and not app.error

    tasks = {}
    app.radio(key='screen_choice').set_value('🌡️ Khí hậu (BR-01)').run()
    choose_cycle('clim_cycle_choice','IUACAAS')
    app.selectbox(key='clim_rule_choice').set_value('climate-demo-b01:IUACAAS:v1').run()
    app.radio(key='clim_metric_filter').set_value('temperature').run()
    tasks['BR-01'] = str(app.selectbox(key='clim_episode_choice').options[0])
    assert '1180,00p' in tasks['BR-01']
    app.radio(key='screen_choice').set_value('🧪 EC/pH drain (BR-03)').run()
    choose_cycle('drain_cycle_choice','Reference')
    app.selectbox(key='drain_rule_choice').set_value('drain-demo-b03:Reference:v1').run()
    app.radio(key='drain_metric_filter').set_value('ec').run()
    tasks['BR-03'] = str(app.selectbox(key='drain_episode_choice').options[0])
    assert '80400 phút' in tasks['BR-03']
    app.radio(key='screen_choice').set_value('🍅 Thu hoạch (BR-05)').run()
    choose_cycle('harv_cycle_choice','Digilog')
    app.date_input(key='harv_window').set_value((date(2020,2,19),date(2020,2,19))).run()
    options = app.selectbox(key='harv_rule_choice').options
    app.selectbox(key='harv_rule_choice').select_index(next(i for i,v in enumerate(options) if 'harvest-demo-b10:Digilog:v2' in str(v))).run()
    values = {m.label:m.value for m in app.metric}
    assert [values[k] for k in ('A observed','B observed','Share B (paired)')] == ['0,05 kg/m²','0,10 kg/m²','66,67 %']
    tasks['BR-05'] = values
    app.radio(key='screen_choice').set_value('💶 Chi phí & Kịch bản (BR-06)').run()
    options = app.selectbox(key='cost_scenario_select').options
    app.selectbox(key='cost_scenario_select').select_index(next(i for i,v in enumerate(options) if 'Offline restore electricity +20%' in str(v))).run()
    assert not app.exception and not app.error
    values = {m.label:m.value for m in app.metric}
    assert values['Baseline (EUR)']=='17.60 EUR' and values['Kịch bản (EUR)']=='20.84 EUR'
    tasks['BR-06'] = values
    result = {'kind': 'automated-ui-walkthrough', 'human_participants': 0,
              'database_read_only': True, 'publication_id': publication_id,
              'scope_id': context['scope_id'], 'snapshot_id': context['snapshot_id'],
              'cycle_id': context['cycle_id'], 'compartment_id': context['compartment_id'],
              'window_inclusive': [day.isoformat() for day in window],
              'raw_water_oracle': {'source': relative, 'sha256': expected_hash, 'rows': len(source_rows),
                                   'irrigation_l_m2': str(irrigation), 'drain_l_m2': str(drain),
                                   'paired_ratio_percent': str(ratio), 'assumed_area_m2': str(area_m2),
                                   'energy_kwh': str(energy_kwh), 'historical_cost_eur': str(cost_eur),
                                   'tariff': {'heat_eur_mj': '0.0083', 'high_eur_kwh': '0.08',
                                              'low_eur_kwh': '0.04'}},
              'total_elapsed_seconds': round(perf_counter() - started, 3),
              'screens': evidence, 'configured_rule_and_scenario_tasks': tasks}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(result, indent=2, ensure_ascii=False))
