"""Host scheduler v2. All ledger decisions run under Queue.transaction().

Backfill reserves the head's full request, after only its mandatory blockers
leave. It uses no duration estimate. Memory remains conservative in observation
mode: raw summed RSS is not accountable unique memory.
"""
import math
from pathlib import Path
import re

TERMINAL = ('finished', 'superseded', 'expired', 'lost', 'rejected')
DRAIN_SECONDS = 300
RECOVERY_SECONDS = 30


def leases_conflict(left, right):
    if 'exclusive-host' in left or 'exclusive-host' in right:
        return True
    for a in left:
        for b in right:
            if a == b:
                return True
            aa, bb = port_range(a), port_range(b)
            if aa and bb and aa[0] == bb[0] and max(aa[1], bb[1]) <= min(aa[2], bb[2]):
                return True
    return False


def port_range(lease):
    match = re.fullmatch(r'ports:(tcp|udp):(\d+)(?:-(\d+))?', lease)
    if not match:
        return None
    protocol, low, high = match.groups()
    return protocol, int(low), int(high or low)


def validate(request):
    if request.get('schema', 'agent-ci.request/v1') != 'agent-ci.request/v1' or request.get('protocol', 2) != 2:
        raise ValueError('incompatible scheduler request')
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}', request['project']):
        raise ValueError('invalid project')
    if not re.fullmatch(r'[0-9a-f]{64}', request['command_digest']):
        raise ValueError('invalid command digest')
    for key in ('lane', 'sha', 'tree', 'task'):
        value = request.get(key, '')
        if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise ValueError('invalid identity')
    for field in ('slots', 'workers', 'memory_mb', 'generation'):
        if type(request[field]) is not int or request[field] < 1:
            raise ValueError('invalid scheduler request')
    if not isinstance(request['leases'], list) or len(request['leases']) > 128:
        raise ValueError('invalid lease list')
    for lease in request['leases']:
        if not re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,160}', lease):
            raise ValueError('invalid lease')
        if lease.startswith('ports:'):
            ports = port_range(lease)
            if not ports or not 1 <= ports[1] <= ports[2] <= 65535:
                raise ValueError('invalid port range')
    if request['mode'] not in ('full', 'run'):
        raise ValueError('invalid check mode')


def submit(data, token, owner, start, now, deadline, request):
    if token in data['jobs']:
        return
    validate(request)
    data['sequence'] += 1
    row = {key: request[key] for key in
           ('project', 'lane', 'sha', 'tree', 'mode', 'command_digest', 'generation')}
    row.update(version=2, owner=owner, start=start, sequence=data['sequence'],
               protocol=2, task=request.get('task') or request['command_digest'],
               lease_generation=data['sequence'], phase='queued',
               submitted_at=now, updated_at=now, deadline=deadline, state='queued',
               landing=bool(request.get('landing')), blocker=None,
               cleanup_required=bool(request.get('cleanup_required')),
               requested={key: request[key] for key in ('slots', 'workers', 'memory_mb', 'leases')})
    for key in ('predicted_mb', 'samples', 'source', 'profile_known', 'profile', 'predicted_s', 'estimate_scope', 'park_version', 'park_envelope'):
        if key in request:
            row[key] = request[key]
    if re.fullmatch(r'[a-zA-Z0-9_.-]{1,160}', request.get('run_id', '')):
        row['run_id'] = request['run_id']
    jobs = data['jobs']
    identity = ('project', 'tree', 'mode', 'command_digest')
    identical = next((key for key, old in jobs.items()
                      if old.get('state') in ('queued', 'running') and
                      all(old.get(k) == row[k] for k in identity)), None)
    lane = (row['project'], row['lane'], row['mode'], row['task'])
    generations = data.setdefault('generations', {})
    lane_key = '\t'.join(lane)
    newest = generations.get(lane_key, 0)
    if row['lane'] and not row['landing'] and row['generation'] < newest:
        row.update(state='superseded', result=77, finished_at=now)
    elif identical:
        row.update(state='following', follow=identical)
        if row['landing']:
            jobs[identical]['landing'] = True
    if row['lane'] and not row['landing']:
        generations[lane_key] = max(newest, row['generation'])
        for old in jobs.values():
            if (old.get('state') == 'queued' and not old.get('landing') and
                    tuple(old.get(k) for k in ('project', 'lane', 'mode', 'task')) == lane and
                    old['generation'] < row['generation'] and
                    not all(old.get(k) == row[k] for k in identity)):
                old.update(state='superseded', result=77, finished_at=now, updated_at=now)
    jobs[token] = row
    if request.get('recovery_of') in jobs:
        target = jobs[request['recovery_of']]
        if target.get('state') != 'quarantined':
            raise ValueError('recovery target is not quarantined')
        row['recovery_of'] = request['recovery_of']
        # Recovery inherits the failed lease's position, not a fresh drain claim.
        row['sequence'] = target['sequence']
    if request.get('maintenance') == 'prune':
        row['maintenance'] = 'prune'


