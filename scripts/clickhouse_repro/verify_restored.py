"""Check all configuration DTOs and actual historical/replay UI after recovery.

Sequential native read actions use the production guarded process proof cache;
each action still validates current physical stamps and the complete registry.
"""
import argparse
import json
import os
import runpy
import sys

from common import STATE, historical_id, write_json

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--mode', choices=('physical', 'rebuilt'), required=True)
args = parser.parse_args()
sys.argv = ['verify_configurations.py', '--mode', args.mode, '--archive', '/state/native-state.json']
runpy.run_path('/repro/verify_configurations.py', run_name='__main__')
os.environ['GREENHOUSE_WALKTHROUGH_PUBLICATION'] = historical_id()
os.environ['GREENHOUSE_WALKTHROUGH_OUTPUT'] = '/state/historical-ui.json'
ui = runpy.run_path('/repro/check_ui.py', run_name='__main__')
app = ui['app']
app.radio(key='screen_choice').set_value('🧪 EC/pH drain (BR-03)').run()
ui['choose_cycle']('drain_cycle_choice', 'Automatoes')
archive = json.loads((STATE / 'native-state.json').read_text())['archive']
profiles = [row for row in archive['configurations']['rows'] if row['kind'] == 'rule_schedule'
    and row['version_id'].startswith('drain-user:') and ':offline-restore:v1' in row['version_id']]
assert len(profiles) == 1
version = profiles[0]['version_id']
options = app.selectbox(key='drain_rule_choice').options
app.selectbox(key='drain_rule_choice').select_index(next(i for i, label in enumerate(options) if version in str(label))).run()
assert not app.exception and not app.error
assert app.selectbox(key='drain_rule_choice').value == version
assert any('user_unverified' in str(label) for label in options)
write_json(STATE / 'native-profile-ui.json', dict(status='passed', mode=args.mode,
    rule_version=version, explicitly_selected=True, user_unverified_label=True))
os.environ['GREENHOUSE_SENSOR_RESULT'] = 'restore-result.json' if args.mode == 'physical' else 'rebuild-result.json'
runpy.run_path('/repro/check_sensor_ui.py', run_name='__main__')
print(json.dumps(dict(status='passed', mode=args.mode, historical_screens=8,
    custom_drain_profile=True, custom_cost_scenario=True, replay_ui=True)), flush=True)
