#!/usr/bin/env python3
"""Shared host admission for heavy checks; no command text or credentials in state."""
import argparse
import copy
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_telemetry
from process_tree import Tree

KEYS = ('MAX_JOBS', 'MAX_WORKERS', 'CPU_RESERVE', 'MEM_RESERVE_MB', 'WORKER_MB', 'MAX_JOB_MB', 'KILL_PCT', 'CLEANUP_QUARANTINE_S', 'FAST_LANE', 'FAST_WORKERS', 'FAST_MEMORY_MB', 'FAST_SECONDS', 'INITIAL_MEMORY_MB')


def output(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=10)


def policy(mode):
    # Both toolkits use the same host policy. Only these numeric settings leave
    # the trusted shell config; never serialize its environment or command text.
    values = dict(os.environ)
    for name in ('agent-lanes', 'agent-lanes'):
        path = Path.home() / '.config' / name / 'config'
        if path.is_file():
            keys = ['AGENT_CI_' + key for key in KEYS]
            script = '. "$1" >/dev/null 2>&1 || exit; shift; for key; do printf "%s=%s\n" "$key" "${!key}"; done'
            result = output(['/bin/bash', '-c', script, '_', str(path), *keys])
            values.update(line.split('=', 1) for line in result.splitlines() if line.partition('=')[2])
    defaults = dict(MAX_JOBS=1, MAX_WORKERS=2, CPU_RESERVE=2, MEM_RESERVE_MB=2048, WORKER_MB=2048, MAX_JOB_MB=8192, KILL_PCT=150, CLEANUP_QUARANTINE_S=600, FAST_LANE=0, FAST_WORKERS=1, FAST_MEMORY_MB=2048, FAST_SECONDS=60, INITIAL_MEMORY_MB=0)
    result = {}
    for key, default in defaults.items():
        raw = str(values.get('AGENT_CI_' + key, default))
        if not raw.isdigit() or not (0 if key in ('FAST_LANE', 'INITIAL_MEMORY_MB') else 1) <= int(raw) <= (1 if key == 'FAST_LANE' else 1048576):
            raise ValueError('AGENT_CI_' + key + ' must be a positive integer')
        result[key] = int(raw)
    if mode == 'local':
        result['MAX_WORKERS'] = 1
    return result


def configured_scheduler_version():
    # Host config wins over the inherited environment, as for numeric policy.
    value = os.environ.get('AGENT_CI_SCHEDULER', 'v1')
    for name in ('agent-lanes', 'agent-lanes'):
        path = Path.home() / '.config' / name / 'config'
        if path.is_file():
            result = output(['/bin/bash', '-c',
                '. "$1" >/dev/null 2>&1 || exit; printf "%s" "${AGENT_CI_SCHEDULER:-}"', '_', str(path)])
            if result:
                value = result
    if value not in ('v1', 'v2'):
        raise ValueError('invalid host scheduler version')
    return value


def scheduler_version():
    version = configured_scheduler_version()
    marker = Path.home()/'.local/state/agent-ci/authority'
    if version == 'v1' and (not marker.exists() or marker.read_text() == 'v1'):
        return version
    import scheduler_authority
    return scheduler_authority.select(version)


def scheduler_sample():
    metrics = sample()
    if sys.platform == 'darwin':
        # Keep the OS reclaimable-memory observation used by V1. Full outstanding
        # commitments are still reserved; summed RSS never earns V2 credit.
        metrics['total_mb'] = int(output(['/usr/sbin/sysctl', '-n', 'hw.memsize'])) // 1048576
        swap = output(['/usr/sbin/sysctl', '-n', 'vm.swapusage'])
        match = re.search(r'used = ([0-9.]+)([MG])', swap)
        if not match:
            raise ValueError('swap observation unavailable')
        metrics['swap_mb'] = float(match[1]) * (1024 if match[2] == 'G' else 1)
    else:
        mem = Path('/proc/meminfo').read_text()
        values = {k: int(v) for k, v in re.findall(r'^(SwapTotal|SwapFree):\s+(\d+)', mem, re.M)}
        metrics['swap_mb'] = (values['SwapTotal'] - values['SwapFree']) / 1024
        metrics['total_mb'] = int(re.search(r'^MemTotal:\s+(\d+)', mem, re.M)[1]) // 1024
    return metrics