def needs_cleanup(row):
    # Old leases predate the untouched flag; keep their recovery requirement.
    return row.get("cleanup_required", False) and row.get("touched", True)


def complete(data, token, result, now, quarantine=False):
    row = data['jobs'].get(token)
    if row is None:
        return
    if row.get('version') != 2:
        data['jobs'].pop(token, None)
        return
    if row.get('state') in TERMINAL:
        return
    if row.get('cancel') and result == 0:
        result = 74
    if row.get('workers') and row.get('admitted_at') is not None:
        served = data.setdefault('served', {})
        served[row['project']] = served.get(row['project'], 0) + max(0, now-row['admitted_at']) * row['workers']
    row.update(state='quarantined' if quarantine else 'finished', result=result,
               finished_at=now, updated_at=now, blocker=None)
    for key in ('workers', 'memory_mb'):
        row.pop(key, None)
    if quarantine:
        # Reaped processes no longer hold host capacity. Retain only resources
        # whose persistent state can be dirty; do not quarantine the host.
        row['quarantined_leases'] = [x for x in row['requested']['leases']
                                   if x != 'exclusive-host' and not x.startswith('postgres:')]
        row['recover_after'] = now
    else:
        row.pop('slot', None)


def unused(row):
    # Only an adapter with unique, attributable memory may provide this credit.
    # Raw ps RSS double-counts shared pages. The initial observer credits zero.
    return row.get('memory_mb', 0)


def blocker(row, active, metrics, limits):
    req = row['requested']
    for token, other in active.items():
        if other.get('version') != 2:
            return dict(reason='legacy-run', holder=token)
        leases = other.get('quarantined_leases', other['requested']['leases'])
        if leases_conflict(req['leases'], leases):
            name = next((x for x in req['leases'] if leases_conflict([x], leases)), 'exclusive-host')
            return dict(reason='lease:' + name, holder=token)
    same = [(t, r) for t, r in active.items() if r.get('project') == row['project']]
    slots = min([req['slots']] + [r['requested']['slots'] for _, r in same])
    if len(same) >= slots:
        return dict(reason='project-slot', holder=same[0][0])
    occupied = {t: r for t, r in active.items() if r.get('workers')}
    if len(occupied) >= limits['MAX_JOBS']:
        return dict(reason='jobs', holder=next(iter(occupied), None))
    workers = sum(r.get('workers', 0) for r in active.values())
    if workers + req['workers'] > limits['MAX_WORKERS']:
        return dict(reason='workers', holder=next(iter(occupied), None))
    reserve = min(limits['CPU_RESERVE'], max(0, metrics['cores'] - 1))
    if (workers + req['workers'] > metrics['cores'] - reserve or
            req['workers'] > math.ceil(metrics['cores'] - reserve - metrics['load'])):
        return dict(reason='cpu', holder=None)
    if metrics['available_mb'] - limits['MEM_RESERVE_MB'] - sum(unused(r) for r in active.values()) < req['memory_mb']:
        return dict(reason='memory', holder=next(iter(occupied), None))
    return None


