"""Bounded, best-effort numeric events; never serialize commands or environment."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import time

SCHEMA = 'agent-ci.telemetry/v1'
EVENTS = {'queued', 'blocked', 'admitted', 'stopped', 'cleanup', 'memory-granted', 'memory-released'}
REASONS = {'fifo', 'jobs', 'workers', 'cpu', 'memory', 'pressure', 'project-slot',
           'memory-growth', 'memory-growth-reserve', 'project-reserve', 'fast-reserve', 'head-drain', 'aged-drain', 'swap',
           'lease', 'deadline', 'memory-budget', 'owner-lost', 'interrupted',
           'monitor-unavailable', 'cleanup-incomplete', 'quarantine-expired', 'complete', 'unknown'}
PHASES = {'queue', 'checkout', 'install', 'template', 'database', 'prepare', 'check', 'cleanup', 'unknown'}
NUMERIC = {'wait_s', 'workers', 'memory_mb', 'exit_code', 'predicted_s', 'samples',
           'host_load', 'available_mb', 'previous_mb', 'ceiling_mb'}
LANES = {'fast', 'normal'}
MAX_BYTES = 2 * 1024 * 1024


def record(event, fields, now=None):
    if event not in EVENTS:
        raise ValueError('unknown telemetry event')
    stamp = time.time() if now is None else now
    if type(stamp) not in (int, float) or not 0 <= stamp <= 10**18 or not math.isfinite(stamp):
        raise ValueError('invalid timestamp')
    row = dict(schema=SCHEMA, event=event, at=stamp)
    for key, value in fields.items():
        if key in NUMERIC:
            if type(value) not in (int, float) or not 0 <= value <= 10**18 or not math.isfinite(value):
                raise ValueError('invalid telemetry number')
            if key == 'samples' and type(value) is not int:
                raise ValueError('invalid telemetry sample count')
        elif key in ('run_id', 'project'):
            if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,160}', value):
                raise ValueError('invalid telemetry identifier')
        elif key == 'reason':
            if value not in REASONS:
                raise ValueError('invalid telemetry reason')
        elif key == 'phase':
            if value not in PHASES:
                raise ValueError('invalid telemetry phase')
        elif key == 'lane_class':
            if value not in LANES:
                raise ValueError('invalid telemetry lane')
        else:
            raise ValueError('unknown telemetry field')
        row[key] = value
    return row


def emit(root, event, now=None, **fields):
    """At most 100ms waiting and two 2MiB generations. Failure never changes CI."""
    try:
        row = record(event, fields, now)
        encoded = (json.dumps(row, separators=(',', ':'), allow_nan=False) + '\n').encode()
        if len(encoded) > 4096:
            return False
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(root / 'events.lock', flags, 0o600), 'wb') as lock:
            deadline = time.monotonic() + 0.1
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.01)
            path = root / 'events.jsonl'
            if path.exists() and path.stat().st_size + len(encoded) > MAX_BYTES:
                path.replace(root / 'events.previous.jsonl')
            with os.fdopen(os.open(path, flags | os.O_APPEND, 0o600), 'ab') as stream:
                stream.write(encoded)
        return True
    except (OSError, ValueError, TypeError):
        return False


def read(root):
    """Bound reads too: a corrupt/external file must not grow the reader's work."""
    for name in ('events.previous.jsonl', 'events.jsonl'):
        try:
            with (Path(root) / name).open('rb') as stream:
                data = stream.read(MAX_BYTES + 4096)
            for line in data.splitlines():
                try:
                    row = json.loads(line)
                    if row.get('schema') != SCHEMA:
                        continue
                    fields = {k: v for k, v in row.items() if k not in ('schema', 'event', 'at')}
                    yield record(row['event'], fields, row['at'])
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
        except OSError:
            continue


def summary(root):
    counts, reasons, waits = {}, {}, []
    for row in read(root):
        counts[row['event']] = counts.get(row['event'], 0) + 1
        if row['event'] == 'blocked':
            reason = row.get('reason', 'unknown')
            reasons[reason] = reasons.get(reason, 0) + 1
        if row['event'] == 'admitted' and 'wait_s' in row:
            waits.append(row['wait_s'])
    waits.sort()
    return dict(schema=SCHEMA, events=counts, blocker_transitions=reasons,
                admitted_samples=len(waits), wait_p95_s=waits[math.ceil(len(waits)*.95)-1] if waits else None,
                retention='two bounded generations; not lifetime totals')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    print(json.dumps(summary(args.root), sort_keys=True))
