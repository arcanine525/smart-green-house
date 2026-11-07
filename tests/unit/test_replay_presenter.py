import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace

import pytest

from greenhouse.app.replay_presenter import episode_rows, export_csv, liveness, summary_rows


def evaluation():
    basis = SimpleNamespace(value='measured')
    metric = SimpleNamespace(metric='ec', basis=basis, unit='dS/m', expected_minutes=10, valid_minutes=5,
        unknown_minutes=5, out_minutes=0, out_hours=None, coverage_pct=50, rule_version='rule-v1')
    episode = SimpleNamespace(metric='ec', basis=basis, original_start=datetime(2019,12,16),
        original_end=datetime(2019,12,16,0,10), original_minutes=10, peak_deviation=1, rule_version='rule-v1')
    climate = SimpleNamespace(metric_results=(), episodes=(), effective_start=datetime(2019,12,16))
    drain = SimpleNamespace(metric_results=(metric,), episodes=(episode,))
    return SimpleNamespace(publication_id='pub-1', replay_id='replay-1', scope_id='scope-1',
        quality_policy_version='policy', provisional=True, observed_through=datetime(2019,12,16,0,5),
        window_end=datetime(2019,12,16,0,10), climate_rule_version=None, drain_rule_version='rule-v1',
        climate=climate, drain=drain)


def test_liveness_uses_operational_time_and_future_is_unknown():
    now = datetime(2026,1,1,tzinfo=timezone.utc)
    assert liveness(now - timedelta(seconds=60), now, 60) == ('active', 60)
    assert liveness(now - timedelta(seconds=61), now, 60) == ('stale', 61)
    assert liveness(now + timedelta(seconds=1), now, 60) == ('unknown', None)
    assert liveness(None, now, 60) == ('unknown', None)
    with pytest.raises(ValueError):
        liveness(datetime(2026,1,1), now, 60)


def test_export_matches_visible_results_and_pinned_identity():
    value = evaluation()
    assert summary_rows(value)[0]['out_minutes'] is None
    assert episode_rows(value)[0]['state'] == 'open_provisional'
    rows = list(csv.DictReader(StringIO(export_csv(value).decode())))
    assert [r['row_type'] for r in rows] == ['summary', 'episode']
    assert all(r['publication_id'] == 'pub-1' and r['scope_id'] == 'scope-1' for r in rows)
    assert rows[0]['out_minutes'] == '' and rows[1]['state'] == 'open_provisional'
    assert rows[0]['basis'] == rows[1]['basis'] == 'measured'
    assert rows[0]['window_start'] == '2019-12-16 00:00:00'
