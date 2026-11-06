import importlib.resources
import json
from datetime import date
from pathlib import Path
import os
import psycopg
from greenhouse.settings import Settings
from streamlit.testing.v1 import AppTest

if 'default_transaction_read_only=on' not in os.environ.get('PGOPTIONS',''):
    raise RuntimeError('read only required')
with psycopg.connect(**Settings.from_env().connection_kwargs()) as connection:
    if connection.execute('SHOW transaction_read_only').fetchone()[0] != 'on':
        raise RuntimeError('PostgreSQL session is not read-only')

def cycle(app, key, compartment):
    options=app.selectbox(key=key).options
    matches=[i for i, label in enumerate(options) if compartment in str(label)]
    assert len(matches)==1, (key, options)
    app.selectbox(key=key).select_index(matches[0]).run()
    check(app)

def check(app):
    assert not app.exception, [str(i.value) for i in app.exception]
    assert not app.error, [str(i.value) for i in app.error]

def metric(app, name):
    return next(item.value for item in app.metric if item.label==name)

result={'kind':'automated_facilitator_task_verification','human_participants':0,'database_read_only':True,
        'publication':'df3e4bcbacab45a5b86fc37d2f53c00a','tasks':{}}
with importlib.resources.as_file(importlib.resources.files('greenhouse.app') / 'home.py') as path:
    app=AppTest.from_file(str(path),default_timeout=240).run()
    assert app.selectbox(key='pub_choice').value.publication_id==result['publication']
    app.radio(key='screen_choice').set_value('🌡️ Khí hậu (BR-01)').run(); cycle(app,'clim_cycle_choice','IUACAAS')
    app.selectbox(key='clim_rule_choice').set_value('climate-demo-b01:IUACAAS:v1').run(); check(app)
    app.radio(key='clim_metric_filter').set_value('temperature').run(); check(app)
    first=str(app.selectbox(key='clim_episode_choice').options[0])
    assert '1180,00p' in first and '2020-05-21T11:45:00' in first and '2020-05-22T07:25:00' in first, first
    result['tasks']['BR-01']=first
    app.radio(key='screen_choice').set_value('💧 Nước (BR-02)').run(); cycle(app,'cycle_choice','Automatoes')
    app.date_input(key='window').set_value((date(2020,3,2),date(2020,3,3))).run(); check(app)
    values={k:metric(app,k) for k in ('Irrigation observed','Drain observed','Tỷ lệ drain (paired)','Độ phủ paired')}
    assert list(values.values())==['9,36 L/m²','3,89 L/m²','41,56 %','100,00 %'], values
    result['tasks']['BR-02']=values
    app.radio(key='screen_choice').set_value('🧪 EC/pH drain (BR-03)').run(); cycle(app,'drain_cycle_choice','Reference')
    app.selectbox(key='drain_rule_choice').set_value('drain-demo-b03:Reference:v1').run(); check(app)
    app.radio(key='drain_metric_filter').set_value('ec').run(); check(app)
    first=str(app.selectbox(key='drain_episode_choice').options[0])
    assert '80400 phút' in first and '2019-12-31T12:35:00' in first and '2020-02-25T08:35:00' in first,first
    result['tasks']['BR-03']=first
    app.radio(key='screen_choice').set_value('⚡ Năng lượng (BR-04)').run(); cycle(app,'energy_cycle','Automatoes')
    app.date_input(key='energy_window').set_value((date(2020,3,2),date(2020,3,3))).run(); check(app)
    values={k:metric(app,k) for k in ('Năng lượng (kWh)','Sản lượng A (kg)','kWh/kg A')}
    assert list(values.values())==['308.078','0.000','NULL'], values
    result['tasks']['BR-04']=values
    app.radio(key='screen_choice').set_value('🍅 Thu hoạch (BR-05)').run(); cycle(app,'harv_cycle_choice','Digilog')
    app.date_input(key='harv_window').set_value((date(2020,2,19),date(2020,2,19))).run(); check(app)
    opts=app.selectbox(key='harv_rule_choice').options
    app.selectbox(key='harv_rule_choice').select_index(next(i for i,x in enumerate(opts) if 'harvest-demo-b10:Digilog:v2' in str(x))).run(); check(app)
    values={k:metric(app,k) for k in ('A observed','B observed','Share B (paired)')}
    assert list(values.values())==['0,05 kg/m²','0,10 kg/m²','66,67 %'], values
    result['tasks']['BR-05']=values
    app.radio(key='screen_choice').set_value('💶 Chi phí & Kịch bản (BR-06)').run(); check(app)
    opts=app.selectbox(key='cost_scenario_select').options
    app.selectbox(key='cost_scenario_select').select_index(next(i for i,x in enumerate(opts) if 'Benchmark Scenario Hybrid' in str(x))).run(); check(app)
    values={k:metric(app,k) for k in ('Baseline (EUR)','Kịch bản (EUR)','Kịch bản (EUR/m²)')}
    assert list(values.values())==['1,161.25 EUR','1,175.15 EUR','18.80 EUR/m²'], values
    result['tasks']['BR-06']=values
Path(os.environ['AGC_STUDY_OUTPUT']).write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
