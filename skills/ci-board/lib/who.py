"""Which worker (lane) submitted a source run, found on this machine (the one running ci-board).

A run id is <date>-<time>-<sha8>-<client pid>-<nonce>; the client is a `remote-ci` process here while
the run waits or runs. Its ancestors lead to a worker (agent metadata files name worker_pid /
supervisor_pid), to an `mq run`/`mq land` process (label "mq landing <lane>"), or to nothing known
(label: the client's working-tree folder). A label is looked up once per run and kept for its life.
"""
import os
import re
import subprocess
import time

RUN_PID = re.compile(r'^\d{8}-\d{6}-[0-9a-f]+-(\d+)-')
MQ = re.compile(r'(^|/)mq (run|land)\b')
LOGS = os.path.expanduser('~/.claude/state/logs')


def out(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def ps_table():
    """pid -> (ppid, command) from one ps call."""
    table = {}
    for line in out(['env', 'LC_ALL=C', 'ps', '-axo', 'pid=,ppid=,command=']).splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else '')
    return table


def cwd_name(pid):
    for line in out(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn']).splitlines():
        if line.startswith('n/'):
            return os.path.basename(line[1:].rstrip('/')) or None
    return None


WINDOW_S = 86400   # workers launched in the last 24 h (batches' window)
MAX_RECORDS = 300
_records = {}      # .run path -> ((mtime, size), {key: pid})


def worker_pids(root=LOGS, now=None):
    """pid -> worker name from the newest <root>/<project>/<name>.log.run records of the last 24 h
    (archived <name>.<date>.<pid> skipped). Chosen by directory entries before any file is read;
    each record is read once per (mtime, size)."""
    now = now or time.time()
    found = []
    try:
        projects = list(os.scandir(root))
    except OSError:
        return {}
    for entry in projects:
        try:
            if not entry.is_dir() or now - entry.stat().st_mtime > WINDOW_S:
                continue
            files = list(os.scandir(entry.path))
        except OSError:
            continue
        for f in files:
            stem = f.name[:-len('.log.run')]
            if not f.name.endswith('.log.run') or not stem or '.' in stem:
                continue
            try:
                info = f.stat()
            except OSError:
                continue
            if now - info.st_mtime <= WINDOW_S:
                found.append((info.st_mtime, info.st_size, f.path, stem))
    found.sort(reverse=True)
    pids, keep = {}, set()
    for mtime, size, path, name in found[:MAX_RECORDS]:
        keep.add(path)
        cached = _records.get(path)
        if not cached or cached[0] != (mtime, size):
            try:
                with open(path, errors='replace') as f:
                    text = f.read(8192)
            except OSError:
                continue
            ids = {}
            for key in ('worker_pid', 'supervisor_pid'):
                hit = re.search(r'^%s=(\d+)$' % key, text, re.M)
                if hit:
                    ids[key] = int(hit[1])
            cached = _records[path] = ((mtime, size), ids)
        for key in ('worker_pid', 'supervisor_pid'):
            if key in cached[1]:
                pids.setdefault(cached[1][key], name)
    for path in set(_records) - keep:
        del _records[path]
    return pids


class Who:
    def __init__(self, logs=LOGS, table=ps_table, cwd=cwd_name, fixture=None):
        self.logs, self.table, self.cwd, self.fixture = logs, table, cwd, fixture
        self.cache = {}   # run id -> label or None

    def label(self, pid, table, workers, landing):
        """Label for one client pid, or None when the client is gone or not a remote-ci process."""
        if pid not in table or 'remote-ci' not in table[pid][1]:
            return None
        seen, at = set(), pid
        while at > 1 and at not in seen:
            seen.add(at)
            if at in workers:
                return workers[at]
            if at not in table:
                break
            if MQ.search(table[at][1]):
                return 'mq landing' + (' ' + landing if landing else '')
            at = table[at][0]
        return self.cwd(pid)

    def annotate(self, rows, landing):
        """Set row['who'] for rows with a run id; landing: project -> its one landing mq lane."""
        if self.fixture is not None:
            for row in rows:
                row['who'] = self.fixture.get(row.get('run'))
            return
        todo = {r['run'] for r in rows if r.get('run') and r['run'] not in self.cache and RUN_PID.match(r['run'])}
        if todo:
            table, workers = self.table(), worker_pids(self.logs)
            projects = {r['run']: r.get('project') for r in rows if r.get('run')}
            for run in todo:
                pid = int(RUN_PID.match(run)[1])
                self.cache[run] = self.label(pid, table, workers, landing.get(projects.get(run)))
        for row in rows:
            row['who'] = self.cache.get(row.get('run'))
        keep = {r.get('run') for r in rows}
        for run in set(self.cache) - keep:
            del self.cache[run]
