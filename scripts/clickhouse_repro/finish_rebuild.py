"""After full parity, map native scenarios and rebuild replay sequentially.

Use the processing image. UI verification is a separate later stage in its
canonical UI image; never overlap their cold proofs on the limited host.
"""
from argparse import Namespace
from pathlib import Path

from common import STATE
from greenhouse.sensor.simulator import STANDARD_AGC_SHA
from native_state import import_scenarios

import_scenarios(Namespace(archive=STATE/'native-state.json', proof=STATE/'parity/parity.json',
    raw=Path('/data/raw/agc-2nd-2019')/STANDARD_AGC_SHA))
from agc_sensor import rebuild
rebuild()
