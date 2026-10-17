"""Workers of every project for the board, read from the state files `batches` reads. Read-only.

Roots: the legacy ~/.claude/state and, when it exists, the canonical state home
($AGENT_LANES_STATE, else $XDG_STATE_HOME/agent-lanes, else ~/.local/state/agent-lanes). Each has
logs/<project>/<name>.log{,.run,.pid,.exit,.last,.delivered}, mq/<project>/ and the tsv registries.

State rules are batches.sh's, ported line by line (test_workers runs batches.sh on the same fixtures):
paused (ops_is_paused) first; with .exit: done when ops_reported (rc=0 line and a non-empty .last),
else ERRORED (rc < 128 and an ERROR: line in the last 5) or DIED; without .exit: running while
ops_running (worker or supervisor pid alive, same command kind, same start time), STALLED when the
log is quiet for BATCHES_STALL_MIN (12) minutes and no child started within that time; else DIED.

Bounded: the newest MAX_FILES .run files of the last 24 h are chosen from directory entries
(os.scandir stat only) before any file is read; logs are read as a short tail.
Privacy: the API carries no free text. Errors and mq reasons are fixed categories; the report, the
brief, prompts, env and session values are never read into a row.
"""
import json
import os
import re
import subprocess
import time

from mqstate import clean, head, mq_events, mq_lane, tail

WINDOW_S = 86400        # like batches: workers launched in the last 24 h
MAX_FILES = 150
WORKER_CMD = re.compile(r'codex|harness_run')
# batches' cause() pattern: the lines a failure reason is taken from.
CAUSE = re.compile(r'ERROR|[Ee]rror:|panic|Killed|capacity|usage limit|rate limit|exit code|stream disconnected')
CATEGORIES = (
    ('provider-capacity', re.compile(r'high demand|capacity|rate limit|usage limit|overloaded|\b429\b|too many requests|'
                                     r'Reconnecting', re.I)),
    ('stream-disconnected', re.compile(r'stream disconnected|disconnected|connection reset|stream closed|broken pipe', re.I)),
    ('auth', re.compile(r'unauthori[sz]ed|\b401\b|\b403\b|forbidden|authenticat|log ?in|credential|expired', re.I)),
    ('timeout', re.compile(r'timed? ?out|deadline exceeded', re.I)),
)
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
KEY = re.compile(r'-[0-9a-f]{10}$')
REPORTED = re.compile(r'^rc=0 ended=[0-9]+$', re.M)
RUN_KEYS = ('run_id', 'started', 'preset', 'harness', 'route', 'model', 'effort', 'dir', 'worker_pid', 'worker_birth',
            'supervisor_pid', 'supervisor_birth')


def stall_s():
    raw = os.environ.get('BATCHES_STALL_MIN', '')
    return int(raw) * 60 if raw.isdigit() else 12 * 60


def legacy_root():
    return os.path.join(os.path.expanduser('~'), '.claude', 'state')