def safe_backfill(head, candidate, active, metrics, limits):
    if leases_conflict(head['requested']['leases'], candidate['requested']['leases']):
        return False
    # Same-project work could take the very slot the head is waiting for.
    if head['project'] == candidate['project']:
        return False
    # Only lease conflicts are certain to depart before the head can start.
    # A full project slot also guarantees at least one departure. Keep the
    # remaining occupants, without predicting which one will depart first.
    survivors = {t: r for t, r in active.items()
                 if not leases_conflict(head['requested']['leases'], r.get('requested', {}).get('leases', []))}
    same = [t for t, r in survivors.items() if r.get('project') == head['project']]
    slots = min([head['requested']['slots']] + [survivors[t]['requested']['slots'] for t in same])
    if len(same) >= slots:
        # Only use this proof when there is exactly one possible departing job.
        if len(same) != 1:
            return False
        survivors.pop(same[0])
    proposed = dict(candidate, workers=candidate['requested']['workers'],
                    memory_mb=candidate['requested']['memory_mb'])
    survivors = dict(survivors, backfill=proposed)
    # Load averages lag new starts. Reserve CPU for every existing commitment
    # plus this candidate instead of assuming their load is already sampled.
    projected = dict(metrics, load=metrics['load'] + candidate['requested']['workers'] +
                     sum(r.get('workers', 0) for r in active.values()))
    if blocker(head, survivors, projected, limits):
        return False
    # Memory samples can fall as existing jobs consume unused commitments.
    # Preserve the head's entire budget in the current live-memory calculation.
    return (metrics['available_mb'] - limits['MEM_RESERVE_MB'] -
            sum(unused(r) for r in active.values()) >=
            head['requested']['memory_mb'] + candidate['requested']['memory_mb'])


def schedule(data, metrics, limits, procs, now):
    import resources
    jobs = data['jobs']
    observe(data, procs, now)
    for token, row in list(jobs.items()):
        if row.get('version') != 2:
            if not resources.live(row, procs):
                jobs.pop(token)
            continue
        state = row['state']
        if state in TERMINAL or state == 'quarantined':
            continue
        if state in ('parked', 'starting'):
            # The pause controller owns result publication and launcher reaping.
            # A starting row may still own a live pre-admission process tree.
            continue
        if row.get('cancel') and not row.get('process') and not row.get('groups'):
            complete(data, token, 74, now, state == 'running' and needs_cleanup(row))
            continue
        if not resources.live(row, procs):
            complete(data, token, 74, now, state == 'running' and needs_cleanup(row))
            if row['state'] != 'quarantined':
                row['state'] = 'lost'
        elif state in ('queued', 'following') and now >= row['deadline']:
            complete(data, token, 75, now)
            row['state'] = 'expired'
    # Propagate results before admission; a follower never holds capacity.
    for row in jobs.values():
        if row.get('state') == 'following':
            leader = jobs.get(row['follow'])
            if not leader or leader.get('state') in TERMINAL + ('quarantined',):
                row.update(state='finished', result=leader.get('result', 74) if leader else 74,
                           finished_at=now, updated_at=now)
    prior = data.get('sample', {})
    pressure = metrics['pressure'] != 1
    growth = metrics.get('swap_mb', 0) > prior.get('swap_mb', metrics.get('swap_mb', 0))
    if pressure or growth:
        data['frozen_until'] = now + RECOVERY_SECONDS
    data['sample'] = dict(at=now, swap_mb=metrics.get('swap_mb', 0))
    frozen = now < data.get('frozen_until', 0)
    data['throttle_reason'] = ('pressure' if pressure else 'swap-recovery') if frozen else None
    pending = sorted((r for r in jobs.values() if r.get('state') == 'queued'),
                     key=lambda r: r['sequence'])
    active = {t: r for t, r in jobs.items() if r.get('workers') or r.get('state') == 'quarantined'}
    if limits.get('FAST_LANE') and not data.get('paused'):
        import scheduler_fast
        scheduler_fast.admit(data, pending, active, metrics, limits, now, frozen)
        pending = []
    head = None
    for row in pending:
        req = row['requested']
        cpu_limit = metrics['cores'] - min(limits['CPU_RESERVE'], max(0, metrics['cores'] - 1))
        if (req['workers'] > min(limits['MAX_WORKERS'], cpu_limit) or
                req['memory_mb'] > limits.get('MAX_JOB_MB', float('inf')) or
                req['memory_mb'] > metrics.get('total_mb', float('inf')) - limits['MEM_RESERVE_MB']):
            row.update(state='rejected', result=75, finished_at=now, updated_at=now,
                       blocker=dict(reason='impossible-request', holder=None))
            continue
        holders = {t: r for t, r in active.items() if t != row.get('recovery_of') and
                   not (row.get('maintenance') == 'prune' and r.get('state') == 'quarantined')}
        reason = blocker(row, holders, metrics, limits)
        if frozen:
            reason = dict(reason='pressure' if pressure else 'swap-recovery', holder=None)
        if metrics.get('legacy_blocker'):
            reason = metrics['legacy_blocker']
        if data.get('paused'):
            reason = dict(reason='maintenance', holder=None)
        if not reason and head and not row.get('recovery_of'):
            if now - head['submitted_at'] >= DRAIN_SECONDS:
                reason = dict(reason='head-drain', holder=next(t for t, r in jobs.items() if r is head))
            elif not safe_backfill(head, row, active, metrics, limits):
                reason = dict(reason='head-reservation', holder=next(t for t, r in jobs.items() if r is head))
        row.update(blocker=reason, updated_at=now)
        if reason:
            # A quarantined resource needs recovery, not a host drain. Unrelated
            # jobs must progress while that resource is unavailable.
            if active.get(reason.get('holder'), {}).get('state') != 'quarantined':
                head = head or row
            continue
        used = {r.get('slot') for r in holders.values() if r.get('project') == row['project']}
        slot = next(i for i in range(1, row['requested']['slots'] + 1) if i not in used)
        row.update(state='running', slot=slot, workers=row['requested']['workers'],
                   memory_mb=row['requested']['memory_mb'], admitted_at=now, phase='admitted', touched=False)
        row['granted'] = dict(jobs=1, slot=slot, workers=row['workers'],
                              memory_mb=row['memory_mb'], leases=list(row['requested']['leases']))
        token = next(t for t, r in jobs.items() if r is row)
        active[token] = row
    # Keep terminal outcomes for late followers. Bound retention by time, not
    # count: bursts must not erase a still-live follower's result.
    followed = {r.get('follow') for r in jobs.values() if r.get('state') == 'following'}
    for token, row in list(jobs.items()):
        if (row.get('state') in TERMINAL and token not in followed and
                now - row.get('finished_at', now) > 86400 and not resources.live(row, procs)):
            jobs.pop(token)