def acquire_v2(queue, token, owner, deadline, limits, request, mode=None, submitted=None):
    import scheduler
    import scheduler_legacy
    import scheduler_process
    import scheduler_events
    import scheduler_pause
    telemetry_state = None
    if mode == 'runner':
        import scheduler_profiles
        request = scheduler_profiles.authorize(request, Path.home()/'ci', limits)
    registered = False
    adopting = bool(request.pop('park_adopt', False))
    park_nonce = request.pop('park_nonce', '')
    envelope_hash = request.get('park_envelope', '')
    while True:
        recovering = bool(request.get('recovery_of'))
        if mode and not recovering and scheduler_version() != 'v2':
            queue.release(token, 74)
            return dict(result=74, state='lost')
        scheduler_process.reconcile(queue)
        if not request.get('recovery_of'):
            import scheduler_maintenance
            scheduler_maintenance.start_recovery(queue)
        if mode:
            limits = policy(mode)
        with queue.transaction() as data:
            # A row can be registered while we wait for the lock. Sample only
            # after the ledger is locked, or that live owner can look absent.
            procs = scheduler_process.snapshot()
            metrics = scheduler_sample()
            now = time.time()
            if mode and not recovering and data.get('authority') == 'v1':
                return dict(result=74, state='lost')
            if recovering and data['jobs'].get(request['recovery_of'], {}).get('state') != 'quarantined':
                return dict(result=74, state='lost')
            if not registered and not recovering and not adopting and (Path.home()/'ci/freeze').exists():
                return dict(result=75, state='frozen')
            scheduler_legacy.import_cleanup(data, Path.home() / 'ci', now)
            if not registered:
                if owner not in procs:
                    raise ValueError('resource queue owner lost')
                if adopting:
                    adopted = scheduler_pause.adopt(data, token, owner, procs[owner]['start'],
                                                    procs[owner]['birth'], envelope_hash,
                                                    park_nonce, now)
                    if adopted == 'expired':
                        return dict(result=75, state='expired')
                    if not adopted:
                        return dict(result=scheduler_pause.PARKED, state='park-conflict')
                else:
                    scheduler.submit(data, token, owner, procs[owner]['start'], now, deadline, request)
                    data['jobs'][token]['owner_birth'] = procs[owner]['birth']
                    if submitted:  # A pool move keeps its first submission time (never in the future).
                        data['jobs'][token]['submitted_at'] = min(now, submitted)
                registered = True
            if not adopting and envelope_hash and scheduler_pause.park(
                    data, token, owner, procs[owner]['birth'], envelope_hash, now):
                outcome = dict(result=scheduler_pause.PARKED, state='parked')
            else:
                outcome = None
            blocked = scheduler_legacy.blocker(Path.home() / 'ci', procs)
            scheduler.schedule(data, dict(metrics, legacy_blocker=blocked), limits, procs, now)
            row = data['jobs'][token]
            if row['state'] == 'running':
                outcome = dict(workers=row['workers'], memory_mb=row['memory_mb'], slot=row['slot'],
                            predicted_mb=row.get('predicted_mb', row['memory_mb']),
                            job_deadline=int(row.get('job_deadline', 0)), lane_class=row.get('lane_class','normal'))
            if row['state'] in scheduler.TERMINAL + ('quarantined',):
                leader = data['jobs'].get(row.get('follow'), {})
                outcome = dict(result=row.get('result', 74), state=row['state'], follow_run=leader.get('run_id'))
            observation = copy.deepcopy(row)
        telemetry_state = scheduler_events.emit_transition(
            queue.root, telemetry_state, observation, metrics, now)
        if outcome is not None:
            return outcome
        time.sleep(min(2, max(0, deadline - now)))


def local_allowed():
    path = Path.home() / '.config/agent-ci/policy.json'
    if path.exists():
        mode = json.loads(path.read_text()).get('execution')
        if mode != 'local-queue':
            raise ValueError('this host requires remote checks; no local fallback')


def sample():
    # Load beside this script, including isolated run snapshots and test imports.
    import importlib.util
    spec = importlib.util.spec_from_file_location('remote_ci_platform', Path(__file__).with_name('platform_info.py'))
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    return adapter.sample(output)


def processes():
    # Start time protects against PID reuse; zombie owners cannot hold the queue.
    result = {}
    for line in output(['ps', '-axo', 'pid=,ppid=,pgid=,rss=,%cpu=,stat=,lstart=']).splitlines():
        parts = line.split(None, 6)
        if len(parts) == 7:
            pid, parent, group, rss, cpu, state, start = parts
            if not state.startswith('Z'):
                result[int(pid)] = dict(parent=int(parent), group=int(group), rss=int(rss), cpu_percent=float(cpu), start=start)
    return result









def project_memory(value, limits):
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError('REMOTE_CI_MEMORY_MB must be a nonnegative integer')
    return min(int(value), limits.get('MAX_JOB_MB', 8192))


def kill_limits():
    # Preserve the installed runner's conservative fallback on policy read errors.
    try:
        limits = policy('runner')
        return limits['KILL_PCT'], limits['MAX_JOB_MB']
    except (OSError, ValueError, subprocess.SubprocessError):
        return 150, 8192


def budget_exceeded(rss, memory, percent, maximum=8192):
    return rss * 100 > min(memory * percent, maximum * 100)


def job_group(row):
    if row.get('version') == 2:
        return row.get('process', {}).get('group', 0)
    group = row.get('group', 0)
    if row.get('marker'):
        try:
            group = int(Path(row['marker']).read_text().strip())
        except (OSError, ValueError):
            pass
    return group


def supervised(row, procs):
    for pid, start in ((row.get('owner'), row.get('start')),
                       (row.get('watcher'), row.get('watcher_start'))):
        if start is not None and procs.get(pid, {}).get('start') == start:
            return True
    return False


