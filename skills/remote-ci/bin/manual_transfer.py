#!/usr/bin/env python3
"""Owner-bound local requests for moving a queued remote-ci run.

The board may request a destination, but only the live pool_move process that
registered the run may consume it and withdraw the remote queue row.
"""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time


IDENTITY = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')
RUN = re.compile(r'^[A-Za-z0-9_.-]{1,160}$')
TERMINAL = ('moved', 'kept', 'started', 'ended')
MAX_JSON = 4096


def root_path():
    return Path(os.environ.get('REMOTE_CI_TRANSFER_STATE') or
                Path.home() / '.local/state/remote-ci/transfers')


def process_birth(pid):
    """Stable enough to reject a reused pid; empty means it cannot be trusted."""
    if type(pid) is not int or pid <= 1:
        return ''
    try:
        return subprocess.run(['/bin/ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True,
                              text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def valid(value, pattern=IDENTITY):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def directory(run, root=None):
    if not valid(run, RUN):
        raise ValueError('invalid run')
    return Path(root or root_path()) / run


def publish(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    pending = path.with_name(path.name + '.new-%d-%s' % (os.getpid(), secrets.token_hex(8)))
    with pending.open('w') as stream:
        json.dump(value, stream, separators=(',', ':'))
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)


def load(path):
    try:
        with path.open() as stream:
            raw = stream.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


class Owner:
    def __init__(self, project, run, source, destinations, root=None, pid=None, birth=None):
        if not all((valid(project), valid(source), valid(run, RUN))):
            raise ValueError('invalid transfer owner')
        destinations = sorted(set(destinations))
        if any(not valid(item) or item == source for item in destinations):
            raise ValueError('invalid transfer destination')
        self.dir = directory(run, root)
        self.pid = pid or os.getpid()
        self.birth = birth if birth is not None else process_birth(self.pid)
        if not self.birth:
            raise ValueError('owner birth unavailable')
        self.record = dict(version=1, project=project, run=run, source=source,
                           destinations=destinations, pid=self.pid, birth=self.birth,
                           state='watching', updated=time.time())
        publish(self.dir / 'owner.json', self.record)

    def request(self):
        current = load(self.dir / 'owner.json')
        owner_keys = ('version', 'run', 'source', 'pid', 'birth')
        if not current or any(current.get(key) != self.record.get(key) for key in owner_keys):
            return None
        value = load(self.dir / 'request.json')
        expected = dict(version=1, run=self.record['run'], source=self.record['source'],
                        owner_pid=self.pid, owner_birth=self.birth)
        if not value or any(value.get(key) != wanted for key, wanted in expected.items()):
            return None
        destination = value.get('destination')
        return destination if destination in self.record['destinations'] else None

    def result(self, state, reason=None, destination=None):
        if state not in TERMINAL:
            raise ValueError('invalid transfer result')
        current = load(self.dir / 'owner.json')
        owner_keys = ('version', 'run', 'source', 'pid', 'birth')
        if not current or any(current.get(key) != self.record.get(key) for key in owner_keys):
            return False
        value = dict(self.record, state=state, updated=time.time())
        if reason:
            value['reason'] = str(reason)[:160]
        if destination in self.record['destinations']:
            value['destination'] = destination
        publish(self.dir / 'owner.json', value)
        return True


def owner_live(value):
    pid = value.get('pid')
    birth = value.get('birth')
    return bool(birth and process_birth(pid) == birth)


def status(run, source, destination, root=None):
    """Public board shape. Never returns pid, birth, paths, routes, or queue tokens."""
    try:
        value = load(directory(run, root) / 'owner.json')
    except ValueError:
        value = None
    if not value or value.get('version') != 1 or value.get('run') != run or value.get('source') != source:
        return dict(available=False, reason='client does not support transfer (old or pinned)')
    state = value.get('state')
    if state in TERMINAL:
        reason = value.get('reason') or {'moved': 'move accepted', 'started': 'check already started',
                                         'kept': 'source kept the check', 'ended': 'client ended'}[state]
        return dict(available=False, state=state, reason=reason)
    if destination not in value.get('destinations', []):
        return dict(available=False, reason='PC is incompatible with this check')
    if not owner_live(value):
        return dict(available=False, reason='client does not support transfer (old or ended)')
    request = load(directory(run, root) / 'request.json')
    if request:
        bound = (request.get('owner_pid') == value.get('pid') and
                 request.get('owner_birth') == value.get('birth') and
                 request.get('source') == value.get('source'))
        if not bound:
            return dict(available=False, reason='move request belongs to an earlier client')
        if request.get('destination') == destination:
            return dict(available=False, state='pending', reason='move requested')
        return dict(available=False, reason='another move is already requested')
    return dict(available=True, destination=destination)


def request(run, source, destination, configured, root=None):
    if not all((valid(run, RUN), valid(source), valid(destination))):
        raise ValueError('invalid request')
    if destination not in configured or source not in configured or destination == source:
        raise ValueError('unknown runner')
    owner_path = directory(run, root) / 'owner.json'
    owner = load(owner_path)
    current = status(run, source, destination, root)
    if not current.get('available'):
        if current.get('state') == 'pending':
            return current
        raise ValueError(current.get('reason') or 'move unavailable')
    confirmed = load(owner_path)
    identity = ('version', 'run', 'source', 'pid', 'birth')
    if not owner or not confirmed or any(owner.get(key) != confirmed.get(key) for key in identity):
        raise ValueError('transfer owner changed; refresh and retry')
    path = directory(run, root) / 'request.json'
    # Bind the request to the exact watcher registration observed above. If that
    # pid is reused or the run is re-registered, the new owner cannot consume it.
    value = dict(version=1, run=run, source=source, destination=destination,
                 owner_pid=confirmed.get('pid'), owner_birth=confirmed.get('birth'), requested=time.time())
    try:
        with path.open('x') as stream:
            json.dump(value, stream, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        existing = load(path)
        keys = ('run', 'source', 'destination', 'owner_pid', 'owner_birth')
        if not existing or any(existing.get(key) != value.get(key) for key in keys):
            raise ValueError('another move is already requested')
    return dict(available=False, state='pending', reason='move requested')
