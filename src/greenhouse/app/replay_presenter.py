"""Pure replay presentation helpers shared by screen and CSV export."""
import csv
from datetime import datetime, timezone
from io import StringIO


def liveness(received_at, now, stale_seconds):
    if received_at is None:
        return 'unknown', None
    if received_at.tzinfo is None or now.tzinfo is None or stale_seconds <= 0:
        raise ValueError('aware timestamps and a positive stale threshold are required')
    age = (now.astimezone(timezone.utc) - received_at.astimezone(timezone.utc)).total_seconds()
    if age < 0:
        return 'unknown', None
    return ('stale' if age > stale_seconds else 'active'), age


def summary_rows(evaluation):
    rows = []
    for group, result in (('climate', evaluation.climate), ('drain', evaluation.drain)):
        for metric in result.metric_results:
            rows.append({'group': group, 'metric': metric.metric, 'basis': metric.basis.value, 'unit': metric.unit,
                'expected_minutes': metric.expected_minutes, 'valid_minutes': metric.valid_minutes,
                'unknown_minutes': metric.unknown_minutes, 'out_minutes': metric.out_minutes if metric.out_hours is not None else None,
                'coverage_pct': metric.coverage_pct, 'rule_version': metric.rule_version})
    return rows


def episode_rows(evaluation):
    rows = []
    for group, result in (('climate', evaluation.climate), ('drain', evaluation.drain)):
        for episode in result.episodes:
            state = ('open_provisional' if evaluation.provisional and episode.original_end >= evaluation.window_end
                     else 'provisional' if evaluation.provisional else 'closed')
            rows.append({'group': group, 'metric': episode.metric, 'basis': episode.basis.value, 'start': episode.original_start,
                'end': episode.original_end, 'minutes': episode.original_minutes,
                'peak_deviation': episode.peak_deviation, 'rule_version': episode.rule_version, 'state': state})
    return rows


def export_csv(evaluation):
    """Serialize exactly the visible summaries/episodes with pinned provenance."""
    fields = ('publication_id', 'replay_id', 'scope_id', 'quality_policy_version', 'provisional',
              'observed_through', 'window_start', 'window_end', 'climate_rule_version', 'drain_rule_version',
              'row_type', 'group', 'metric', 'basis', 'unit', 'expected_minutes', 'valid_minutes',
              'unknown_minutes', 'out_minutes', 'coverage_pct', 'rule_version',
              'start', 'end', 'minutes', 'peak_deviation', 'state')
    output = StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    common = {key: getattr(evaluation, key) for key in fields[:10] if key != 'window_start'}
    common['window_start'] = evaluation.climate.effective_start
    for kind, data in (('summary', summary_rows(evaluation)), ('episode', episode_rows(evaluation))):
        for row in data:
            writer.writerow(common | {'row_type': kind} | row)
    return output.getvalue().encode('utf-8')