def cleanup_live(row, procs, quarantine=None):
    markers = [row[key] for key in ('tree_marker', 'infra_tree_marker') if row.get(key)]
    if markers:
        uncertain = False
        for marker in markers:
            try:
                tree = Tree.load(marker)
                members = tree.observe(procs)
                if set(members) - tree.uncertain:
                    row.pop('quarantine_since', None)
                    row.pop('quarantine_expired', None)
                    return True  # Verified surviving descendants do not expire.
                uncertain = uncertain or bool(members)
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                uncertain = True  # A lost journal and unreadable identities share one bound.
        if not uncertain:
            row.pop('quarantine_since', None)
            row.pop('quarantine_expired', None)
            return False
        if supervised(row, procs):
            row.pop('quarantine_since', None)
            row.pop('quarantine_expired', None)
            return True
        if quarantine is None:
            quarantine = policy('runner')['CLEANUP_QUARANTINE_S']
        now = time.time()
        since = row.setdefault('quarantine_since', now)
        if now - since < quarantine:
            return True
        row['quarantine_expired'] = True
        return False
    group = job_group(row)
    return bool(group and any(p['group'] == group for p in procs.values()))


def sweep(data, procs, quarantine):
    expired = []
    for token, row in list(data['jobs'].items()):
        if row.get('version') != 2 and not live(row, procs, quarantine):
            if row.get('quarantine_expired'):
                expired.append((token, row.get('run_id', token)))
            del data['jobs'][token]
    return expired


def cleanup_events(queue, tokens):
    # Keep best-effort telemetry outside the admission/release transaction.
    for token, run_id in tokens:
        event(queue, token, 'cleanup', run_id=run_id, reason='quarantine-expired')


def live(row, procs, quarantine=None):
    if row.get('version') == 2:
        owner = procs.get(row.get('owner'), {})
        # Kernel birth identity is authoritative. Linux/WSL ps lstart is
        # reconstructed from a drifting boot-time wall clock and can change
        # for a living PID; comparing it as well would lose a healthy queue.
        owner_live = bool(owner) and (
            owner.get('birth') == row['owner_birth'] if row.get('owner_birth')
            else owner.get('start') == row.get('start'))
        return bool(owner_live or row.get('process') or row.get('groups'))
    if supervised(row, procs):
        row.pop('quarantine_since', None)
        return True
    return cleanup_live(row, procs, quarantine)



class Queue:
    def __init__(self, root=None):
        self.root = Path(root) if root else Path.home() / '.local/state/agent-ci'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def transaction(self):
        with (self.root / 'queue.lock').open('a') as lock:
            # Never wait indefinitely behind a crashed or stopped coordinator.
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError('resource queue lock unavailable')
                    time.sleep(0.1)
            path = self.root / 'queue.json'
            data = json.loads(path.read_text()) if path.exists() else dict(sequence=0, jobs={})
            before = json.dumps(data, sort_keys=True)
            yield data
            # Existing project guards read the v1 top-level group. Publish that
            # view with the same atomic ledger update as its v2 phase record.
            # It grants nothing independently and must disappear on cancellation,
            # phase reap, generation mismatch or terminal state.
            for row in data['jobs'].values():
                if row.get('version') != 2:
                    continue
                phase = row.get('process') or {}
                group = phase.get('group')
                registered = row.get('groups', {}).get(str(group), {})
                # Observation adds descendants to groups independently of process.
                # Compare immutable phase identity, never the changing member set.
                identity = ('group', 'generation', 'phase', 'deadline', 'tree_marker')
                leader = phase.get('members', {}).get(str(group))
                if (row.get('state') == 'running' and not row.get('cancel') and
                        row.get('workers', 0) > 0 and type(group) is int and group > 0 and
                        phase.get('generation') == row.get('lease_generation') and
                        leader is not None and
                        registered.get('members', {}).get(str(group)) == leader and
                        all(registered.get(key) == phase.get(key) for key in identity)):
                    row['group'] = group
                else:
                    row.pop('group', None)
            if json.dumps(data, sort_keys=True) != before:
                new = path.with_suffix('.json.new')
                new.write_text(json.dumps(data, sort_keys=True) + '\n')
                new.chmod(0o600)
                new.replace(path)

    def release(self, token, result=74, quarantine=False, actual=None):
        unreaped = False
        released = None
        report_peak = False
        with self.transaction() as data:
            row = data['jobs'].get(token, {})
            v2 = row.get('version') == 2
            if v2:
                import scheduler
                if row.get('process') or row.get('groups'):
                    row.update(cancel=True, result=76)
                    unreaped = True
                else:
                    if actual is not None:
                        row['actual_check_peak_mb'] = actual
                    scheduler.complete(data, token, result, time.time(), quarantine)
                    released = row.get('result')
            else:
                retained = bool((row.get('tree_marker') or row.get('infra_tree_marker')) and
                                cleanup_live(row, processes()))
                report_peak = 'peak_rss_mb' in row and not row.get('peak_reported')
                if report_peak:
                    row['peak_reported'] = True
                if retained:
                    row['cleanup_pending'] = True
                else:
                    data['jobs'].pop(token, None)
        if unreaped:
            raise ValueError('recorded process group not reaped')
        if v2:
            return released
        if report_peak:
            print(f"== memory peak {math.ceil(row['peak_rss_mb'])} MB of {row['memory_mb']} granted", flush=True)
        if not retained and row.get('quarantine_expired'):
            cleanup_events(self, [(token, row.get('run_id', token))])
        return not retained


    def record_peak(self, token, peak):
        with self.transaction() as data:
            row = data['jobs'][token]
            row['peak_rss_mb'] = max(row.get('peak_rss_mb', 0), peak)


    def update(self, token, **fields):
        with self.transaction() as data:
            data['jobs'][token].update(fields)

    def register_infra_tree(self, token, marker, allow_missing=False):
        with self.transaction() as data:
            row = data['jobs'].get(token)
            if row is None and allow_missing:
                return False  # Legacy recovery holds the host guard and project quarantine.
            if not row or row.get('version') == 2 or not row.get('workers'):
                raise ValueError('infrastructure command needs an admitted V1 grant')
            current = row.get('infra_tree_marker')
            if current and current != marker:
                raise ValueError('infrastructure ownership already registered')
            row['infra_tree_marker'] = marker
            return True

    def clear_infra_tree(self, token, marker, allow_missing=False):
        with self.transaction() as data:
            row = data['jobs'].get(token)
            if row is None and allow_missing:
                return
            if not row or row.get('infra_tree_marker') != marker:
                raise ValueError('infrastructure ownership identity changed')
            row.pop('infra_tree_marker')



