"""Run the committed full AGC backfill and persist its actual publication ID."""
import argparse
import sys

from common import COORDINATOR, STATE, connect, write_json
from greenhouse.sensor.simulator import STANDARD_AGC_SHA

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--workflow', required=True)
args = parser.parse_args()
client = connect()
client.close()
sys.path.insert(0, '/migration')
import backfill
sys.argv = ['backfill.py', '--metadata', str(STATE / 'source-metadata.json'),
    '--raw', '/data/raw/agc-2nd-2019/' + STANDARD_AGC_SHA,
    '--archive', '/data/import/autonomous-greenhouse-challengeagc-2nd-2019.zip',
    '--output', str(STATE / 'output'), '--coordinator', str(COORDINATOR),
    '--workflow-id', args.workflow]
result = backfill.main()
write_json(STATE / 'historical.json', result)
