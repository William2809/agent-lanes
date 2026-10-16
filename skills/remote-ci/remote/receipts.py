#!/usr/bin/env python3
"""Private remote-ci measurements. Python 3.9; never store commands or environment."""
import argparse
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import threading
import time

PHASES = ('queue', 'checkout', 'install', 'database', 'template', 'clone', 'prepare', 'check', 'cleanup')
CACHE_KEYS = ('install', 'template', 'tree')
SAMPLE_INTERVAL = 2
MAX_GAP_INTERVALS = 3
UNSAMPLED = object()


def output(argv, **kwargs):
    return subprocess.check_output(argv, text=True, stderr=subprocess.DEVNULL, timeout=5, **kwargs)


def read(path):
    return json.loads(Path(path).read_text())


def write(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    new = path.with_suffix('.json.new')
    fd = os.open(str(new), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(row, stream, separators=(',', ':'), allow_nan=False)
        stream.write('\n')
    os.replace(str(new), str(path))


@contextmanager
def locked(path):
    root = Path(path).parent.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(root / '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as stream:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('receipt lock')
                time.sleep(0.02)
        yield


def mutate(path, fn):
    with locked(path):
        row = read(path)
        fn(row)
        write(path, row)


def host_sample():
    try:
        if sys.platform == 'darwin':
            vm = output(['/usr/bin/vm_stat'])
            size = int(re.search(r'page size of (\d+) bytes', vm)[1])
            values = dict((k, int(v)) for k, v in re.findall(r'^([^:\n]+):\s+(\d+)\.', vm, re.M))
            swap = output(['/usr/sbin/sysctl', '-n', 'vm.swapusage'])
            return dict(pressure=int(output(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level'])),
                        swap_used_bytes=int(float(re.search(r'used = ([\d.]+)M', swap)[1]) * 1048576),
                        pageout_bytes=values['Pageouts'] * size,
                        swapout_bytes=values['Swapouts'] * size)
        mem = dict((k, int(v) * 1024) for k, v in re.findall(r'^(\w+):\s+(\d+)', Path('/proc/meminfo').read_text(), re.M))
        vm = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
        return dict(pressure=None, swap_used_bytes=mem['SwapTotal'] - mem['SwapFree'],
                    swapout_bytes=int(vm['pswpout']) * os.sysconf('SC_PAGE_SIZE'),
                    pageout_bytes=int(vm['pgpgout']) * 1024)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return {}


def host_delta(a, b):
    return {key: b[key] - a[key] if isinstance(a.get(key), (int, float)) and
            isinstance(b.get(key), (int, float)) else None
            for key in ('pressure', 'swap_used_bytes', 'pageout_bytes', 'swapout_bytes')}


def new_phase(now, host):
    return dict(started_at=now, duration_seconds=0, peak_rss_bytes=None,
                os_write_bytes=None, sampled_cpu_seconds=None, samples=0, host_before=host, host_after=None,
                host_delta=None, rusage={}, _writes={})


def begin(path, project, run, mode, now=None, owner=None):
    for value in (project, run):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', value):
            raise ValueError('invalid identifier')
    if mode not in ('full', 'targeted'):
        raise ValueError('invalid mode')
    now = time.time() if now is None else now
    host = host_sample()
    row = dict(schema_version=1, project=project, run_id=run, mode=mode,
               runner_class=sys.platform + '-' + os.uname().machine,
               started_at=now, ended_at=None, duration_seconds=None, owner_pid=owner or os.getppid(),
               granted=dict(workers=None, memory_mb=None), current_phase='queue',
               phases={'queue': new_phase(now, host)}, exit_code=None,
               classification=None, complete_observation=False, sampler=None, postgres=None,
               cache={key: dict(outcome='not-reached', reason='not-reached') for key in CACHE_KEYS},
               turbo_size_delta_bytes=None,
               measurement=dict(rss='sampled sum of owned tree members; shared pages may repeat',
                   os_write_bytes='sampled process counters; lower bound; excludes unsampled exits',
                   host='host-wide, not attributable to this run',
                   postgres='cluster-wide; unmanaged clients and delayed flushes may contribute'))
    with locked(path):
        write(path, row)


def close_phase(row, now, host):
    phase = row['phases'][row['current_phase']]
    phase['duration_seconds'] += max(0, now - phase.pop('_resumed_at', phase['started_at']))
    phase['host_after'] = host
    delta = host_delta(phase.pop('_host_resumed', phase['host_before']), host)
    previous = phase['host_delta']
    phase['host_delta'] = delta if previous is None else {
        key: value + previous[key] if previous.get(key) is not None and value is not None else None
        for key, value in delta.items()}


def transition(path, phase, now=None, host=None):
    if phase not in PHASES:
        raise ValueError('invalid phase')
    now = time.time() if now is None else now
    host = host_sample() if host is None else host
    def apply(row):
        close_phase(row, now, host)
        if phase not in row['phases']:
            row['phases'][phase] = new_phase(now, host)
        else:
            row['phases'][phase]['_resumed_at'] = now
            row['phases'][phase]['_host_resumed'] = host
        row['current_phase'] = phase
    mutate(path, apply)


def grant(path, workers, memory):
    mutate(path, lambda row: row.update(granted=dict(workers=int(workers), memory_mb=int(memory))))


def cache(path, key, outcome, reason):
    if key not in CACHE_KEYS or outcome not in ('hit', 'miss', 'follower', 'disabled', 'not-reached'):
        raise ValueError('invalid cache event')
    if reason not in ('input-match', 'inputs-changed-or-missing', 'not-configured', 'not-cached',
                      'tree-pass', 'following', 'not-reached', 'template-missing', 'template-ready'):
        raise ValueError('invalid cache reason')
    mutate(path, lambda row: row['cache'].update({key: dict(outcome=outcome, reason=reason)}))


def record_sample(path, rss, writes):
    def apply(row):
        if row['ended_at'] is not None:
            return
        phase = row['phases'][row['current_phase']]
        phase['peak_rss_bytes'] = max(phase['peak_rss_bytes'] or 0, rss)
        phase['samples'] += 1
        previous = row.setdefault('_previous_writes', {})
        for pid, value in writes.items():
            phase['_writes'][pid] = phase['_writes'].get(pid, 0) + max(0, value - previous.get(pid, 0))
            previous[pid] = max(previous.get(pid, 0), value)
        if writes:
            phase['os_write_bytes'] = sum(phase['_writes'].values())
    mutate(path, apply)


def process_writes(procs, group=None):
    result = {}
    lib = ctypes.CDLL('/usr/lib/libproc.dylib') if sys.platform == 'darwin' else None
    def depth(pid):
        seen = set()
        while pid in procs and pid not in seen:
            seen.add(pid)
            pid = procs[pid].get('parent')
        return len(seen)
    # Linux parent counters include reaped children. Read parents first, then
    # use a single tree high-water mark, never sum dead-child high-water marks.
    for pid in sorted(procs, key=depth):
        proc = procs[pid]
        if group is not None and proc['group'] != group:
            continue
        try:
            if lib:
                # rusage_info_v2: UUID[16], then 18 uint64 fields (XNU resource.h).
                data = (ctypes.c_uint64 * 20)()
                if lib.proc_pid_rusage(pid, 2, ctypes.byref(data)) != 0:
                    continue
                birth, count = str(data[10]), data[19]
            else:
                birth = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()[19]
                count = int(re.search(r'^write_bytes:\s+(\d+)', Path('/proc/%s/io' % pid).read_text(), re.M)[1])
            result[str(pid) + ':' + birth] = count
        except (OSError, ValueError, TypeError):
            continue
    if sys.platform.startswith('linux') and result:
        return {'linux-tree': sum(result.values())}
    return result


def monitor(path, owner, tree_path=None):
    """Sample in memory; avoid rewriting large PG snapshots every two seconds."""
    birth, phase, pending, previous, previous_cpu = None, None, {}, {}, {}
    stopped = threading.Event()
    owner_lost = False
    flushed = time.monotonic()
    last_tick = flushed
    health = dict(interval_seconds=SAMPLE_INTERVAL, max_gap_intervals=MAX_GAP_INTERVALS,
                  started_at=time.time(), first_sample_at=None, last_sample_at=None, ended_at=None,
                  clean_exit=False, samples=0, gap_count=0, max_gap_seconds=0)
    def stop(*_):
        stopped.set()
    signal.signal(signal.SIGTERM, stop)
    def flush(final=False):
        if not pending and not final:
            return
        # A killed owner cannot join this writer before removing its files.
        # Do not recreate its directories or publish orphan buffered samples.
        if owner_lost or not Path(path).is_file():
            return
        try:
            os.kill(owner, 0)
        except ProcessLookupError:
            return
        def apply(row):
            for name, sample in pending.items():
                target = row['phases'][name]
                target['peak_rss_bytes'] = max(target['peak_rss_bytes'] or 0, sample['peak_rss_bytes'])
                target['samples'] += sample['samples']
                if sample['os_write_bytes'] is not None:
                    target['os_write_bytes'] = (target['os_write_bytes'] or 0) + sample['os_write_bytes']
                if sample['sampled_cpu_seconds'] is not None:
                    target['sampled_cpu_seconds'] = (target.get('sampled_cpu_seconds') or 0) + sample['sampled_cpu_seconds']
            row['sampler'] = dict(health)
            row['complete_observation'] = observation_complete(row)
        mutate(path, apply)
        pending.clear()
    try:
        while not stopped.is_set():
            try:
                os.kill(owner, 0)
            except ProcessLookupError:
                owner_lost = True
                return 0
            row = read(path)
            if row['ended_at'] is not None:
                health['clean_exit'] = True
                return 0
            current = row['current_phase']
            if current != phase or time.monotonic() - flushed >= 60:
                flush()
                phase, flushed = current, time.monotonic()
            procs = {}
            for line in output(['ps', '-axo', 'pid=,ppid=,pgid=,rss=,stat=,time=,lstart=']).splitlines():
                pid, parent, group, rss, state, cpu, start = line.split(None, 6)
                if not state.startswith('Z'):
                    procs[int(pid)] = dict(parent=int(parent), group=int(group), rss=int(rss),
                                           cpu=cpu_seconds(cpu), start=start)
            if owner not in procs or (birth is not None and procs[owner]['start'] != birth):
                owner_lost = True
                return 0
            birth = procs[owner]['start']
            members = None
            if tree_path is not None and Path(tree_path).exists():
                # The watchdog retains observed descendants after reparenting.
                # Read its journal; never publish a second ancestry-only peak.
                from process_tree import Tree
                try:
                    tree = Tree.load(tree_path)
                    if tree.known:
                        members = tree.observe(procs)
                except (OSError, ValueError, KeyError, TypeError, OverflowError):
                    members = {}
                    health['gap_count'] += 1
            if members is None:
                # Queue/checkout/cleanup may have no active supervised tree.
                owned, excluded = {owner}, {os.getpid()}
                for selected in (excluded, owned):
                    while True:
                        children = {pid for pid, p in procs.items() if p['parent'] in selected}
                        if children <= selected:
                            break
                        selected.update(children)
                members = {pid: p for pid, p in procs.items() if pid in owned - excluded}
            if members:
                tick = time.monotonic()
                gap = tick - last_tick
                last_tick = tick
                health['max_gap_seconds'] = max(health['max_gap_seconds'], gap)
                health['gap_count'] += int(gap > SAMPLE_INTERVAL * MAX_GAP_INTERVALS)
                health['samples'] += 1
                health['last_sample_at'] = time.time()
                if health['first_sample_at'] is None:
                    health['first_sample_at'] = health['last_sample_at']
                sample = pending.setdefault(phase, dict(peak_rss_bytes=0, samples=0,
                                                        os_write_bytes=None, sampled_cpu_seconds=None))
                sample['peak_rss_bytes'] = max(sample['peak_rss_bytes'], sum(p['rss'] for p in members.values()) * 1024)
                sample['samples'] += 1
                counters = {str(pid) + ':' + p['start']: p['cpu'] for pid, p in members.items()}
                sample['sampled_cpu_seconds'] = (sample['sampled_cpu_seconds'] or 0) + sum(
                    max(0, value - previous_cpu.get(pid, 0)) for pid, value in counters.items())
                for pid, value in counters.items():
                    previous_cpu[pid] = max(previous_cpu.get(pid, 0), value)
                writes = process_writes(members)
                if writes:
                    sample['os_write_bytes'] = (sample['os_write_bytes'] or 0) + sum(
                        max(0, value - previous.get(pid, 0)) for pid, value in writes.items())
                    for pid, value in writes.items():
                        previous[pid] = max(previous.get(pid, 0), value)
            stopped.wait(SAMPLE_INTERVAL)
        health['clean_exit'] = True
    finally:
        health['ended_at'] = time.time()
        flush(final=True)


def cpu_seconds(value):
    """ps TIME uses [days-][hours:]minutes:seconds, with optional fractions."""
    days, clock = value.split('-', 1) if '-' in value else ('0', value)
    result = 0.0
    for part in clock.split(':'):
        result = result * 60 + float(part)
    return int(days) * 86400 + result


def observation_complete(row):
    health = row.get('sampler') or {}
    end = row.get('ended_at')
    limit = SAMPLE_INTERVAL * MAX_GAP_INTERVALS
    return bool(end is not None and health.get('clean_exit') and health.get('samples', 0) > 0
                and health.get('gap_count') == 0 and health.get('last_sample_at') is not None
                and health.get('first_sample_at') is not None
                and health['first_sample_at'] - row['started_at'] <= limit
                and end - health['last_sample_at'] <= limit)


def parse_time(text):
    result = {}
    match = re.search(r'^\s*[\d.]+\s+real\s+([\d.]+)\s+user\s+([\d.]+)\s+sys\s*$', text, re.M)
    if match:
        result.update(user_seconds=float(match[1]), system_seconds=float(match[2]),
                      cpu_seconds=float(match[1]) + float(match[2]))
    for key, label in [('max_rss_bytes', 'maximum resident set size'),
                       ('block_output_operations', 'block output operations')]:
        match = re.search(r'^\s*(\d+)\s+' + label + r'\s*$', text, re.M)
        if match:
            result[key] = int(match[1])
    return result


def record_rusage(path, data):
    def apply(row):
        phase = row['phases'][row['current_phase']]
        phase['timing_status'] = 'available' if data else 'timing unavailable'
        usage = phase['rusage']
        if 'max_rss_bytes' in data:
            usage['max_rss_bytes'] = max(usage.get('max_rss_bytes', 0), data['max_rss_bytes'])
        if 'block_output_operations' in data:
            usage['block_output_operations'] = usage.get('block_output_operations', 0) + data['block_output_operations']
        for key in ('user_seconds', 'system_seconds', 'cpu_seconds'):
            if key in data:
                usage[key] = usage.get(key, 0) + data[key]
    mutate(path, apply)


PG_SQL = """select json_build_object(
 'version', current_setting('server_version_num')::int,
 'wal', (select row_to_json(w) from pg_stat_wal w),
 'io', (select coalesce(json_agg(i), '[]'::json) from pg_stat_io i),
 'checkpointer', (select row_to_json(c) from pg_stat_checkpointer c),
 'databases', (select json_agg(oid) from pg_database));"""


def pg_sample():
    try:
        env = dict(os.environ, PGCONNECT_TIMEOUT='2', PGOPTIONS='-c statement_timeout=2000')
        return json.loads(output(['psql', '-X', '-w', '-d', 'postgres', '-Atq', '-c', PG_SQL], env=env))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def counter_delta(a, b):
    same = a.get('stats_reset') == b.get('stats_reset')
    result = {key: (value - a[key] if same and isinstance(a.get(key), (int, float)) and value >= a[key] else None)
              for key, value in b.items() if isinstance(value, (int, float))}
    result.update(stats_reset_before=a.get('stats_reset'), stats_reset_after=b.get('stats_reset'))
    return result


def pg_delta(a, b):
    if a is None or b is None:
        return None
    key = lambda row: tuple(row.get(k) for k in ('backend_type', 'object', 'context'))
    old = {key(row): row for row in a['io']}
    io = [dict(counter_delta(old.get(key(row), {}), row),
               **{k: row[k] for k in ('backend_type', 'object', 'context')}) for row in b['io']]
    return dict(wal=counter_delta(a['wal'], b['wal']), io=io,
                checkpointer=counter_delta(a['checkpointer'], b['checkpointer']),
                databases_created_observed=len(set(b['databases']) - set(a['databases'])),
                databases_dropped_observed=len(set(a['databases']) - set(b['databases'])))


def postgres(path, cluster, snapshot=UNSAMPLED, now=None, end=False):
    with locked(path):
        # SQL and the active index form one window boundary. No sample may pass
        # another run's final snapshot before its overlap has been registered.
        if snapshot is UNSAMPLED:
            snapshot = pg_sample()
        now = time.time() if now is None else now
        row = read(path)
        pg = row.get('postgres')
        if pg is None:
            pg = dict(cluster=cluster, started_at=now, ended_at=None, overlapping_runs=[],
                      attribution='cluster-window', before=snapshot, after=None, delta=None, windows=[])
            row['postgres'] = pg
        if pg['ended_at'] is not None:
            return
        root = Path(path).parent.parent
        index = root / '.postgres-active'
        identities = read(index) if index.exists() else []
        own = row['project'] + '/' + row['run_id']
        active = []
        for identity in identities:
            if identity == own:
                continue
            other_path = root / (identity + '.json')
            try:
                other = read(other_path)
            except (OSError, ValueError):
                continue
            opg = other.get('postgres')
            if not opg or opg['ended_at'] is not None:
                continue
            try:
                os.kill(other['owner_pid'], 0)
            except ProcessLookupError:
                continue
            active.append(identity)
            if opg['cluster'] != cluster:
                continue
            for target, source in ((pg, other), (opg, row)):
                ident = source['project'] + '/' + source['run_id']
                target['overlapping_runs'] = sorted(set(target['overlapping_runs'] + [ident]))
                target['attribution'] = 'cluster-window'
            write(other_path, other)
        prior = pg['after'] if pg['after'] is not None else pg['before']
        pg['windows'].append(dict(phase=row['current_phase'], at=now, delta=pg_delta(prior, snapshot)))
        pg['after'] = snapshot
        pg['delta'] = pg_delta(pg['before'], snapshot)
        if pg['delta'] is not None:
            for key in ('databases_created_observed', 'databases_dropped_observed'):
                pg['delta'][key] = sum(window['delta'][key] for window in pg['windows'] if window['delta'])
        if end:
            pg['ended_at'] = now
        else:
            active.append(own)
        write(path, row)
        write(index, sorted(set(active)))


def turbo_size(directory):
    # Allocated size is cache occupancy, never a write counter.
    try:
        return int(output(['du', '-sk', str(directory)]).split()[0]) * 1024 if Path(directory).exists() else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def finish(path, code, now=None, host=None, classification=None):
    if classification not in (None, 'candidate', 'infra', 'busy', 'superseded'):
        raise ValueError('invalid classification')
    now = time.time() if now is None else now
    host = host_sample() if host is None else host
    def apply(row):
        if row['current_phase'] is not None:
            close_phase(row, now, host)
        row.update(ended_at=now, duration_seconds=max(0, now - row['started_at']), exit_code=code,
                   classification=classification or ('candidate' if code in (0, 1) else 'busy' if code == 75 else 'infra'),
                   current_phase=None)
        row['complete_observation'] = observation_complete(row)
        for phase in row['phases'].values():
            phase.pop('_writes', None)
        row.pop('_previous_writes', None)
    mutate(path, apply)


def prune(directory, keep=200, max_bytes=50 * 1024 * 1024):
    rows = []
    total_bytes = 0
    for path in Path(directory).glob('*.json'):
        try:
            stat = path.stat()
            size = stat.st_size
            total_bytes += size
            try:
                row = read(path)
                if not isinstance(row, dict):
                    raise ValueError('invalid receipt')
            except ValueError:
                # Atomic writes never expose partial JSON. Corrupt receipts
                # still consume the byte budget and have no live owner.
                rows.append((stat.st_mtime, path, size))
                continue
            ended = row.get('ended_at')
            if ended is None and row.get('owner_pid'):
                try:
                    os.kill(row['owner_pid'], 0)
                except ProcessLookupError:
                    ended = row['started_at']  # orphan observation, never a completed check
            if ended is not None:
                rows.append((ended, path, size))
        except (ValueError, OSError):
            continue
    old = []
    remaining = len(rows)
    for _, path, size in sorted(rows):
        if remaining <= max(0, keep) and total_bytes <= max_bytes:
            break
        old.append(str(path))
        remaining -= 1
        total_bytes -= size
    stale = [str(path) for path in Path(directory).glob('*.json.new')
             if time.time() - path.stat().st_mtime > 3600]
    if old or stale:
        if Path(directory).parent.name == 'receipts':
            results = Path(directory).parent.parent / 'projects' / Path(directory).name / 'results'
            timing = [str(results / (Path(path).stem + '.time')) for path in old
                      if (results / (Path(path).stem + '.time')).exists()]
        else:
            timing = []
        old += [str(Path(path).with_suffix('.json.new')) for path in old
                if Path(path).with_suffix('.json.new').exists()]
        old += timing
        old = list(dict.fromkeys(old + stale))
        trash = Path(__file__).with_name('ctrash.sh')
        command = ['/bin/sh', str(trash)] if trash.is_file() else ['ctrash']
        for start in range(0, len(old), 128):
            # Runner data goes to ctrash's dated folders (expire after 7 days), never the Finder Trash.
            subprocess.run([*command, *old[start:start + 128]], stdout=subprocess.DEVNULL,
                           env=dict(os.environ, CTRASH_DATED='1'),
                           stderr=subprocess.DEVNULL, timeout=10, check=True)


def quantiles(values):
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    return dict(median=statistics.median(values) if values else None,
                p90=values[math.ceil(len(values) * .9) - 1] if values else None, samples=len(values))


def summarize(root, project=None):
    groups = {}
    for path in Path(root).glob('*/*.json'):
        try:
            row = read(path)
            if row.get('schema_version') != 1 or (project and row['project'] != project):
                continue
            groups.setdefault((row['project'], row['mode']), []).append(row)
        except (OSError, ValueError, KeyError):
            continue
    result = []
    for (project, mode), rows in sorted(groups.items()):
        complete = [r for r in rows if r.get('ended_at') is not None]
        summary = dict(project=project, mode=mode, runs=len(rows), finished=len(complete),
                       incomplete=len(rows) - len(complete), duration_seconds=quantiles(r['duration_seconds'] for r in complete))
        for metric in ('peak_rss_bytes', 'os_write_bytes', 'sampled_cpu_seconds', 'cpu_seconds'):
            values = []
            for row in complete:
                samples = [(p.get('rusage', {}) if metric == 'cpu_seconds' else p).get(metric)
                           for p in row['phases'].values()]
                samples = [s for s in samples if s is not None]
                values.append((max(samples) if metric == 'peak_rss_bytes' else sum(samples)) if samples else None)
            summary[metric] = quantiles(values)
        # Overlapping windows are not per-run WAL observations; do not sum them twice.
        summary['wal_bytes'] = quantiles((r.get('postgres') or {}).get('delta', {}).get('wal', {}).get('wal_bytes')
            for r in complete if (r.get('postgres') or {}).get('delta') and
            (r.get('postgres') or {}).get('attribution') == 'cluster-window' and
            not (r.get('postgres') or {}).get('overlapping_runs'))
        for key in CACHE_KEYS:
            outcomes = [r['cache'][key]['outcome'] for r in complete]
            known = [v for v in outcomes if v in ('hit', 'miss', 'follower')]
            summary[key + '_hit_rate'] = known.count('hit') / len(known) if known else None
            summary[key + '_samples'] = len(known)
            if key == 'tree':
                summary['tree_follower_rate'] = known.count('follower') / len(known) if known else None
        result.append(summary)
    return dict(schema_version=1, groups=result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['begin', 'phase', 'grant', 'profile', 'cache', 'postgres', 'pg-end', 'rusage', 'timing-unavailable', 'turbo-before', 'turbo-after', 'finish', 'finalize', 'monitor', 'summary'])
    parser.add_argument('path', type=Path)
    parser.add_argument('values', nargs='*')
    args = parser.parse_args()
    action, path, values = args.action, args.path, args.values
    if action == 'monitor':
        return monitor(path, int(values[0]), values[1] if len(values) > 1 else None)
    if action == 'begin':
        begin(path, *values[:3], owner=int(values[3]) if len(values) > 3 else None)
    elif action == 'phase':
        transition(path, values[0])
    elif action == 'grant':
        grant(path, *map(int, values))
    elif action == 'profile':
        if len(values) != 1 or not re.fullmatch(r'[0-9a-f]{64}', values[0]):
            raise ValueError('invalid scheduler profile')
        mutate(path, lambda row: row.update(scheduler_profile=values[0]))
    elif action == 'cache':
        cache(path, *values)
    elif action in ('postgres', 'pg-end'):
        # The hash identifies a cluster route without disclosing host/user/password.
        cluster = hashlib.sha256((os.environ.get('PGHOST', '') + ':' + os.environ.get('PGPORT', '')).encode()).hexdigest()[:16]
        postgres(path, cluster, end=action == 'pg-end')
    elif action == 'rusage':
        data = parse_time(Path(values[0]).read_text())
        record_rusage(path, data)
    elif action == 'timing-unavailable':
        mutate(path, lambda row: row['phases'][row['current_phase']].update(timing_status='timing unavailable'))
    elif action.startswith('turbo-'):
        size = turbo_size(values[0])
        if action == 'turbo-before':
            mutate(path, lambda row: row.update(turbo_size_before_bytes=size))
        else:
            def apply(row):
                before = row.get('turbo_size_before_bytes')
                row['turbo_size_delta_bytes'] = size - before if size is not None and before is not None else None
            mutate(path, apply)
    elif action in ('finish', 'finalize'):
        if action == 'finalize':
            # run.sh has already published .exit and sent TERM. Allow one
            # bounded flush; the shell's one-second deadline covers this tail.
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline:
                if (read(path).get('sampler') or {}).get('ended_at') is not None:
                    break
                time.sleep(.02)
        finish(path, int(values[0]), classification=values[1] if len(values) > 1 else None)
        prune(path.parent)
    else:
        print(json.dumps(summarize(path, values[0] if values else None)))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print('== receipt unavailable', file=sys.stderr)
        sys.exit(1)
