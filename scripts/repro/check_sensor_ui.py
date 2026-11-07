"""Verify the restored AGC replay is usable through the production dashboard."""
import importlib.resources
import json
import os
from pathlib import Path
import psycopg
from streamlit.testing.v1 import AppTest
from greenhouse.settings import Settings
from greenhouse.db.replay_catalog import current_replays

assert 'default_transaction_read_only=on' in os.environ.get('PGOPTIONS','')
evidence=Path('/evidence')
expected=json.loads((evidence/'sensor-restore.json').read_text())
with importlib.resources.as_file(importlib.resources.files('greenhouse.app')/'home.py') as source:
    app=AppTest.from_file(str(source),default_timeout=180).run()
    app.radio(key='screen_choice').set_value('📡 Sensor replay (BR-01/03)').run()
    assert not app.exception and not app.error
    key=app.selectbox(key='replay_publication').value
    with psycopg.connect(**Settings.from_env().connection_kwargs()) as connection:
        assert connection.execute('SHOW transaction_read_only').fetchone()[0]=='on'
        selected=next(row for row in current_replays(connection) if (row[2],row[0],row[4])==key)
    assert selected[0]==expected['replay_id'] and selected[1]==expected['publication_id']
    assert any(expected['publication_id'] in caption.value for caption in app.caption)
    assert selected[7]['complete'] and selected[7]['accepted_events']==30
    app.selectbox(key='replay_climate_rule').set_value('climate-demo-b01:Automatoes:v1').run()
    app.selectbox(key='replay_drain_rule').set_value('drain-demo-b03:Automatoes:v1').run()
    assert not app.exception and not app.error
    assert len(app.download_button)==1
    assert len(app.get('plotly_chart'))==4
    result={'status':'passed','replay_id':selected[0],'publication_id':selected[1],
        'accepted_events':30,'complete':True,'climate_rule':'climate-demo-b01:Automatoes:v1',
        'drain_rule':'drain-demo-b03:Automatoes:v1','charts':4,'csv_export_available':True}
(evidence/'sensor-ui.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
