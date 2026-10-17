"""Phase memory grants. All mutations share the admission ledger transaction.

New admissions preserve one shared largest-growth allowance. Heavy commands
acquire their entire trusted ceiling before launch, so a granted command never
waits for another memory increment while holding its working set. Infrastructure
and cleanup retain the initial grant. No grant is reduced from sampled RSS.
"""
import copy
from pathlib import Path
import time

import ci_telemetry
import resources

GROWTH_WAIT_SECONDS = 60


def admission_memory(rows):
    rows = [row for row in rows if row.get('workers')]
    held = sum(row.get('memory_mb', 0) for row in rows)
    extra = max([0] + [max(0, row.get('memory_ceiling_mb', row.get('memory_mb', 0)) -
                               row.get('memory_mb', 0)) for row in rows])
    return held + extra


def grow(data, token, metrics, limits, now):
    """Attempt one complete phase grant; never partially acquire an increment."""
    row = data['jobs'][token]
    target = row['memory_ceiling_mb']
    if target <= row['memory_mb']:
        row.pop('memory_wait', None)
        return True
    waiters = [(r['memory_wait']['at'], r['sequence'], t) for t, r in data['jobs'].items()
               if r.get('state') == 'running' and r.get('memory_wait') and not r.get('cancel')]
    if waiters and min(waiters)[2] != token:
        return False
    prior = data.get('sample', {})
    pressure = metrics['pressure'] != 1
    swap = metrics.get('swap_mb', 0) > prior.get('swap_mb', metrics.get('swap_mb', 0))
    if pressure or swap:
        data['frozen_until'] = now + 30
    data['sample'] = dict(at=now, swap_mb=metrics.get('swap_mb', 0))
    if now < data.get('frozen_until', 0):
        return False
    committed = sum(r.get('memory_mb', 0) for r in data['jobs'].values() if r.get('workers'))
    available = min(metrics['available_mb'], metrics.get('total_mb', float('inf'))) - limits['MEM_RESERVE_MB']
    if target > limits['MAX_JOB_MB'] or committed + target - row['memory_mb'] > available:
        return False
    row['memory_mb'] = target
    row['granted']['memory_mb'] = target
    row['peak_grant_mb'] = max(target, row.get('peak_grant_mb', 0))
    row.pop('memory_wait', None)
    row['blocker'] = None
    return True


def event(row, kind, phase, previous, waited=0):
    ci_telemetry.emit(Path.home()/'ci/telemetry', kind,
        project=row['project'], run_id=row.get('run_id', 'unknown'), phase=phase,
        memory_mb=row['memory_mb'], previous_mb=previous,
        ceiling_mb=row['memory_ceiling_mb'], wait_s=max(0, waited))


def prepare(queue, token, phase, heavy, seconds, interrupted):
    """Wait before creating a child; the wait consumes the original deadline."""
    import scheduler_process
    started = time.monotonic()
    generation = None
    waiting = False
    try:
        while True:
            observation = None
            with queue.transaction() as data:
                row = data['jobs'][token]
                if not row.get('initial_memory_mb') or not heavy or phase in ('cleanup', 'maintenance'):
                    return True, seconds
                if generation is None:
                    generation = row['lease_generation']
                now = time.time()
                elapsed = time.monotonic() - started
                owner = scheduler_process.snapshot().get(row['owner'], {})
                if (row['state'] != 'running' or row.get('cancel') or interrupted[0] or
                        row['lease_generation'] != generation or row.get('groups') or
                        row.get('process') or owner.get('birth') != row.get('owner_birth') or
                        elapsed >= min(GROWTH_WAIT_SECONDS, seconds) or
                        now >= min(row['deadline'], row.get('job_deadline', row['deadline']))):
                    return False, 0
                previous = row['memory_mb']
                if row['memory_ceiling_mb'] <= previous:
                    return True, max(1, seconds - int(elapsed))
                row.setdefault('memory_wait', dict(at=now, target_mb=row['memory_ceiling_mb']))
                row['blocker'] = dict(reason='memory-growth', holder=None)
                if grow(data, token, resources.scheduler_sample(), resources.policy('runner'), now):
                    observation = copy.deepcopy(row)
                else:
                    row['phase'] = 'memory-wait'
            if observation:
                event(observation, 'memory-granted', phase, previous, elapsed)
                print('== memory grant %s -> %s MB phase=%s wait=%ss' %
                      (previous, observation['memory_mb'], phase, int(elapsed)), flush=True)
                return True, max(1, seconds - int(elapsed))
            if not waiting:
                print('== memory waiting phase=%s timeout=%ss' % (phase, GROWTH_WAIT_SECONDS), flush=True)
                ci_telemetry.emit(Path.home()/'ci/telemetry', 'blocked', phase=phase,
                                  reason='memory-growth', project=row['project'], run_id=row.get('run_id', 'unknown'))
                waiting = True
            time.sleep(min(1, max(0, seconds - (time.monotonic() - started))))
    finally:
        if generation is not None:
            with queue.transaction() as data:
                row = data['jobs'].get(token, {})
                if row.get('lease_generation') == generation:
                    row.pop('memory_wait', None)
                    if (row.get('blocker') or {}).get('reason') == 'memory-growth':
                        row['blocker'] = None


def release_phase(queue, token, generation, phase):
    """Called only after reaping; failure keeps the full commitment for recovery."""
    observation = None
    with queue.transaction() as data:
        row = data['jobs'].get(token, {})
        initial = row.get('initial_memory_mb')
        if (not initial or row.get('state') != 'running' or
                row.get('lease_generation') != generation or row.get('groups') or
                row.get('process') or row.get('reap_failed')):
            return
        previous = row['memory_mb']
        if previous > initial:
            row['memory_mb'] = initial
            row['granted']['memory_mb'] = initial
            observation = copy.deepcopy(row)
    if observation:
        event(observation, 'memory-released', phase, previous)
        print('== memory release %s -> %s MB phase=%s' % (previous, initial, phase), flush=True)
