#!/usr/bin/env python3
"""Read-only remote-ci runner snapshot: prints one JSON object and changes nothing.

Run over SSH with `ssh HOST python3 - < snapshot.py`. Stdlib only, macOS and Linux.
Reads ~/.local/state/agent-ci/queue.json (no lock taken), ~/ci/lock, per-project
slots/logs/results. Never prints config values, command text or raw log lines.
"""
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True
HOME = os.environ.get('CI_BOARD_HOME') or os.path.expanduser('~')
CI = os.path.join(HOME, 'ci')
QUEUE = os.path.join(HOME, '.local', 'state', 'agent-ci', 'queue.json')
NOW = time.time()
WAIT = 10800          # remote-ci default REMOTE_CI_WAIT_TIMEOUT; older unfinished logs are lost
HISTORY = 50
# Logs are read as the first HEAD bytes (the run header) and the last TAIL bytes (timing and result
# lines), within BUDGET bytes per poll. The board passes the finished runs it already holds as
# argv[1] "run=inode.size.mtime.exitmtime,..."; those are not read again (a "same" stub instead).
HEAD, TAIL = 16384, 65536
WHOLE = HEAD + TAIL
BUDGET = 4 * 1048576
READ = [0]            # bytes read this poll
KNOWN = dict(item.split('=', 1) for item in (sys.argv[1] if len(sys.argv) > 1 else '').split(',') if '=' in item)
MARKERS = ('== timing', '== template', '== check-result', '== install', '== resources', '== cached',
           '== following', '== finished', '== cleanup', '== git ', '== stale', '== Error')
TOP = ('checkout', 'install', 'database', 'prepare', 'check')
RUN_ID = re.compile(r'^(\d{8}-\d{6})-([0-9a-f]{4,40})-')
TIMING = re.compile(r'^== timing ([a-z-]+) (\d+)s')
HEADER = re.compile(r'^== \S+ run \S+ sha ([0-9a-f]+) slot (\d+)/(\d+|exclusive) start (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)')
FINISHED = re.compile(r'^== finished rc=(\d+)')


