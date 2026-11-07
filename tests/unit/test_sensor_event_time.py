from datetime import datetime
from decimal import Decimal

import pytest

from greenhouse.sensor.event_time import event_slot, normalize_cursor, slot_from_serial


def test_excel_serial_residual_uses_same_slot_as_batch():
    serial = '43815.0034722222'
    raw = datetime(2019, 12, 16, 0, 4, 59, 999998)
    expected = datetime(2019, 12, 16, 0, 5)
    assert slot_from_serial(serial) == expected
    assert normalize_cursor(raw.isoformat()) == expected
    event = {'source_observed_at': raw.isoformat(), 'source_provenance': {
        'source_time_serial': serial, 'slot_start': expected.isoformat()}}
    assert event_slot(event) == expected


def test_off_grid_and_mismatched_provenance_rejected():
    with pytest.raises(ValueError, match='tolerance'):
        normalize_cursor('2019-12-16T00:01:00')
    with pytest.raises(ValueError, match='tolerance'):
        slot_from_serial(str(Decimal(43815) + Decimal(60) / Decimal(86400)))
    with pytest.raises(ValueError, match='different slots'):
        event_slot({'source_observed_at': '2019-12-16T00:05:00',
                    'source_provenance': {'source_time_serial': '43815'}})
