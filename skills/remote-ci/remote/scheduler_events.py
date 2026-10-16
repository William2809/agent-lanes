"""Emit bounded scheduler transitions after the queue transaction is released."""
import math

import ci_telemetry


BLOCKERS = {
    'project-slot': 'project-slot',
    'project-reserve': 'project-reserve',
    'fast-reserve': 'fast-reserve',
    'head-drain': 'head-drain',
    'aged-drain': 'aged-drain',
    'pressure': 'pressure',
    'swap-recovery': 'swap',
    'jobs': 'jobs',
    'workers': 'workers',
    'cpu': 'cpu',
    'memory': 'memory',
}


def _reason(row):
    blocker = row.get('blocker')
    raw = blocker.get('reason') if isinstance(blocker, dict) else None
    if isinstance(raw, str) and raw.startswith('lease:'):
        return 'lease'
    return BLOCKERS.get(raw, 'unknown') if raw else None


def summary(row):
    """Keep only state needed to suppress repeated polling events."""
    if not isinstance(row, dict):
        return None
    state = row.get('state')
    return (state, _reason(row) if state == 'queued' else None)


def _number(fields, key, value):
    if type(value) in (int, float) and 0 <= value <= 10**18 and math.isfinite(value):
        fields[key] = value


def emit_transition(root, previous, row, metrics=None, now=None):
    """Emit this job's new queue state and return its privacy-safe summary."""
    current = summary(row)
    if current is None or current == previous:
        return current
    identity = dict(run_id=row.get('run_id', ''), project=row.get('project', ''))
    if previous is None:
        ci_telemetry.emit(root / 'telemetry', 'queued', now=now, **identity)
    state, reason = current
    if state == 'queued' and reason:
        prior_reason = previous[1] if previous and previous[0] == 'queued' else None
        if reason != prior_reason:
            ci_telemetry.emit(root / 'telemetry', 'blocked', now=now,
                              reason=reason, phase='queue', **identity)
    elif state == 'running' and (not previous or previous[0] != 'running'):
        fields = dict(identity, lane_class=row.get('lane_class', 'normal'))
        requested = row.get('requested', {})
        _number(fields, 'workers', row.get('workers', requested.get('workers')))
        _number(fields, 'memory_mb', row.get('memory_mb', requested.get('memory_mb')))
        _number(fields, 'predicted_s', row.get('predicted_s'))
        _number(fields, 'samples', row.get('samples'))
        _number(fields, 'wait_s', (now - row['submitted_at'])
                if type(now) in (int, float) and type(row.get('submitted_at')) in (int, float) else None)
        if isinstance(metrics, dict):
            _number(fields, 'host_load', metrics.get('load'))
            _number(fields, 'available_mb', metrics.get('available_mb'))
        ci_telemetry.emit(root / 'telemetry', 'admitted', now=now, **fields)
    return current