def out(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=1.5).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def metrics():
    m = dict(cores=os.cpu_count() or 1, load=None, mem_total_mb=None, mem_available_mb=None, pressure=None)
    try:
        m['load'] = round(os.getloadavg()[0], 2)
    except OSError:
        pass
    if sys.platform == 'darwin':
        total = out(['/usr/sbin/sysctl', '-n', 'hw.memsize']).strip()
        if total.isdigit():
            m['mem_total_mb'] = int(total) // 1048576
        vm = out(['/usr/bin/vm_stat'])
        size = re.search(r'page size of (\d+) bytes', vm)
        if size:
            counts = {k: int(v) for k, v in re.findall(r'^([^:\n]+):\s+(\d+)\.', vm, re.M)}
            pages = sum(counts.get(k, 0) for k in ('Pages free', 'Pages inactive', 'Pages speculative'))
            m['mem_available_mb'] = pages * int(size[1]) // 1048576
        level = out(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level']).strip()
        m['pressure'] = int(level) if level.isdigit() else None
    else:
        try:
            with open('/proc/meminfo') as f:
                mem = f.read()
            for key, name in (('MemTotal', 'mem_total_mb'), ('MemAvailable', 'mem_available_mb')):
                found = re.search(r'^%s:\s+(\d+)' % key, mem, re.M)
                if found:
                    m[name] = int(found[1]) // 1024
            m['pressure'] = 1
        except OSError:
            pass
    # Fixture hook: only a local test HOME (CI_BOARD_HOME) may pin metrics; never set over SSH.
    if os.environ.get('CI_BOARD_HOME'):
        try:
            with open(os.path.join(HOME, '.ci-board-metrics.json')) as f:
                m.update(json.load(f))
        except (OSError, ValueError):
            pass
    return m


def policy():
    """Host admission limits (numbers only), the way resources.py reads them."""
    values = {k: v for k, v in os.environ.items() if k.startswith('AGENT_CI_')}
    keys = ('AGENT_CI_MAX_JOBS', 'AGENT_CI_MAX_WORKERS')
    for name in ('agent-lanes', 'agent-lanes'):
        # Read as KEY=VALUE text, never sourced: only these two numeric keys are kept.
        values.update({k: v for k, v in read_config(os.path.join(HOME, '.config', name, 'config'), keys).items() if v})
    result = {}
    for key, default in (('MAX_JOBS', 1), ('MAX_WORKERS', 2)):
        raw = str(values.get('AGENT_CI_' + key, default))
        result[key.lower()] = int(raw) if raw.isdigit() and int(raw) > 0 else default
    return result


def read_config(path, keys):
    """KEY=VALUE lines of a shell config read as text, never sourced or executed (ci-board's reader)."""
    values = {}
    try:
        with open(path, errors='replace') as f:
            lines = f.read(65536).splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, sep, value = line.partition('=')
        if not sep or key not in keys:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        else:
            value = value.split(' #', 1)[0].rstrip()
        if '$' in value or '`' in value:
            continue
        values[key] = value
    return values


def start_key(text):
    """ps lstart in any locale order ("Thu Oct 15" or "Thu 15 Oct"), whitespace-insensitive."""
    text = ' '.join(str(text).split())
    for fmt in ('%a %b %d %H:%M:%S %Y', '%a %d %b %H:%M:%S %Y'):
        try:
            return time.strftime('%Y%m%d%H%M%S', time.strptime(text, fmt))
        except ValueError:
            pass
    return text


def processes():
    """pid -> (pgid, start, rss_kb, cpu%, ppid) or None when ps is unavailable (liveness unknown).
    Also fills RUNSH (run.sh processes) and SERVICES (postgres / VM memory outside any check)."""
    text = out(['env', 'LC_ALL=C', 'ps', '-axo', 'pid=,ppid=,pgid=,rss=,pcpu=,stat=,lstart=,command='])
    RUNSH.clear()
    SERVICES.clear()
    WAITS.clear()
    if not text:
        return None
    result = {}
    for line in text.splitlines():
        parts = line.split(None, 11)
        if len(parts) >= 11 and parts[0].isdigit() and not parts[5].startswith('Z'):
            try:
                cpu = float(parts[4].replace(',', '.'))
            except ValueError:
                cpu = 0.0
            pid = int(parts[0])
            rss = int(parts[3]) if parts[3].isdigit() else 0
            result[pid] = (int(parts[2]) if parts[2].isdigit() else 0, start_key(' '.join(parts[6:11])),
                           rss, cpu, int(parts[1]) if parts[1].isdigit() else 0)
            argv = parts[11].split() if len(parts) > 11 else []
            name = os.path.basename(argv[0]) if argv else ''
            service = ('postgres' if name.startswith('postgres') else
                       'vm' if name in VM_NAMES or name.startswith('qemu-system') else None)
            if service:
                SERVICES[service] = SERVICES.get(service, 0) + rss
            names = [os.path.basename(a) for a in argv[:3]]
            tag = ('cache' if 'cache.py' in names else 'slot' if 'resources.py' in names and 'acquire' in argv[:4]
                   else 'database' if name in ('createdb', 'dropdb', 'psql') or name.startswith('pg_')
                   else 'sleep' if name == 'sleep' else None)
            if tag:
                WAITS[pid] = tag
            at = next((i for i, a in enumerate(argv[:3]) if a.endswith('/run.sh')), None)
            if at is not None and len(argv) - at - 1 in (6, 7):
                # run.sh PROJECT SHA TREE RUN 'CMD' FORCE MODE; an empty CMD vanishes from ps.
                args = argv[at + 1:]
                RUNSH[args[3]] = dict(pid=pid, project=args[0], kind='run' if len(args) == 7 else 'check')
    return result


RUNSH = {}   # run id -> {pid, project, kind}; filled by processes(); command text is never kept
SERVICES = {}   # 'postgres' / 'vm' -> RSS kB summed over those processes
WAITS = {}   # pid -> cache | slot | database | sleep, from program names only (never args or paths)
DONE_STATES = ('finished', 'superseded', 'expired', 'lost', 'rejected')   # quarantined rows still hold leftovers
VM_NAMES = ('com.apple.Virtualization.VirtualMachine', 'limactl', 'colima', 'vfkit')
V2_PHASES = ('checkout', 'install', 'template', 'database', 'prepare', 'check', 'cleanup', 'maintenance')


def wait_reason(root, procs, phase):
    """What a quiet check waits on, as a fixed category: a cache.py child (cache registry lock), Postgres
    tools (template work), the host queue's acquire, or run.sh's own `sleep 1` lock loop read by phase."""
    if procs is None or root not in procs or root <= 1:
        return None
    children = {}
    for pid, p in procs.items():
        children.setdefault(p[4], []).append(pid)
    seen, stack, found = set(), [root], set()
    while stack:
        pid = stack.pop()
        if pid not in seen:
            seen.add(pid)
            stack.extend(children.get(pid, ()))
            if WAITS.get(pid) == 'sleep':
                if procs[pid][4] == root:
                    found.add({'queued': 'slot', 'install': 'cache', 'database': 'database'}.get(phase))
            elif pid in WAITS:
                found.add(WAITS[pid])
    return next((c for c in ('cache', 'database', 'slot') if c in found), None)


def tree_usage(root, procs):
    """RSS (MB) and CPU (%) of a pid and all its descendants. The check's commands run in their own
    process groups (nice, time -l, bash -c), so the run.sh tree, not its group, is the check's cost.
    A child whose parent died is re-parented to launchd and leaves the tree."""
    if procs is None or root not in procs or root <= 1:
        return None, None
    children = {}
    for pid, p in procs.items():
        children.setdefault(p[4], []).append(pid)
    seen, stack = set(), [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, ()))
    return sum(procs[p][2] for p in seen) // 1024, round(sum(procs[p][3] for p in seen), 1)


