"""Known-answer regressions for the independent raw evidence oracle."""
from datetime import datetime, timedelta
from decimal import Decimal

from greenhouse.evaluation.br03 import raw_measure, raw_oracle, read_raw


def test_shared_outage_null_lexemes_keep_missing_separate_from_invalid(tmp_path):
    path = tmp_path / 'GreenhouseClimate.csv'
    path.write_text(
        '%time,EC_drain_PC,pH_drain_PC\n'
        '43831,3,6\n'
        '43831.00347,NaN,NA\n'
        '43831.00694,N/A,\n'
        '43831.01042,-1,0\n'
        '43831.01389,Infinity,6\n')
    start = datetime(2020, 1, 1)
    end = start + timedelta(minutes=25)
    rows = read_raw(path, start, end)
    result = raw_oracle(rows, start, end)
    assert result['metrics']['ec']['minutes_by_class'] == {'inside': 5, 'missing': 10, 'invalid': 10}
    assert result['metrics']['ph']['minutes_by_class'] == {'inside': 10, 'missing': 10, 'suspect': 5}
    assert result['joint']['jointly_valid_minutes'] == 5
    assert result['joint']['joint_out_minutes'] == 0
    assert rows[start + timedelta(minutes=5)]['reasons'] == {'ec': 'nan_token', 'ph': 'na_token'}
    assert rows[start + timedelta(minutes=10)]['reasons'] == {'ec': 'n_a_token', 'ph': 'empty'}
    assert raw_measure(' -1 ', 'ec') == (Decimal('-1'), 'invalid', 'negative_quantity')
    assert raw_measure('0', 'ph') == (Decimal('0'), 'suspect', 'demo_zero_ph_suspect')
