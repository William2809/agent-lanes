#!/usr/bin/env python3
"""Bounded, privacy-preserving scheduler estimates from runner receipts."""
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import sys


MAX_RECEIPTS = 100
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_NUMBER = 10 ** 18
PROJECT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def key(project, command, context, workers, database=False):
    """Return an exact profile without persisting command or context text."""
    values = [project, command, context, workers, bool(database)]
    return hashlib.sha256(json.dumps(values, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def context(config_file, kit):
    """Hash the snapshotted kit itself; older clients omit its VERSION file."""
    digest = hashlib.sha256()
    for path in sorted(Path(kit).iterdir()):
        if path.suffix in ('.py', '.sh') and path.is_file():
            digest.update(path.name.encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(hashlib.sha256(Path(config_file).read_bytes()).digest())
    digest.update(json.dumps([sys.platform, os.uname().machine,
                             list(sys.version_info[:3])]).encode())
    return digest.hexdigest()


def _number(value):
    return (type(value) in (int, float) and 0 <= value <= MAX_NUMBER and
            math.isfinite(value))


def _files(directory):
    try:
        def entries(stream):
            for entry in stream:
                try:
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith('.json'):
                        stat = entry.stat(follow_symlinks=False)
                        yield stat.st_mtime_ns, entry.name, Path(entry.path)
                except OSError:
                    continue
        with os.scandir(directory) as stream:
            latest = heapq.nlargest(MAX_RECEIPTS, entries(stream), key=lambda item: item[:2])
        return [item[2] for item in reversed(latest)]
    except OSError:
        return []


def _read(path):
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_RECEIPT_BYTES + 1)
        if len(data) > MAX_RECEIPT_BYTES:
            return None
        row = json.loads(data)
        return row if isinstance(row, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def _observation(row, project, profile):
    if row.get('project') != project or row.get('scheduler_profile') != profile:
        return None
    granted = row.get('granted')
    phases = row.get('phases')
    cache = row.get('cache')
    workers = granted.get('workers') if isinstance(granted, dict) else None
    if type(workers) is not int or workers < 1:
        return None
    if not isinstance(phases, dict) or not isinstance(cache, dict):
        return None
    tree = cache.get('tree')
    if not isinstance(tree, dict) or not isinstance(tree.get('outcome'), str):
        return None

    peaks = []
    for phase in phases.values():
        if not isinstance(phase, dict):
            return None
        peak = phase.get('peak_rss_bytes')
        if peak is not None:
            if not _number(peak):
                return None
            peaks.append(peak)

    ended = row.get('ended_at')
    started = row.get('started_at')
    terminal = ended is not None or row.get('exit_code') is not None or row.get('classification') is not None
    failed = terminal and (row.get('exit_code') != 0 or row.get('killed') is True or
                           row.get('incomplete') is True or
                           row.get('complete_observation') is not True or
                           row.get('classification') in ('killed', 'superseded', 'infra', 'busy'))
    cached = tree.get('outcome') in ('hit', 'follower')
    cleanup = phases.get('cleanup')
    queue = phases.get('queue')
    check = phases.get('check')
    duration = None
    if (not failed and not cached and isinstance(cleanup, dict) and
            isinstance(queue, dict) and isinstance(check, dict) and
            _number(check.get('duration_seconds')) and
            _number(cleanup.get('duration_seconds')) and
            _number(started) and _number(ended) and
            _number(queue.get('duration_seconds')) and ended >= started):
        duration = ended - started - queue['duration_seconds']
        if not _number(duration):
            duration = None
    success = duration is not None and bool(peaks)
    order = ended if _number(ended) else started if _number(started) else -1
    return dict(success=success, failed=failed, peak=max(peaks) if peaks else None,
                duration=duration, order=order)


def read_model(root, project, profile, memory_floor_mb, min_samples=5, fast_limit_s=60):
    """Read at most 100 receipts and return a conservative whole-job estimate."""
    if not isinstance(project, str) or not PROJECT.fullmatch(project) or not isinstance(profile, str):
        raise ValueError('project and profile must be strings')
    if (type(memory_floor_mb) not in (int, float) or memory_floor_mb <= 0 or
            not math.isfinite(memory_floor_mb)):
        raise ValueError('positive memory floor required')
    if type(min_samples) is not int or min_samples < 1:
        raise ValueError('positive sample count required')
    if not _number(fast_limit_s):
        raise ValueError('invalid fast limit')

    rows = []
    for path in _files(Path(root) / 'receipts' / project):
        raw = _read(path)
        if raw is None:
            continue
        observation = _observation(raw, project, profile)
        if observation is not None:
            rows.append(observation)

    rows.sort(key=lambda row: row['order'])
    last_failure = max((index for index, row in enumerate(rows) if row['failed']), default=-1)
    successes = [row for row in rows[last_failure + 1:] if row['success']]
    durations = [row['duration'] for row in successes[-20:]]
    memory = [row['peak'] for row in rows if row['peak'] is not None]
    known = len(successes) >= min_samples
    predicted_s = max(durations) * 1.25 if known and durations else None
    predicted_mb = int(math.ceil(max([memory_floor_mb] +
                                     [peak / 1048576 * 1.25 + 256 for peak in memory])))
    return dict(profile=profile, profile_known=known, predicted_s=predicted_s,
                predicted_mb=predicted_mb, samples=len(successes), estimate_scope='job',
                source='receipts' if rows else 'unknown')


def authorize(request, root, limits):
    """Replace caller claims with an estimate derived from trusted receipts."""
    result = dict(request)
    workers = request.get('workers')
    requested = request.get('memory_mb')
    worker_mb = limits.get('WORKER_MB')
    maximum = limits.get('MAX_JOB_MB')
    if (type(workers) is not int or workers < 1 or type(requested) is not int or
            requested < 1 or type(worker_mb) is not int or worker_mb < 1 or
            type(maximum) is not int or maximum < 1):
        raise ValueError('invalid profile memory limits')

    floor = worker_mb * workers
    context = request.get('profile_context')
    command = request.get('command_digest')
    project = request.get('project')
    framed = (isinstance(project, str) and PROJECT.fullmatch(project) and
              isinstance(context, str) and DIGEST.fullmatch(context) and
              isinstance(command, str) and DIGEST.fullmatch(command))
    database = any(isinstance(lease, str) and lease.startswith('postgres:')
                   for lease in request.get('leases', ()))
    service = 2048 if database else 0
    if framed:
        profile = key(project, command, context, workers, database)
        model = read_model(root, project, profile, floor,
                           fast_limit_s=limits.get('FAST_SECONDS', 60))
    else:
        model = dict(profile=None, profile_known=False, predicted_s=None,
                     predicted_mb=floor, samples=0, estimate_scope='job', source='unknown')

    memory = max(requested, model['predicted_mb'] + service,
                 floor + service, 4096 + service if not model['profile_known'] else 0)
    if memory > maximum:
        raise ValueError('authorized memory exceeds maximum job budget')
    result.update(model)
    result['memory_mb'] = int(math.ceil(memory))
    return result
