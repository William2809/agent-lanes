"""Normalize runner receipts and estimate only comparable, completed work."""
import hashlib
import json
import math


def key(project, command_digest, runner_class, toolchain_digest, phase, cache, workers):
    # Digests represent commands/toolchains; no command text enters telemetry.
    values = [project, command_digest, runner_class, toolchain_digest, phase, cache, workers]
    return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()


def number(value):
    return type(value) in (int, float) and 0 <= value <= 10**18 and math.isfinite(value)


def normalize(row):
    """Accept the receipt lane's nested schema and the scheduler's flat schema.

    Legacy rows without an exact profile stay useful observations, not evidence
    for fast-lane classification. Phase RSS is a sampled upper proxy, not USS.
    """
    phases = row.get('phases') or {}
    peaks = [p.get('peak_rss_bytes') for p in phases.values() if isinstance(p, dict)]
    peaks = [v / 1048576 for v in peaks if number(v)]
    flat = row.get('peak_rss_mb')
    if number(flat):
        peaks.append(flat)
    workers = (row.get('granted') or {}).get('workers', row.get('workers'))
    duration = row.get('duration_s', row.get('duration_seconds'))
    check = phases.get('check', {})
    if number(check.get('duration_seconds')):
        duration = check['duration_seconds']
    complete = row.get('complete', row.get('complete_observation', False)) is True
    complete = complete and row.get('exit_code') == 0 and not any(
        row.get(k) for k in ('incomplete', 'killed', 'cached'))
    cache = (row.get('cache') or {}).get('tree', {})
    if isinstance(cache, dict) and cache.get('outcome') in ('hit', 'follower'):
        complete = False
    return dict(profile=row.get('profile'), project=row.get('project'),
                mode='run' if row.get('mode') == 'targeted' else row.get('mode'),
                workers=workers, duration_s=duration if number(duration) else None,
                peak_rss_mb=max(peaks) if peaks else None, complete=complete)


def estimate(rows, profile, memory_floor_mb, min_samples=5, fast_limit_s=60):
    """No downward budget adjustment from missing, failed or censored samples."""
    if memory_floor_mb <= 0 or min_samples < 1:
        raise ValueError('positive model limits required')
    samples, memory_bounds = [], []
    for raw in rows:
        try:
            row = normalize(raw)
            if row['profile'] != profile:
                continue
            if row['peak_rss_mb']:
                # A killed run is censored duration, but its observed memory is
                # still a lower bound. Ignoring it can repeat the same OOM.
                memory_bounds.append(row['peak_rss_mb'])
            if row['complete'] and row['duration_s'] is not None and row['peak_rss_mb']:
                samples.append(row)
        except (AttributeError, TypeError, ValueError):
            continue
    samples = samples[-100:]
    known = len(samples) >= min_samples
    durations = sorted(r['duration_s'] for r in samples)
    p90 = durations[math.ceil(len(durations) * .9)-1] if known else None
    memory = max([memory_floor_mb] + [math.ceil(peak * 1.25 + 256) for peak in memory_bounds[-100:]])
    return dict(samples=len(samples), profile_known=known, predicted_s=p90, estimate_scope='check',
                predicted_mb=memory, fast=bool(known and p90 <= fast_limit_s))


def observations(events):
    """Join bounded telemetry by phase and profile; aborted/missing phases don't train."""
    rows = {}
    for event in events:
        if event.get('phase') != 'check' or not event.get('profile'):
            continue
        identity = event.get('run_id'), event['profile']
        row = rows.setdefault(identity, dict(profile=event['profile'], complete=False, peak_rss_mb=0))
        row['peak_rss_mb'] = max(row['peak_rss_mb'], event.get('peak_rss_mb', 0))
        if event.get('event') == 'phase-ended':
            row.update(duration_s=event.get('duration_s'), exit_code=event.get('exit_code'))
        elif event.get('event') == 'cleanup':
            row['clean'] = event.get('exit_code') == 0
        elif event.get('event') == 'stopped':
            row['killed'] = True
    for row in rows.values():
        row['complete'] = row.get('clean') is True and row.get('exit_code') == 0 and not row.get('killed')
    return list(rows.values())