def canonical_root():
    if os.environ.get('AGENT_LANES_STATE'):
        return os.path.abspath(os.path.expanduser(os.environ['AGENT_LANES_STATE']))
    base = os.environ.get('XDG_STATE_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'state')
    return os.path.join(os.path.abspath(os.path.expanduser(base)), 'agent-lanes')


def default_roots():
    return [r for r in (legacy_root(), canonical_root()) if os.path.isdir(r)]


def stat(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def fields(text):
    """Only the known keys of a .run record (first value wins); other keys are never kept."""
    found = {}
    for line in (text or '').splitlines():
        key, _, value = line.partition('=')
        if key in RUN_KEYS and key not in found:
            found[key] = value.strip()
    return found


def tsv(roots, name):
    rows = []
    for root in roots:
        rows += [line.split('\t') for line in (head(os.path.join(root, name), 1 << 20) or '').splitlines() if line]
    return rows


def category(lines):
    """Fixed category of the last error-looking line, or None when no line looks like an error."""
    hits = [ANSI.sub('', line) for line in lines if CAUSE.search(line) or 'high demand' in line]
    if not hits:
        return None
    return next((name for name, pattern in CATEGORIES if pattern.search(hits[-1])), 'other')


def branch(directory):
    """Branch from the worktree's HEAD file (no git call), or None."""
    if not directory:
        return None
    dot = os.path.join(directory, '.git')
    gitdir = dot
    if os.path.isfile(dot):
        text = (head(dot, 1024) or '').strip()
        if not text.startswith('gitdir: '):
            return None
        gitdir = os.path.join(directory, text[8:].strip())
    ref = (head(os.path.join(gitdir, 'HEAD'), 1024) or '').strip()
    if ref.startswith('ref: refs/heads/'):
        return clean(ref[16:], 80)
    return ('detached ' + clean(ref[:7])) if re.match(r'^[0-9a-f]{7}', ref) else None


def etime_s(text):
    """ps etime ([[dd-]hh:]mm:ss) in seconds."""
    days, _, rest = text.rpartition('-')
    parts = [int(p) for p in rest.split(':') if p.isdigit()]
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs + (int(days) * 86400 if days.isdigit() else 0)


def ps_rows():
    """pid -> (ppid, age seconds, start time, command) from one ps call."""
    try:
        text = subprocess.run(['env', 'LC_ALL=C', 'ps', '-axo', 'pid=,ppid=,etime=,lstart=,command='],
                              capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in text.splitlines():
        parts = line.split(None, 8)
        if len(parts) >= 8 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), etime_s(parts[2]), ' '.join(parts[3:8]),
                                    parts[8] if len(parts) > 8 else '')
    return table


def pid_alive(pid):
    # state.sh ops_pid_live is `kill -0`: another user's process (EPERM) counts as not live.
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return False
    return True


def youngest_child(root, table):
    """Seconds since the youngest descendant of root started, or None (batches' youngest_child)."""
    best = None
    for pid, row in table.items():
        at, seen = pid, set()
        while at in table and at != root and at > 1 and at not in seen:
            seen.add(at)
            at = table[at][0]
        if at == root and pid != root and (best is None or row[1] < best):
            best = row[1]
    return best


class Workers:
    def __init__(self, roots=None, table=ps_rows, alive=pid_alive, now=time.time, max_files=MAX_FILES):
        self.roots = default_roots() if roots is None else roots
        self.table, self.alive, self.now, self.max_files = table, alive, now, max_files

    def candidates(self, now):
        """[(root, project folder, log path)] of the newest .run files, chosen before any file is read."""
        found = []
        for root in self.roots:
            try:
                projects = list(os.scandir(os.path.join(root, 'logs')))
            except OSError:
                continue
            for entry in projects:
                try:
                    # A launch or pause renames a .run into its folder, so an older folder has none.
                    if not entry.is_dir() or now - entry.stat().st_mtime > WINDOW_S:
                        continue
                    files = list(os.scandir(entry.path))
                except OSError:
                    continue
                for f in files:
                    stem = f.name[:-len('.log.run')]
                    if not f.name.endswith('.log.run') or not stem or '.' in stem:
                        continue   # archived attempts (<name>.<date>.<pid>) are history
                    try:
                        at = f.stat().st_mtime
                    except OSError:
                        continue
                    if now - at <= WINDOW_S:
                        found.append((at, root, entry.name, f.path[:-4]))
        found.sort(key=lambda c: -c[0])
        out, seen = [], set()
        for at, root, folder, log in found[:self.max_files]:
            real = os.path.realpath(log)
            if real not in seen:
                seen.add(real)
                out.append((root, folder, log))
        return out

    @staticmethod
    def project_name(root, folder):
        """Canonical folders are <name>-<hash10>; repos/<folder>.json names them."""
        try:
            return clean(str(json.loads(head(os.path.join(root, 'repos', folder + '.json'), 4096))['name']))
        except (ValueError, KeyError, TypeError):
            return clean(KEY.sub('', folder) or folder)

    def acks(self):
        """name -> ack time. The acked file holds names only, so its mtime bounds each ack from above."""
        acked = {}
        for root in self.roots:
            path = os.path.join(root, 'acked')
            info = stat(path)
            for line in (head(path, 1 << 20) or '').splitlines():
                if line and info:
                    acked[line] = max(acked.get(line, 0), info.st_mtime)
        return acked

    def scan(self):
        now = self.now()
        table = self.table() if self.table else {}
        paused = tsv(self.roots, 'paused-workers.tsv')
        acked = self.acks()
        tags = {}
        for r in tsv(self.roots, 'worker-tags.tsv'):
            if len(r) >= 5:
                tags[(r[1], r[2])] = clean(r[3] + (':' + r[4] if r[4] else ''))
        rows, events, names = [], {}, {}
        for root, folder, log in self.candidates(now):
            if (root, folder) not in names:
                names[(root, folder)] = self.project_name(root, folder)
            row = self.worker(root, names[(root, folder)], folder, log, now, table, paused, acked, tags, events)
            if row:
                rows.append(row)
        # mq state belongs to the newest worker of a lane (rows are newest first).
        owners = set()
        for row in rows:
            lane = row.pop('_mq')
            if not lane or (row['project'], row['lane']) in owners:
                continue
            owners.add((row['project'], row['lane']))
            row['mq'] = lane
            # An acknowledged worker comes back only for a bounce or park after its ack.
            if lane['reason'] and (row['ack_at'] is None or (lane['problem_at'] or 0) > row['ack_at']):
                row['problems'].append(dict(level='warn', kind=lane['state'], text=lane['state'] + ': ' + lane['reason']))
        return rows

    def worker_live(self, pid, log_rec, key, pattern, table):
        """state.sh ops_worker_live / ops_supervisor_live, observation mode."""
        if not pid.isdigit() or not self.alive(int(pid)):
            return False
        row = table.get(int(pid))
        if row and row[3] and not re.search(pattern, row[3]):
            return False
        registered = log_rec.get(key + '_pid', '')
        if key == 'worker' and registered and registered != pid:
            return False
        birth = ' '.join(log_rec.get(key + '_birth', '').split())
        current = row[2] if row else ''
        return not birth or not current or current == birth

    def worker(self, root, project, folder, log, now, table, paused, acked, tags, events):
        rec = fields(head(log + '.run'))
        if not rec.get('run_id'):
            return None
        started = int(rec['started']) if rec.get('started', '').isdigit() else None
        if started and now - started > WINDOW_S:
            return None
        name = os.path.basename(log)[:-4]
        directory = rec.get('dir') or ''
        lane = clean(os.path.basename(directory.rstrip('/')))
        log_info = stat(log)
        log_at = log_info.st_mtime if log_info else started
        exit_text = head(log + '.exit', 256)
        found = re.search(r'^rc=(-?\d+) ended=(\d+)', exit_text or '', re.M)
        rc, ended = (int(found[1]), int(found[2])) if found else (None, None)
        last_info = stat(log + '.last')
        reported = exit_text is not None and bool(last_info and last_info.st_size) and bool(REPORTED.search(exit_text))
        provider = clean((rec.get('route') or '').split('-')[0] or rec.get('harness') or 'worker', 24)
        problems, why = [], None
        ack_at = acked.get(name)
        if any(self.paused(r, name, log, rec, directory) for r in paused):
            state = 'paused'
        elif exit_text is not None:
            if reported:
                state = 'finished' if head(log + '.delivered', 256) == rec['run_id'] + '\n' else 'done'
            else:
                state = 'errored'
                lines = tail(log)
                errors = [line for line in lines[-5:] if line.startswith('ERROR:')]
                if (rc if rc is not None else 0) < 128 and errors:
                    why = category(errors) or 'other'
                elif rc:
                    why = 'nonzero-exit (rc %d)' % rc
                else:
                    why = category(lines) or 'other'
        else:
            # ops_running: the .pid file when present, else the registered pid.
            pid_text = head(log + '.pid', 64)
            pid = pid_text.strip() if pid_text is not None else rec.get('worker_pid', '')
            if self.worker_live(pid, rec, 'worker', WORKER_CMD, table) or \
                    self.worker_live(rec.get('supervisor_pid', ''), rec, 'supervisor', 'supervise-worker', table):
                state = 'running'
                quiet, limit = now - (log_at or now), stall_s()
                busy = youngest_child(int(rec['worker_pid']), table) \
                    if quiet > limit and rec.get('worker_pid', '').isdigit() else None
                if quiet > limit and not (busy is not None and busy <= limit):
                    state = 'stalled'
                    problems.append(dict(level='warn', kind='stalled', text='stalled: no log output for %dm' % (quiet // 60)))
            else:
                state, why = 'errored', 'died'
        if why:
            problems.append(dict(level='bad', kind='errored', text=provider + ': ' + why))
        if ack_at is not None and state in ('errored', 'stalled'):
            problems = []   # batches hides acknowledged DIED/ERRORED/STALLED workers
        mq = None
        if lane and lane != project:
            mqdir = self.mq_dir(root, project, folder)
            if mqdir not in events:
                events[mqdir] = mq_events(mqdir)
            mq = mq_lane(mqdir, lane, events[mqdir], now)
        return dict(key=project + '/' + name, name=clean(name), project=project, dir=directory, lane=lane,
                    branch=branch(directory), model=clean(rec.get('model')), effort=clean(rec.get('effort'), 16),
                    preset=clean(rec.get('preset'), 24), harness=clean(rec.get('harness'), 24), provider=provider,
                    state=state, started=started, ended=ended, rc=rc, log_at=log_at, log=log,
                    tag=tags.get((project, name)), problems=problems, ack_at=ack_at,
                    resume='cd %s && wk %s -r' % (directory or '<worktree>', clean(name)) if state == 'errored' else None,
                    mq=None, ci=None, _mq=mq)

    @staticmethod
    def paused(row, name, log, rec, directory):
        # ops_is_paused: rows name the log and run (4 columns); older rows the run (3) or the directory.
        if row[0] != name:
            return False
        if len(row) >= 4:
            return row[3] == log and row[2] == rec.get('run_id', '')
        if len(row) == 3:
            return row[2] == rec.get('run_id', '')
        return not directory or (len(row) > 1 and row[1] == directory)

    def mq_dir(self, root, project, folder):
        own = os.path.join(root, 'mq', folder)
        if os.path.isdir(own):
            return own
        for other in self.roots:
            path = os.path.join(other, 'mq', project)
            if os.path.isdir(path):
                return path
        return own


def with_ci(rows, current, last):
    """Copies of rows with their CI run. current/last: {(project, label): run}.

    A label is a worker name (its client process descends from the worker), "mq landing <lane>"
    (the lane's newest worker) or the client's folder (the lane's newest live worker). Rows are
    newest first, so the first match is the newest worker."""
    out = [dict(r, problems=list(r['problems'])) for r in rows]
    by_name, by_lane, live_lane = {}, {}, {}
    for r in out:
        by_name.setdefault((r['project'], r['name']), r)
        by_lane.setdefault((r['project'], r['lane']), r)
        if r['state'] in ('running', 'stalled'):
            live_lane.setdefault((r['project'], r['lane']), r)

    def owner(key):
        project, label = key
        if label.startswith('mq landing '):
            return by_lane.get((project, label[11:])), True
        return by_name.get((project, label)) or live_lane.get((project, label)), False
    for key, run in current.items():
        row, via_mq = owner(key)
        if row and not row['ci']:
            row['ci'] = dict(run, via_mq=via_mq)
    for key, run in last.items():
        row, via_mq = owner(key)
        if not row or row['ci'] or (row['started'] and (run.get('ended') or 0) < row['started']):
            continue
        row['ci'] = dict(run, via_mq=via_mq)
        fresh = row['ack_at'] is None or (run.get('ended') or 0) > row['ack_at']
        if run.get('outcome') == 'fail' and not via_mq and row['state'] != 'finished' and fresh:
            row['problems'].append(dict(level='bad', kind='ci', note='since board start',
                                        text='last CI run failed' + (' on ' + run['sha'][:7] if run.get('sha') else '')))
    return out