def unused_reservation(row, procs):
    if row.get('infra_tree_marker'):
        return row['memory_mb']  # Never credit an unverified infrastructure tree.
    group = job_group(row)
    if procs is None or not group:
        return row['memory_mb']
    try:
        if row.get('tree_marker'):
            tree = Tree.load(row['tree_marker'])
        elif 'memory_members' in row:
            tree = Tree.from_record(dict(root=group, known=row['memory_members']))
        elif group in procs and procs[group]['group'] == group:
            tree = Tree(group, procs[group].get('birth'))
        else:
            return row['memory_mb']
        tracked = tree.observe(procs)
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return row['memory_mb']  # Unknown ownership cannot earn memory credit.
    row['memory_members'] = {str(pid): birth for pid, birth in tree.known.items()}
    used = sum(p['rss'] for pid, p in tracked.items() if pid not in tree.uncertain) // 1024
    return max(0, row['memory_mb'] - used)



def free_memory(active, metrics, limits, procs=None):
    return max(0, metrics['available_mb'] - limits['MEM_RESERVE_MB']
               - sum(unused_reservation(row, procs) for row in active))


def capacity(rows, metrics, limits, requested, procs=None, memory_mb=0):
    active = [row for row in rows if row.get('workers')]
    if metrics['pressure'] != 1 or len(active) >= limits['MAX_JOBS']:
        return 0
    reserved = sum(row['workers'] for row in active)
    reserve = min(limits['CPU_RESERVE'], max(0, metrics['cores'] - 1))
    cpu = min(metrics['cores'] - reserve - reserved,
              math.ceil(metrics['cores'] - reserve - metrics['load']))
    memory = free_memory(active, metrics, limits, procs)
    if memory < project_memory(memory_mb, limits):
        return 0
    # MAX_WORKERS bounds the host: grants of all running jobs together.
    return max(0, min(requested, limits['MAX_WORKERS'] - reserved, cpu, memory // limits['WORKER_MB']))


def status_snapshot(rows, metrics, limits, procs):
    active = [row for row in rows if row.get('workers')]
    waiting = len(rows) - len(active)
    return dict(metrics, active=len(active), waiting=waiting, policy=limits,
                can_admit=bool(capacity(rows, metrics, limits, 1, procs)) and not waiting,
                free_workers=capacity(rows, metrics, limits, limits['MAX_WORKERS'], procs),
                free_memory_mb=free_memory(active, metrics, limits, procs))


def event(queue, token, name, run_id=None, **fields):
    return ci_telemetry.emit(queue.root / 'telemetry', name, run_id=run_id or token, **fields)


def blocking_reason(rows, metrics, limits):
    active = [row for row in rows if row.get('workers')]
    if metrics['pressure'] != 1:
        return 'pressure'
    if len(active) >= limits['MAX_JOBS']:
        return 'jobs'
    workers = sum(row['workers'] for row in active)
    if workers >= limits['MAX_WORKERS']:
        return 'workers'
    reserve = min(limits['CPU_RESERVE'], max(0, metrics['cores'] - 1))
    if workers >= metrics['cores'] - reserve or metrics['load'] >= metrics['cores'] - reserve:
        return 'cpu'
    return 'memory'


def acquire(queue, token, owner, deadline, limits, requested, mode=None, memory_mb=0, submitted=None, run_id=None):
    local_allowed()
    project_memory(memory_mb, limits)
    registered = False
    started = time.time() if submitted is None else submitted
    last_reason = None
    while time.time() < deadline:
        if scheduler_version() == 'v2':
            raise ValueError('host switched authority; drain v1 before v2 admission')
        if mode:
            limits = policy(mode)
        admitted = None
        with queue.transaction() as data:
            procs = processes()
            metrics = sample()  # Sample memory after RSS, under the same ledger lock.
            if time.time() >= deadline:
                break
            jobs = data['jobs']
            if data.get('authority') == 'v2':
                raise ValueError('scheduler authority changed while waiting')
            if any(row.get('version') == 2 and (row.get('state') not in ('finished', 'superseded', 'expired', 'lost', 'rejected') or row.get('workers') or row.get('groups') or row.get('process')) for row in jobs.values()):
                raise ValueError('v2 leases must be resolved before v1 admission')
            expired = sweep(data, procs, limits.get('CLEANUP_QUARANTINE_S', 600))
            if token not in jobs:
                if registered or owner not in procs:
                    raise ValueError('resource queue owner lost')
                data['sequence'] += 1
                jobs[token] = dict(owner=owner, start=procs[owner]['start'], sequence=data['sequence'],
                                   submitted_at=time.time(), deadline=deadline, requested_workers=requested,
                                   requested_memory_mb=project_memory(memory_mb, limits), run_id=run_id or token)
                registered = True
            pending = sorted((key for key, row in jobs.items() if not row.get('workers') and row.get('version') != 2),
                             key=lambda key: jobs[key]['sequence'])
            eligible = pending and pending[0] == token
            reason = 'fifo'
            if pending and not eligible:
                head = jobs[pending[0]]
                # Keep V1's bounded memory backfill, then drain for the head.
                active = [row for row in jobs.values() if row.get('workers')]
                need = head.get('requested_memory_mb', 0)
                eligible = (need > free_memory(active, metrics, limits, procs) and
                            time.time() < min(head.get('submitted_at', 0) + 30, head.get('deadline', 0)))
            if eligible and not any(r.get('version') == 2 and r.get('workers') for r in jobs.values()):
                workers = capacity(jobs.values(), metrics, limits, requested, procs, memory_mb)
                if workers:
                    memory = max(workers * limits['WORKER_MB'], project_memory(memory_mb, limits))
                    jobs[token].update(workers=workers, memory_mb=memory, peak_rss_mb=0)
                    admitted = workers, memory
                else:
                    reason = blocking_reason(jobs.values(), metrics, limits)
        cleanup_events(queue, expired)
        elapsed = max(0, time.time() - started)
        if admitted:
            event(queue, token, 'admitted', run_id=run_id, wait_s=elapsed, workers=admitted[0], memory_mb=admitted[1])
            return admitted
        if reason != last_reason:
            event(queue, token, 'blocked', run_id=run_id, reason=reason, wait_s=elapsed)
            last_reason = reason
        time.sleep(min(2, max(0, deadline - time.time())))
    queue.release(token)
    event(queue, token, 'stopped', run_id=run_id, reason='deadline', wait_s=max(0, time.time()-started), exit_code=75)
    return None



def terminate(group, tree=None):
    if tree is not None:
        return tree.reap(processes)
    try:
        os.killpg(group, signal.SIGTERM)
        time.sleep(1)
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin may return EPERM for a group containing only unreaped zombies.
        # A live inaccessible group is a real cleanup failure, not success.
        if any(p['group'] == group for p in processes().values()):
            raise



def watch(group, seconds, memory, marker, token=None, phase='unknown', telemetry_root=None, run_id=None, peak_file=None):
    started = time.monotonic()
    deadline = started + seconds
    tree = Tree(group)
    stopping = [False]
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[sig] = signal.signal(sig, lambda _s, _f: stopping.__setitem__(0, True))
    code, reason, peak = 0, 'complete', 0
    next_metrics, metrics = 0, {}
    root = telemetry_root or Path.home() / '.local/state/agent-ci/telemetry'
    ident = run_id or token or str(group)
    try:
        percent, maximum = kill_limits()
        if token:
            own = processes().get(os.getpid())
            if own:
                Queue().update(token, watcher=os.getpid(), watcher_start=own['start'], tree_marker=str(marker)+'.tree')
        tree.save(str(marker) + '.tree')
        while not stopping[0]:
            previous_members = dict(tree.known)
            members = tree.observe(processes())
            if tree.known != previous_members:
                tree.save(str(marker) + '.tree')
            if not members:
                break
            rss = sum(p['rss'] for p in members.values()) / 1024
            peak = max(peak, rss)
            now = time.monotonic()
            if now >= next_metrics:
                metrics = sample()
                next_metrics = now + 2
            if budget_exceeded(rss, memory, percent, maximum):
                code, reason = 75, 'memory-budget'
            elif metrics['pressure'] >= 4:
                code, reason = 75, 'pressure'
            elif now >= deadline:
                code, reason = 74, 'deadline'
            if code:
                # Publish before killing the command: its parent may immediately
                # request watchdog shutdown when wait() returns.
                Path(marker).write_text(str(code))
                break
            time.sleep(0.5)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        if not stopping[0]:
            code, reason = 74, 'monitor-unavailable'
            Path(marker).write_text(str(code))
            raise
    finally:
        try:
            clean = terminate(group, tree) if code else tree.reap(processes)
            tree.save(str(marker) + '.tree')
        except (OSError, ValueError, subprocess.SubprocessError):
            clean = False
        if not clean:
            code, reason = 76, 'cleanup-incomplete'
        ci_telemetry.emit(root, 'cleanup', run_id=ident, exit_code=0 if clean else 76,
                          reason='complete' if clean else 'cleanup-incomplete')
    if peak_file:
        path = Path(peak_file)
        old = int(path.read_text()) if path.exists() else 0
        path.write_text(str(max(math.ceil(peak), old)))
    if token:
        try:
            Queue().record_peak(token, peak)
        except (OSError, ValueError, KeyError):
            pass  # A diagnostic peak must not replace the check's verdict.
    else:
        print(f'== memory peak {math.ceil(peak)} MB of {memory} granted', flush=True)
    if code:
        Path(marker).write_text(str(code))
        message = {'memory-budget': 'busy: memory budget exceeded',
                   'pressure': 'busy: critical host memory pressure',
                   'deadline': 'timeout after %s seconds' % seconds,
                   'monitor-unavailable': 'lost-run: resource monitor unavailable',
                   'cleanup-incomplete': 'runner cleanup pending: process tree not reaped'}
        print(message[reason], flush=True)
        ci_telemetry.emit(root, 'stopped', run_id=ident, reason=reason, exit_code=code)
    for sig, handler in previous.items():
        signal.signal(sig, handler)
    return code



def worker_env(workers):
    result = dict(AGENT_CI_WORKERS=str(workers), AGENT_CI_ADMITTED='1',
                  CMAKE_BUILD_PARALLEL_LEVEL=str(workers), MAKEFLAGS='-j' + str(workers),
                  CARGO_BUILD_JOBS=str(workers), OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
                  MKL_NUM_THREADS='1', VECLIB_MAXIMUM_THREADS='1', NUMEXPR_NUM_THREADS='1')
    prior = os.environ.get('VITEST_MAX_WORKERS', '')
    result['VITEST_MAX_WORKERS'] = str(min(workers, int(prior))) if prior.isdigit() and int(prior) > 0 else str(workers)
    return result


def local_run(args, queue, limits):
    token = uuid.uuid4().hex
    child = monitor = None
    rc = 74
    marker = queue.root / (token + '.stop')
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        print('== resources queued (local)', flush=True)
        admitted = acquire(queue, token, os.getpid(), time.time() + args.wait, limits, 1)
        if not admitted:
            print('busy: resource queue deadline reached', flush=True)
            return 75
        workers, memory = admitted
        print(f'== resources workers={workers} memory_mb={memory}', flush=True)
        child = subprocess.Popen(['nice', '-n', '10', '/bin/bash', '-c', args.command],
                                 env=dict(os.environ, **worker_env(workers), AGENT_CI_TOKEN=token), start_new_session=True)
        queue.update(token, group=child.pid)
        monitor = subprocess.Popen([sys.executable, __file__, 'watch', '--group', str(child.pid),
                                    '--token', token, '--phase', 'check', '--timeout', str(args.timeout), '--memory', str(memory), '--marker', str(marker)], start_new_session=True)
        rc = child.wait()
        rc = rc if rc >= 0 else 128 - rc
    except KeyboardInterrupt:
        rc = 74
    finally:
        if monitor:
            monitor.terminate()
            try:
                monitor.wait(timeout=30)
            except subprocess.TimeoutExpired:
                monitor.kill()
                monitor.wait()
                rc = 76
        if child:
            path = Path(str(marker) + '.tree')
            try:
                if path.exists():
                    tree = Tree.load(path)
                    if not tree.reap(processes):
                        rc = 76
                    tree.save(path)
                else:
                    terminate(child.pid)
                child.wait(timeout=5)
            except (OSError, ValueError, subprocess.SubprocessError):
                rc = 76
        if not queue.release(token):
            rc = 76
        # Retain a small stop receipt for diagnostics; no command text is stored.

    return 76 if rc == 76 else int(marker.read_text()) if marker.exists() else rc

        # Retain a small stop receipt for diagnostics; no command text is stored.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['acquire', 'release', 'mark', 'watch', 'status', 'run', 'local-allowed', 'scheduler-version', 'cancel', 'verify', 'verify-resume', 'active-count', 'quarantined-projects', 'recover-pending', 'event', 'telemetry', 'reap', 'profile', 'withdraw'])
    parser.add_argument('--json', action='store_true', help='status is always JSON')
    parser.add_argument('--token')
    parser.add_argument('--run-id')
    parser.add_argument('--submitted', type=int)
    parser.add_argument('--event', choices=sorted(ci_telemetry.EVENTS))
    parser.add_argument('--reason', choices=sorted(ci_telemetry.REASONS))
    parser.add_argument('--exit-code', type=int)
    parser.add_argument('--command-digest')
    parser.add_argument('--context')
    parser.add_argument('--project')
    parser.add_argument('--owner', type=int)
    parser.add_argument('--deadline', type=int)
    parser.add_argument('--mode', choices=['local', 'runner'], default='runner')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--group', type=int)
    parser.add_argument('--marker')
    parser.add_argument('--memory', type=int)
    parser.add_argument('--memory-mb', type=int, default=0)
    parser.add_argument('--peak-file')
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--wait', type=int, default=10800)
    parser.add_argument('--command')
    parser.add_argument('--request-file')
    parser.add_argument('--park-envelope')
    parser.add_argument('--park-adopt', action='store_true')
    parser.add_argument('--park-nonce')
    parser.add_argument('--result', type=int, default=74)
    parser.add_argument('--quarantine', action='store_true')
    parser.add_argument('--actual-memory', type=int)
    parser.add_argument('--generation', type=int)
    parser.add_argument('--to', help='withdraw: the pool runner the client moves the job to')
    parser.add_argument('--phase', choices=sorted(ci_telemetry.PHASES | {'queued', 'admitted', 'maintenance'}), default='unknown')
    args = parser.parse_args()
    try:
        if args.action == 'profile':
            import ci_profiles
            if not all(re.fullmatch(r'[0-9a-f]{64}', value or '') for value in (args.command_digest, args.context)):
                raise ValueError('profile requires command and config digests')
            print(ci_profiles.key(args.project, args.command_digest, sys.platform+'-'+os.uname().machine,
                                  args.context, args.phase, 'unknown', args.workers))
            return 0
        if args.action == 'reap':
            path = Path(str(args.marker) + '.tree')
            if not path.exists():
                return 0  # Legacy runs had no tree record.
            tree = Tree.load(path)
            clean = tree.reap(processes)
            tree.save(path)
            return 0 if clean else 76
        if args.action == 'watch':
            try:
                return watch(args.group, args.timeout, args.memory, args.marker, args.token, args.phase, run_id=args.run_id, peak_file=args.peak_file)
            except (OSError, ValueError, subprocess.SubprocessError):
                Path(args.marker).write_text('74')
                print('lost-run: resource monitor unavailable', flush=True)
                terminate(args.group)
                return 74
        if args.action == 'local-allowed':
            local_allowed()
            return 0
        if args.action == 'scheduler-version':
            print(scheduler_version())
            return 0
        queue = Queue()
        if args.action == 'withdraw':
            import scheduler_withdraw
            try:
                if scheduler_version() != 'v2' or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', args.to or ''):
                    raise ValueError('withdraw needs scheduler v2 and a runner name')
                with queue.transaction() as data:
                    scheduler_withdraw.withdraw(data, args.token, args.generation, args.to, time.time())
            except ValueError as error:
                print('remote-ci: withdraw refused (%s)' % error, file=sys.stderr)
                return 1
            return 0
        if args.action == 'telemetry':
            print(json.dumps(ci_telemetry.summary(queue.root / 'telemetry'), sort_keys=True))
        elif args.action == 'event':
            fields = {key: value for key, value in (('reason', args.reason), ('exit_code', args.exit_code)) if value is not None}
            event(queue, args.token, args.event, run_id=args.run_id, **fields)
        elif args.action == 'quarantined-projects':
            with queue.transaction() as data:
                print(' '.join(sorted({row['project'] for row in data['jobs'].values()
                                       if row.get('version') == 2 and row.get('state') == 'quarantined'})))
        elif args.action == 'active-count':
            import scheduler_authority
            with queue.transaction() as data:
                count = sum(scheduler_authority.unresolved(row) for row in data['jobs'].values())
                count += sum(1 for _ in (Path.home()/'ci').glob('projects/*/results/*.cleanup'))
            print(count)
        elif args.action == 'recover-pending':
            import scheduler_authority
            scheduler_authority.recover_pending(queue)
        elif args.action == 'verify':
            import scheduler_process
            procs = scheduler_process.snapshot()
            with queue.transaction() as data:
                row = data['jobs'].get(args.token, {})
                records = row.get('groups', {}).values()
                if (row.get('version') != 2 or row.get('protocol') != 2 or
                        row.get('state') != 'running' or row.get('phase') != args.phase or
                        row.get('lease_generation') != args.generation or
                        not any(record.get('generation') == args.generation and
                                scheduler_process.members(record, procs) and
                                procs.get(args.owner, {}).get('group') == record['group'] for record in records)):
                    raise ValueError('maintenance needs a live scheduler phase')
        elif args.action == 'verify-resume':
            import scheduler_process
            procs = scheduler_process.snapshot()
            with queue.transaction() as data:
                row = data['jobs'].get(args.token, {})
                owner = procs.get(args.owner, {})
                if (row.get('state') != 'starting' or row.get('park_version') != 1 or
                        row.get('resume_nonce') != args.park_nonce or row.get('resume_reaping') or
                        row.get('resume_owner') != args.owner or
                        not row.get('resume_owner_birth') or
                        owner.get('birth') != row.get('resume_owner_birth')):
                    raise ValueError('resume launcher identity changed')
        elif args.action == 'cancel':
            terminal = None
            with queue.transaction() as data:
                row = data['jobs'][args.token]
                if (row.get('version') != 2 or row.get('protocol') != 2 or
                        row.get('state') not in ('queued', 'following', 'running', 'parked', 'starting') or
                        (args.phase not in ('queued', 'admitted') and not row.get('groups')) or
                        row.get('lease_generation') != args.generation or row.get('phase') != args.phase):
                    raise ValueError('lease identity changed')
                row['cancel'] = True
                if row.get('state') == 'parked':
                    import scheduler_pause
                    scheduler_pause.terminalize_parked(data, args.token, 74, time.time())
                    terminal = dict(row)
            if terminal:
                import scheduler_pause
                import scheduler_process
                scheduler_pause.audit(queue, Path.home()/'ci', scheduler_process.snapshot())
            import scheduler_process
            scheduler_process.reconcile(queue)
        elif args.action == 'release':
            with queue.transaction() as data:
                row = data['jobs'].get(args.token, {})
                v2 = row.get('version') == 2
                if v2 and row.get('workers'):
                    print('== memory peak %s MB of %s granted' %
                          (row.get('peak_rss_mb', 0), row['memory_mb']), flush=True)
            released = queue.release(args.token, args.result, args.quarantine, args.actual_memory)
            if v2:
                if released is not None and released != args.result:
                    return released
            elif not released:
                print('runner cleanup pending: process ownership retained', file=sys.stderr)
                return 76
        elif args.action == 'mark':
            queue.update(args.token, marker=args.marker)
        elif args.action == 'acquire':
            if args.request_file:
                local_allowed()
                if scheduler_version() != 'v2':
                    raise ValueError('v2 disabled on host')
                request = json.loads(Path(args.request_file).read_text())
                if args.park_envelope and request.get('park_version') == 1:
                    request.update(park_envelope=args.park_envelope,
                                   park_adopt=args.park_adopt, park_nonce=args.park_nonce or '')
                admitted = acquire_v2(queue, args.token, args.owner, args.deadline, policy(args.mode), request, args.mode, args.submitted)
                import scheduler_withdraw
                if admitted.get('result') == scheduler_withdraw.MOVED:
                    with queue.transaction() as data:
                        dest = data['jobs'].get(args.token, {}).get('moved_to')
                    print('== moved to %s' % dest if dest else '== withdrawn', file=sys.stderr)
                    return scheduler_withdraw.MOVED
                if 'result' in admitted:
                    if admitted.get('follow_run'):
                        print('== following run %s sha %s (scheduler)' % (admitted['follow_run'], request['sha']), file=sys.stderr)
                    print('superseded' if admitted['result'] == 77 else '== followed result rc=%s' % admitted['result'], file=sys.stderr)
                    # A followed success must not execute again. Distinct protocol status.
                    return 78 if admitted['result'] == 0 else admitted['result']
                print('%s %s %s %s %s %s' % tuple(admitted[k] for k in ('workers', 'memory_mb', 'slot', 'predicted_mb', 'job_deadline', 'lane_class')))
                return 0
            admitted = acquire(queue, args.token, args.owner, args.deadline, policy(args.mode), args.workers, args.mode, args.memory_mb, args.submitted, args.run_id)
            if not admitted:
                print('busy: resource queue deadline reached', file=sys.stderr)
                return 75
            print('%s %s' % admitted)
        elif args.action == 'status':
            v2 = scheduler_version() == 'v2'
            if v2:
                import scheduler_process
                import scheduler_maintenance
                scheduler_process.reconcile(queue)
                scheduler_maintenance.start_recovery(queue)
            limits = policy(args.mode)
            with queue.transaction() as data:
                now = time.time()
                procs = processes()
                metrics = sample()
                expired = sweep(data, procs, limits.get('CLEANUP_QUARANTINE_S', 600))
                if v2 and any(r.get('version') == 2 for r in data['jobs'].values()):
                    import scheduler
                    import scheduler_process
                    scheduler.observe(data, scheduler_process.snapshot(), now)
                rows = [row for row in data['jobs'].values() if row.get('version') == 2 or live(row, procs)]
                pending = [r for r in rows if r.get('state', 'queued') in ('queued', 'following', 'running')]
                snapshot = status_snapshot(pending, metrics, limits, procs)
                cooldown = max(0, data.get('frozen_until', 0) - now) if v2 else 0
                if cooldown:
                    snapshot['can_admit'] = False
                snapshot.update(throttle_reason=data.get('throttle_reason'),
                                frozen=bool(cooldown),
                                cooldown_seconds=math.ceil(cooldown),
                                jobs={k: v for k, v in data['jobs'].items()
                                      if v.get('version') == 2 or live(v, procs)})
                print(json.dumps(snapshot))
            cleanup_events(queue, expired)
        else:
            if not args.command or args.wait < 1 or args.timeout < 1:
                raise ValueError('run needs a command and positive deadlines')
            return local_run(args, queue, policy('local'))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('lost-run: resource coordinator unavailable (%s)' % type(error).__name__, file=sys.stderr)
        return 74



if __name__ == '__main__':
    sys.exit(main())