def services():
    """Host memory that serves checks but sits outside every check's tree: postgres backends and a
    docker VM (Colima/lima, Docker Desktop), with the VM's configured size when Colima states it."""
    if os.environ.get('CI_BOARD_HOME'):
        return None
    result = {k: v // 1024 for k, v in SERVICES.items()}
    if 'vm' in result:
        size = 0
        try:
            root = os.path.join(HOME, '.colima')
            for name in os.listdir(root):
                try:
                    with open(os.path.join(root, name, 'colima.yaml')) as f:
                        found = re.search(r'^memory:\s*(\d+)', f.read(65536), re.M)
                    size += int(found[1]) * 1024 if found else 0
                except OSError:
                    pass
        except OSError:
            pass
        result['vm_size_mb'] = size or None
    return result


def group_usage(job, procs):
    """Live RSS (MB) and CPU (%) of a queue job's process groups: its owner's group, the
    group it registered, and the check's group named by its marker file."""
    if os.environ.get('CI_BOARD_HOME') and job.get('fixture_usage'):
        return tuple(job['fixture_usage'])  # fixture queue rows pin usage; owners there are pid 1
    if procs is None:
        return None, None
    groups = set()
    owner = procs.get(job.get('owner'))
    if owner:
        groups.add(owner[0])
    groups.update(job_groups(job))
    groups.discard(0)
    groups.discard(1)   # never sum launchd/init's group
    members = [p for p in procs.values() if p[0] in groups]
    if not members:
        return None, None
    return sum(p[2] for p in members) // 1024, round(sum(p[3] for p in members), 1)


def job_groups(job):
    """Process groups a queue row names: v1 `group`, v2 `process`/`groups` records, and the marker file's
    group only for rows without a tree record (resources.py's rule; a stale marker pgid can be reused)."""
    found = {job.get('group') or 0}
    records = [job.get('process')] + list((job.get('groups') or {}).values())
    found.update(r.get('group') or 0 for r in records if isinstance(r, dict))
    if not job.get('tree_marker'):
        found.add(marker_group(job))
    return {g for g in found if isinstance(g, int)} - {0, 1}


def marker_group(job):
    """The check's process group, written by run.sh to the job's marker file (0 when unknown)."""
    try:
        with open(job['marker']) as f:
            text = f.read(32).strip()
        return int(text) if text.isdigit() else 0
    except (OSError, KeyError, TypeError):
        return 0


def birth(pid):
    """Linux start of a pid in clock ticks since boot (/proc stat field 22), as process_tree.py pins it.
    Unlike ps lstart it does not move when the wall clock is corrected (WSL after boot). None elsewhere."""
    try:
        with open('/proc/%d/stat' % int(pid)) as f:
            return f.read().rpartition(') ')[2].split()[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


def job_live(job, run, pid, procs):
    """resources.py's identity rules (supervised, cleanup_live), never "the pid exists": the owner's or its
    watcher's recorded start, run.sh of this very run as the owner pid (the token ends in run.sh's pid; ps lstart
    shifts when WSL corrects its clock after boot), a pinned birth (owner or the watcher's tree record), else a
    live member of the check's process group."""
    for who, start in ((job.get('owner'), job.get('start')), (job.get('watcher'), job.get('watcher_start'))):
        if start and who in procs and procs[who][1] == start_key(start):
            return True
    if run and pid and RUNSH.get(run, {}).get('pid') == pid == job.get('owner'):
        return True
    if job.get('owner_birth') and job.get('owner') in procs and birth(job['owner']) == job['owner_birth']:
        return True
    try:
        with open(job['tree_marker']) as f:
            known = json.load(f)['known']
        if any(int(p) in procs and birth(p) == saved for p, saved in known.items()):
            return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    groups = job_groups(job)
    return any(p[0] in groups for p in procs.values())


def queue(procs):
    try:
        with open(QUEUE) as f:
            data = json.load(f)
        jobs = data.get('jobs') or {}
    except FileNotFoundError:
        return dict(readable=True, rows=[])
    except (OSError, ValueError, AttributeError):
        return dict(readable=False, rows=[])
    rows = []
    for token, job in jobs.items():
        if not isinstance(job, dict) or job.get('state') in DONE_STATES:
            continue   # v2 keeps finished rows in the ledger until they expire
        # Runner tokens are "<project>-<run>-<pid>"; local safe-check tokens are bare hex.
        found = re.match(r'^(.+?)-(\d{8}-\d{6}-[0-9a-f]+-\d+-[^-]+)-(\d+)$', str(token))
        # The token ends in run.sh's pid; its descendant tree holds every command of the check.
        pid = int(found[3]) if found else None
        live = None if procs is None else job_live(job, found[2] if found else None, pid, procs)
        key = start_key(job.get('start', ''))
        since = time.mktime(time.strptime(key, '%Y%m%d%H%M%S')) if key.isdigit() else None
        rss, cpu = None, None
        if live:
            rss, cpu = tree_usage(pid, procs) if pid and procs and pid in procs else (None, None)
            if rss is None:
                rss, cpu = group_usage(job, procs)
        maintenance = (job.get('state') if job.get('version') == 2 and
                       job.get('state') in ('parked', 'starting') and
                       not any(job.get(k) for k in ('workers', 'memory_mb', 'groups', 'process', 'granted')) else None)
        phase = job.get('process')
        trusted_phase = (job.get('version') == 2 and job.get('state') == 'running' and
                         isinstance(phase, dict) and
                         phase.get('generation') == job.get('lease_generation') and
                         phase.get('phase') in V2_PHASES)
        phase_deadline = phase.get('deadline') if trusted_phase else None
        job_deadline = job.get('job_deadline') if job.get('version') == 2 and job.get('state') == 'running' else None
        run_deadline = job.get('deadline') if job.get('version') == 2 and job.get('state') == 'running' else None
        admitted_at = job.get('admitted_at') if job.get('version') == 2 and job.get('state') == 'running' else None
        phase_deadline = phase_deadline if type(phase_deadline) in (int, float) and phase_deadline > 0 and math.isfinite(phase_deadline) else None
        job_deadline = job_deadline if type(job_deadline) in (int, float) and job_deadline > 0 and math.isfinite(job_deadline) else None
        run_deadline = run_deadline if type(run_deadline) in (int, float) and run_deadline > 0 and math.isfinite(run_deadline) else None
        admitted_at = admitted_at if type(admitted_at) in (int, float) and admitted_at > 0 and math.isfinite(admitted_at) else None
        rows.append(dict(project=found[1] if found else None, run=found[2] if found else None, since=since,
                         kind='runner' if found else 'local', sequence=job.get('sequence', 0),
                         workers=job.get('workers') or 0, memory_mb=job.get('memory_mb') or 0, live=live,
                         rss_mb=rss, cpu_pct=cpu, phase_deadline=phase_deadline,
                         job_deadline=job_deadline, run_deadline=run_deadline,
                         ledger_phase=phase.get('phase') if trusted_phase else None,
                         admitted_at=admitted_at, maintenance_state=maintenance))
        if os.environ.get('CI_BOARD_HOME') and job.get('fixture_wait'):
            rows[-1]['fixture_wait'] = job['fixture_wait']   # fixture owners are pid 1: no process tree
    rows.sort(key=lambda r: r['sequence'])
    return dict(readable=True, rows=rows)


def read_part(path):
    """A log's lines: whole when small, else the first HEAD and last TAIL bytes."""
    try:
        with open(path, 'rb') as f:
            size = os.fstat(f.fileno()).st_size
            if size <= WHOLE:
                data = f.read()
            else:
                head = f.read(HEAD)
                f.seek(size - TAIL)
                tail = f.read(TAIL)
                data = head[:head.rfind(b'\n') + 1] + tail[tail.find(b'\n') + 1:]
        READ[0] += len(data)
        return data.decode('utf-8', 'replace').splitlines()
    except OSError:
        return None


def ts(stamp):
    try:
        return time.mktime(time.strptime(stamp, '%Y-%m-%d %H:%M:%S'))
    except ValueError:
        return None


def conf_flags(path):
    flags = dict(db=False, prepare=False, check=None, exclusive=False)
    try:
        with open(path, errors='replace') as f:
            text = f.read(65536)
    except OSError:
        return flags
    # Only presence of a key (or a 0/1 switch) is used; values are never sent out.
    flags['db'] = bool(re.search(r'^\s*(export\s+)?REMOTE_CI_POSTGRES=["\']?\d', text, re.M))
    flags['prepare'] = bool(re.search(r'^\s*(export\s+)?REMOTE_CI_PREPARE=\S', text, re.M))
    # Host-exclusive runs: opt-in REMOTE_CI_EXCLUSIVE=1 on the current runner; an older runner
    # (whose lib.sh predates the switch) ran every REMOTE_CI_SLOTS=1 project exclusively.
    found = re.search(r'^\s*(?:export\s+)?REMOTE_CI_SLOTS=["\']?(\d+)', text, re.M)
    one_slot = not found or found[1] == '1'
    if old_runner():
        flags['exclusive'] = one_slot
    else:
        flags['exclusive'] = bool(re.search(r'^\s*(export\s+)?REMOTE_CI_EXCLUSIVE=["\']?1\b', text, re.M))
    # Hash of the configured full check's first log line, to tell full suites from --run commands.
    found = re.search(r'^\s*(?:export\s+)?REMOTE_CI_CHECK=(.*)$', text, re.M)
    raw = found[1] if found else 'pnpm run check'
    if raw[:1] in ('"', "'"):
        raw = raw[1:-1] if len(raw) > 1 and raw.endswith(raw[0]) else raw[1:]
    flags['check'] = hashlib.sha256(('== ' + raw).encode()).hexdigest()[:8]
    return flags


_OLD_RUNNER = {}


def old_runner():
    """True when the installed runner predates REMOTE_CI_EXCLUSIVE (SLOTS=1 meant host-exclusive)."""
    if CI not in _OLD_RUNNER:
        try:
            with open(os.path.join(CI, 'bin', 'lib.sh'), errors='replace') as f:
                _OLD_RUNNER[CI] = 'REMOTE_CI_EXCLUSIVE' not in f.read(65536)
        except OSError:
            _OLD_RUNNER[CI] = False
    return _OLD_RUNNER[CI]


def kind_of(info, flags, run):
    """'check' (the configured full suite), 'run' (a one-off --run command) or None (unknown yet)."""
    if info['cached'] or info['following']:
        return 'check'
    if info['check'] and flags.get('check'):
        return 'check' if info['check'] == flags['check'] else 'run'
    found = RUNSH.get(run)
    return found['kind'] if found else None


def parse_log(lines, flags):
    """Phase and timings from a run log's `== ...` lines."""
    info = dict(sha=None, slot=None, started=None, timings={}, queued=False, admitted=False,
                cached=False, following=False, busy=False, rc=None, workers=None, check=None,
                exclusive=flags.get('exclusive', False))
    setup_done = False
    for line in lines:
        if not line.startswith(('== ', 'busy:')):
            continue
        if line.startswith('busy:'):
            info['busy'] = True
        elif line.startswith('== resources queued'):
            info['queued'] = True
        elif line.startswith('== resources workers='):
            info['admitted'] = True
            found = re.match(r'== resources workers=(\d+)', line)
            info['workers'] = int(found[1]) if found else None
        elif line.startswith('== cached'):
            info['cached'] = True
        elif line.startswith('== following run'):
            info['following'] = True
        elif TIMING.match(line):
            found = TIMING.match(line)
            info['timings'][found[1]] = int(found[2])
            if found[1] in ('install', 'database', 'prepare'):
                setup_done, info['check'] = True, None
        elif setup_done and info['check'] is None and not line.startswith(MARKERS) and info['started']:
            # "== <check command>": keep only a short hash so runs of one command share a median.
            info['check'] = hashlib.sha256(line.encode()).hexdigest()[:8]
        elif HEADER.match(line):
            found = HEADER.match(line)
            info.update(sha=found[1], slot=int(found[2]), started=ts(found[4]))
            if found[3] == 'exclusive':
                info['exclusive'] = True
        elif FINISHED.match(line):
            info['rc'] = int(FINISHED.match(line)[1])
    t = info['timings']
    if info['started'] is None:
        phase = 'following' if info['following'] else 'queued'
        # Newer run.sh takes the slot/exclusive lock before host admission; older ones after.
        # Only an exclusive run waits for the host lock; any other run waits for its project slot.
        sub = ('tree' if info['following'] else 'slot' if info['admitted'] else
               'resources' if info['queued'] else 'lock' if info['exclusive'] else 'slot')
        since = None
    elif 'run' in t:
        phase, sub, since = 'cleanup', 'cleanup', info['started'] + t['run']
    else:
        done = sum(t.get(k, 0) for k in TOP)
        since = info['started'] + done
        last = max([TOP.index(k) for k in TOP if k in t], default=-1)
        if info['check'] or last >= TOP.index('prepare'):
            phase, sub = 'check', 'check'
        elif last < 0:
            phase, sub = 'install', 'checkout'
        elif last == 0:
            phase, sub = 'install', 'install'
        elif flags['db'] and last < TOP.index('database'):
            phase, sub = 'database', 'database'
        elif flags['prepare']:
            phase, sub = 'database', 'prepare'
        else:
            phase, sub = 'check', 'check'
    info.update(phase=phase, step=sub, phase_since=since)
    return info


def outcome(rc):
    return 'pass' if rc == 0 else 'fail' if rc == 1 else 'infra'


def projects(qrows, queue_ok, procs_ok):
    active, finished, slots = [], [], []
    root = os.path.join(CI, 'projects')
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    live_runs = {(r['project'], r['run']): r for r in qrows
                 if r['project'] and r['run'] and (r['live'] is True or r.get('maintenance_state'))}
    done = []   # finished runs found by stat only; the newest HISTORY are read below
    for name in names:
        pdir = os.path.join(root, name)
        logs, results = os.path.join(pdir, 'logs'), os.path.join(pdir, 'results')
        try:
            runs = sorted((f[:-4] for f in os.listdir(logs) if f.endswith('.log')), reverse=True)
        except OSError:
            runs = []
        try:
            exits = {f[:-5] for f in os.listdir(results) if f.endswith('.exit')}
        except OSError:
            exits = set()
        for slot_file in sorted(os.listdir(os.path.join(pdir, 'slots'))) if os.path.isdir(os.path.join(pdir, 'slots')) else []:
            if slot_file.endswith('.run'):
                try:
                    with open(os.path.join(pdir, 'slots', slot_file)) as f:
                        slots.append(dict(project=name, slot=slot_file[:-4], run=f.read().strip()))
                except OSError:
                    pass
        selected = list(runs[:HISTORY])
        selected.extend(run for run in runs[HISTORY:] if (name, run) in live_runs)
        for run in selected:
            found = RUN_ID.match(run)
            submitted = ts(found[1][:4] + '-' + found[1][4:6] + '-' + found[1][6:8] + ' ' +
                           found[1][9:11] + ':' + found[1][11:13] + ':' + found[1][13:15]) if found else None
            log = os.path.join(logs, run + '.log')
            try:
                info = os.stat(log)
            except OSError:
                continue
            mtime = info.st_mtime
            row = dict(project=name, run=run, sha=found[2] if found else None, branch=None, submitted=submitted)
            if run in exits:
                try:
                    end = os.stat(os.path.join(results, run + '.exit')).st_mtime
                except OSError:
                    end = mtime
                key = '%d.%d.%d.%d' % (info.st_ino, info.st_size, int(mtime), int(end))
                done.append((end, key, log, results, pdir, row))
                continue
            live = (name, run) in live_runs
            if not live and NOW - mtime > WAIT:
                continue  # never finished and silent past the wait deadline: lost
            budget_exhausted = READ[0] >= BUDGET
            lines = [] if budget_exhausted else read_part(log)
            if lines is None:
                continue
            flags = conf_flags(os.path.join(pdir, 'runs', run, 'conf'))
            info = parse_log(lines, flags)
            if not live and info['queued'] and queue_ok and procs_ok and not info['following']:
                continue  # registered with the queue but its owner is gone: lost run
            trusted = live_runs.get((name, run), {})
            maintenance = trusted.get('maintenance_state')
            if maintenance:
                info.update(phase='queued', step='maintenance', started=None, phase_since=None)
            ledger_phase = trusted.get('ledger_phase')
            admitted_at = trusted.get('admitted_at')
            if live and budget_exhausted and ledger_phase and admitted_at:
                phase = ('install' if ledger_phase in ('checkout', 'install') else
                         'database' if ledger_phase in ('template', 'database', 'prepare') else
                         'cleanup' if ledger_phase in ('cleanup', 'maintenance') else 'check')
                info.update(phase=phase, step=ledger_phase, started=admitted_at,
                            phase_since=admitted_at)
            row.update(sha=info['sha'] or row['sha'], slot=info['slot'], phase=info['phase'],
                       step=info['step'], started=info['started'], phase_since=info['phase_since'],
                       workers=trusted.get('workers') if live else info['workers'],
                       elapsed_s=round(NOW - info['started']) if info['started'] else None,
                       waited_s=round((info['started'] or NOW) - (submitted or mtime)),
                       sequence=trusted.get('sequence') if live else None,
                       live=trusted.get('live', live), maintenance_state=maintenance, check=info['check'],
                       phase_deadline=trusted.get('phase_deadline'), job_deadline=trusted.get('job_deadline'),
                       run_deadline=trusted.get('run_deadline'),
                       log_idle_s=max(0, round(NOW - mtime)),
                       kind=kind_of(info, flags, run), exclusive=info['exclusive'],
                       timings={k: info['timings'][k] for k in TOP if k in info['timings']})
            active.append(row)
    done.sort(key=lambda d: d[0], reverse=True)
    for end, key, log, results, pdir, row in done[:HISTORY]:
        run = row['run']
        if KNOWN.get(run) == key:
            finished.append(dict(project=row['project'], run=run, ended=end, same=True))
            continue
        if READ[0] >= BUDGET:
            finished.append(dict(project=row['project'], run=run, ended=end, deferred=True))
            continue
        try:
            with open(os.path.join(results, run + '.exit')) as f:
                rc = int(f.read().strip() or -1)
        except (OSError, ValueError):
            rc = -1
        lines = read_part(log) or []
        info = parse_log(lines, dict(db=False, prepare=False))
        info_kind = kind_of(info, conf_flags(os.path.join(pdir, 'runs', run, 'conf')), run)
        t = info['timings']
        row.update(rc=rc, outcome=outcome(rc), ended=end, cached=info['cached'] or info['following'],
                   sha=info['sha'] or row['sha'], run_s=t.get('run'), total_s=t.get('total'),
                   queue_s=t.get('queue'), check=info['check'], kind=info_kind,
                   phases={k: t[k] for k in TOP if k in t}, cache_key=key)
        finished.append(row)
    return active, finished, slots


def drain_holder():
    """The exclusive run draining the runner (~/ci/drain + drain.at), or None."""
    holder = lock_holder('drain')
    if holder:
        try:
            with open(os.path.join(CI, 'drain.at')) as f:
                at = f.read(32).strip()
            holder['at'] = int(at) if at.isdigit() else None
        except OSError:
            holder['at'] = None
    return holder


def lock_holder(name='lock'):
    try:
        with open(os.path.join(CI, name)) as f:
            pid = f.read().strip()
    except OSError:
        return None
    if not pid.isdigit():
        return None
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        pass
    except OSError:
        return None
    pid = int(pid)
    holder = next((dict(pid=pid, run=run, project=v['project'], kind=v['kind'])
                   for run, v in RUNSH.items() if v['pid'] == pid), None)
    if not holder:
        return dict(pid=pid, run=None, project=None, kind=None, exclusive=None)
    # Slot runs take ~/ci/lock briefly as an admission guard; only an exclusive run holds it for a run.
    holder['exclusive'] = conf_flags(os.path.join(CI, 'projects', holder['project'], 'runs', holder['run'],
                                                  'conf'))['exclusive']
    return holder


DOCKER_PATHS = ('/usr/local/bin/docker', '/opt/homebrew/bin/docker', '/usr/bin/docker',
                '/Applications/Docker.app/Contents/Resources/bin/docker', '~/.orbstack/bin/docker',
                '~/.docker/bin/docker')
DOCKER_TIMEOUT = 1.8


def docker_start():
    """Start `docker ps` and `docker stats --no-stream` in parallel with the rest of the snapshot."""
    if os.environ.get('CI_BOARD_HOME'):
        return 'fixture'
    binary = shutil.which('docker') or next(
        (p for p in (os.path.expanduser(x) for x in DOCKER_PATHS) if os.access(p, os.X_OK)), None)
    if not binary:
        return None
    procs = []
    for args in (['ps', '--no-trunc', '--format', '{{json .}}'],
                 ['stats', '--no-stream', '--no-trunc', '--format', '{{json .}}']):
        try:
            procs.append(subprocess.Popen([binary, *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          stdin=subprocess.DEVNULL, text=True))
        except OSError:
            return None
    return procs


def docker_rows(text):
    rows = []
    for line in (text or '').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def size_mb(text):
    found = re.match(r'\s*([\d.]+)\s*([KMGT]?i?B)', text or '', re.I)
    if not found:
        return None
    scale = {'B': 1 / 1048576, 'KB': 1 / 1024, 'KIB': 1 / 1024, 'MB': 1, 'MIB': 1, 'GB': 1024, 'GIB': 1024,
             'TB': 1048576, 'TIB': 1048576}.get(found[2].upper(), 1)
    return round(float(found[1]) * scale, 1)


def containers(started, active, deadline):
    """{state: ok|absent|stopped|timeout, items: [...]}; never raises."""
    if started == 'fixture':
        try:
            with open(os.path.join(HOME, '.ci-board-docker.json')) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return dict(state='absent', items=[])
        if raw.get('state', 'ok') != 'ok':
            return dict(state=raw['state'], items=[])
        ps_rows, stats_rows = raw.get('ps', []), raw.get('stats', [])
    elif started is None:
        return dict(state='absent', items=[])
    else:
        outputs, codes = [], []
        for proc in started:
            try:
                text, _ = proc.communicate(timeout=max(0.1, deadline - time.monotonic()))
                outputs.append(text)
                codes.append(proc.returncode)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                outputs.append('')
                codes.append(None)
        if codes[0] is None:
            return dict(state='timeout', items=[])
        if codes[0] != 0:
            return dict(state='stopped', items=[])
        ps_rows, stats_rows = docker_rows(outputs[0]), docker_rows(outputs[1])
    stats = {}
    for row in stats_rows:
        for key in (row.get('ID', '')[:12], row.get('Name', '')):
            if key:
                stats[key] = row
    owners = [(r['project'], r['run']) for r in active if r.get('project')]
    items = []
    for row in ps_rows:
        name = str(row.get('Names', '')).split(',')[0]
        labels = str(row.get('Labels', ''))
        stat = stats.get(name) or stats.get(str(row.get('ID', ''))[:12]) or {}
        cpu = re.match(r'\s*([\d.]+)', str(stat.get('CPUPerc', '')))
        owner = None
        hay = (name + ' ' + labels).lower()
        for project, run in owners:
            norm = re.sub(r'[^a-z0-9]', '', project.lower())
            if (run and run.lower() in hay) or (norm and norm in re.sub(r'[^a-z0-9]', '', hay)):
                owner = project
                break
        test = owner is not None or 'org.testcontainers' in labels or re.search(r'(^|[-_])(test|ci)([-_]|$)', name.lower())
        items.append(dict(name=name, image=str(row.get('Image', ''))[:80], status=str(row.get('Status', ''))[:60],
                          ports=str(row.get('Ports', ''))[:120], cpu_pct=float(cpu[1]) if cpu else None,
                          mem_mb=size_mb(str(stat.get('MemUsage', '')).split('/')[0]),
                          owner=owner, test=bool(test)))
    items.sort(key=lambda c: (c['owner'] is None, not c['test'], c['name']))
    return dict(state='ok', items=items[:40])


def main():
    start = time.monotonic()
    docker = docker_start()
    procs = processes()
    q = queue(procs)
    live_rows = [r for r in q['rows'] if r['live'] is not False or r.get('maintenance_state')]
    active, finished, slots = projects(q['rows'], q['readable'], procs is not None)
    snap = dict(
        version=1, now=NOW, host=socket.gethostname().split('.')[0], platform=sys.platform,
        ci_present=os.path.isdir(CI), metrics=metrics(), policy=policy(),
        queue=dict(readable=q['readable'], liveness=procs is not None,
                   granted=sum(r['workers'] for r in live_rows),
                   active=[r for r in live_rows if r['workers']],
                   waiting=[r for r in live_rows if not r['workers']]),
        lock=lock_holder(), drain=drain_holder(), slots=slots, active=active, finished=finished)
    usage = {r['run']: r for r in live_rows if r['run']}
    for row in active:
        found = usage.get(row['run'])
        row['rss_mb'], row['cpu_pct'] = (found['rss_mb'], found['cpu_pct']) if found else (None, None)
        row['reserved_mb'] = found['memory_mb'] or None if found else None
        runsh = RUNSH.get(row['run'])
        row['wait'] = (found or {}).pop('fixture_wait', None) or (
            wait_reason(runsh['pid'], procs, row['phase']) if runsh else None)
    snap['services'] = services()
    snap['containers'] = containers(docker, active, start + DOCKER_TIMEOUT)
    if os.environ.get('CI_BOARD_HOME'):
        # Fixture HOMEs name a generic host and may pin the admission policy.
        for key in ('host', 'policy', 'services'):
            value = snap['metrics'].pop(key, None)
            if value:
                snap[key] = value
    snap['took_ms'] = round((time.monotonic() - start) * 1000)
    snap['read_bytes'] = READ[0]
    json.dump(snap, sys.stdout, separators=(',', ':'))
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