def observe(data, procs, now):
    import resources
    import scheduler_process
    for row in data['jobs'].values():
        if row.get('version') != 2 or not row.get('workers'):
            continue
        # A saved child record is proof of a potentially touched grant,
        # including records written by an older phase supervisor.
        if row.get('process') or row.get('groups'):
            row['touched'] = True
        tracked = {}
        for record in row.get('groups', {}).values():
            if record.get('generation') == row['lease_generation']:
                scheduler_process.refresh(record, procs)
                tracked.update(scheduler_process.members(record, procs))
        rss = sum(p.get('rss', 0) for p in tracked.values()) // 1024
        row.update(current_rss_mb=rss, peak_rss_mb=max(rss, row.get('peak_rss_mb', 0)),
                   unused_mb=unused(row), observed_at=now)


def prediction(root, project, mode, workers, default):
    """Read bounded recent receipts; logs are a lower-confidence fallback."""
    import json
    root = Path(root)
    peaks = []
    paths = list((root / 'receipts').glob('*.json')) + list((root / 'receipts' / project).glob('*.json'))
    for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
        try:
            row = json.loads(path.read_text())
            if (row.get('project'), row.get('mode'), row.get('workers')) != (project, mode, workers):
                continue
            if row.get('exit_code') != 0 or row.get('incomplete') or row.get('killed'):
                continue
            peak = float(row['peak_rss_mb'])
            if math.isfinite(peak) and peak > 0:
                peaks.append(peak)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    source = 'receipts'
    known = bool(peaks)
    if not peaks:
        source = 'logs'
        for path in sorted((root / 'projects' / project / 'logs').glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
            try:
                # Read only bounded tails. Commands are never copied to state.
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 131072))
                    text = stream.read().decode('utf-8', errors='replace')
                if not re.search(r'^== finished rc=0 ', text, re.M):
                    continue
                if '== scheduler mode=' in text and '== scheduler mode=' + mode not in text:
                    continue
                if not re.search(r'^== resources workers=%s ' % workers, text, re.M):
                    continue
                values = re.findall(r'^\s*(\d+)\s+maximum resident', text, re.M)
                peaks.extend(int(v) / 1048576 for v in values)
                known = known or bool(values and '== scheduler mode=' + mode in text)
            except OSError:
                continue
    return dict(predicted_mb=math.ceil(max(peaks) * 1.25 + 256) if peaks else default,
                samples=len(peaks), source=source if peaks else 'default', profile_known=known)
