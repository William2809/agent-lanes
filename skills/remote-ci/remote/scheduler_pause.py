#!/usr/bin/env python3
"""Durable v2 queued-work maintenance pause and pinned resume."""
import hashlib
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

PARK_VERSION = 1
PARKED = 80


@contextmanager
def admin_lock(ci, timeout=10):
    path = Path(ci)/'pause.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('pause controller is busy')
                time.sleep(.1)
        yield


def marker(ci):
    return Path(ci) / 'pause.json'


def envelope(ci, row):
    return Path(ci).resolve() / 'projects' / row['project'] / 'runs' / row['run_id'] / 'resume.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def kit_digest(directory):
    value = hashlib.sha256()
    for path in sorted((Path(directory)/'bin').iterdir(), key=lambda item: item.name):
        if path.is_file():
            value.update(path.name.encode() + b'\0' + bytes.fromhex(digest(path)))
    return value.hexdigest()


def eligible(row):
    return (row.get('version') == 2 and row.get('park_version') == PARK_VERSION and
            row.get('state') == 'queued' and not any(row.get(key) for key in
            ('workers', 'memory_mb', 'slot', 'process', 'groups', 'granted',
             'quarantined_leases')) and
            not row.get('recovery_of'))


def valid_park(row, ci, procs):
    if row.get('state') != 'parked' or row.get('park_version') != PARK_VERSION:
        return False
    if any(row.get(key) for key in ('owner', 'workers', 'memory_mb', 'slot', 'process',
                                    'groups', 'granted', 'quarantined_leases')):
        return False
    if row.get('deadline', 0) <= time.time():
        return False
    old = procs.get(row.get('parked_owner', -1), {})
    if old and old.get('birth') == row.get('parked_owner_birth'):
        return False
    path = envelope(ci, row)
    try:
        record = json.loads(path.read_text())
        directory = path.parent
        argv = record.get('argv')
        identity = [row.get(key) for key in ('project', 'sha', 'tree', 'run_id')]
        return (path.is_file() and path.resolve() == path.absolute() and
                directory.resolve() == directory.absolute() and
                isinstance(argv, list) and argv[:4] == identity and len(argv) == 9 and
                isinstance(argv[8], str) and argv[8].isdigit() and int(argv[8]) > 0 and
                int(argv[8]) <= row.get('submitted_at', 0) and
                (path.stat().st_mode & 0o777) == 0o600 and
                digest(path) == row.get('park_envelope') and
                record.get('version') == PARK_VERSION and
                record.get('token') == row.get('_token') and
                not (directory/'conf').is_symlink() and
                not any(item.is_symlink() for item in (directory/'bin').iterdir()) and
                digest(directory/'conf') == record.get('config_sha256') and
                kit_digest(directory) == record.get('kit_sha256'))
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def request_pause(queue, ci, reason, procs):
    ci = Path(ci)
    ci.mkdir(parents=True, exist_ok=True)
    freeze = ci/'freeze'
    prior = {}
    try:
        prior = json.loads(marker(ci).read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    create_freeze = not freeze.exists()
    owned_freeze = bool(prior.get('owned_freeze')) or create_freeze
    if create_freeze:
        temp = ci/'freeze.new'
        temp.write_text('pause: %s\n' % reason[:120])
        temp.replace(freeze)
    now = time.time()
    with queue.transaction() as data:
        import scheduler_authority
        live = [row for row in data['jobs'].values() if scheduler_authority.unresolved(row)]
        incompatible = [row for row in live if row.get('state') == 'following' or
                        row.get('park_version') != PARK_VERSION]
        if incompatible:
            data.pop('paused', None)
        else:
            data['paused'] = dict(version=PARK_VERSION, at=now, reason=reason[:120],
                                  incompatible=0)
    temp = marker(ci).with_suffix('.json.new')
    temp.write_text(json.dumps(dict(version=PARK_VERSION, at=now, reason=reason[:120],
                                    owned_freeze=owned_freeze)))
    temp.chmod(0o600)
    temp.replace(marker(ci))
    return len(incompatible)


def park(data, token, owner, owner_birth, envelope_hash, now):
    row = data['jobs'].get(token, {})
    if not data.get('paused') or not eligible(row) or row.get('owner') != owner or row.get('owner_birth') != owner_birth:
        return False
    row.update(state='parked', phase='queued', parked_at=now, parked_owner=owner,
               parked_owner_birth=owner_birth, park_envelope=envelope_hash, blocker=None,
               updated_at=now)
    for key in ('owner', 'start', 'owner_birth'):
        row.pop(key, None)
    return True


def adopt(data, token, owner, start, birth, envelope_hash, nonce, now):
    row = data['jobs'].get(token, {})
    if (row.get('state') != 'starting' or row.get('park_version') != PARK_VERSION or
            row.get('park_envelope') != envelope_hash or row.get('resume_nonce') != nonce):
        return False
    if row.get('resume_owner') != owner or row.get('resume_owner_birth') != birth or row.get('resume_reaping'):
        return False
    if now >= row['deadline']:
        import scheduler
        scheduler.complete(data, token, 75, now)
        row['state'] = 'expired'
        return 'expired'
    row.update(state='queued', owner=owner, start=start, owner_birth=birth,
               lease_generation=row['lease_generation'] + 1, phase='queued', updated_at=now,
               resumed_at=now, blocker=None)
    for key in ('parked_owner', 'parked_owner_birth', 'resume_nonce', 'resume_starting_at',
                'resume_owner', 'resume_owner_birth', 'resume_tree', 'resume_reaping'):
        row.pop(key, None)
    return True


def publish_result(ci, row):
    result = Path(ci)/'projects'/row['project']/'results'/(row['run_id']+'.exit')
    rc = int(row.get('result', 74))
    try:
        current = int(result.read_text().strip())
        if current != rc:
            raise ValueError('parked result conflicts with durable result')
        return rc
    except FileNotFoundError:
        pass
    result.parent.mkdir(parents=True, exist_ok=True)
    temp = result.with_name(result.name + '.%s.new' % os.getpid())
    temp.write_text('%s\n' % rc)
    # Every contender publishes the same terminal ledger result.
    if result.exists() and int(result.read_text().strip()) != rc:
        temp.unlink()
        raise ValueError('parked result conflicts with durable result')
    temp.replace(result)
    return rc


def terminalize_parked(data, token, rc, now):
    import scheduler
    row = data['jobs'][token]
    if row.get('state') != 'parked':
        raise ValueError('only ownerless parked work can be finalized here')
    scheduler.complete(data, token, rc, now)
    row.update(state='expired' if rc == 75 else 'lost', park_terminal=True)


def audit(queue, ci, procs):
    import scheduler
    import scheduler_resume
    import scheduler_process
    now = time.time()
    with queue.transaction() as data:
        recovering = [t for t, r in data['jobs'].items()
                      if r.get('state') == 'starting' and scheduler_resume.due(r, now)]
    for token in recovering:
        scheduler_resume.recover(queue, token, now)
    if recovering:
        procs = scheduler_process.snapshot()
    with queue.transaction() as data:
        for token, row in data['jobs'].items():
            if row.get('park_version') != PARK_VERSION or row.get('state') != 'parked':
                continue
            old = procs.get(row.get('parked_owner', -1), {})
            if old and old.get('birth') == row.get('parked_owner_birth'):
                continue
            rc = 74 if row.get('cancel') else 75 if now >= row.get('deadline', now+1) else None
            if rc is not None:
                terminalize_parked(data, token, rc, now)
        rows = [dict(row, _token=token) for token, row in data['jobs'].items()]
        resuming = bool(data.get('resuming'))
    for row in rows:
        # Normal run.sh owns cleanup and log-before-result publication. Merely
        # advertising pause support must never let an observer finish its run.
        old = procs.get(row.get('parked_owner', -1), {})
        if (row.get('park_terminal') and row.get('state') in scheduler.TERMINAL and
                not (old and old.get('birth') == row.get('parked_owner_birth'))):
            publish_result(ci, row)
    blockers = 0
    parked = 0
    for row in rows:
        if row.get('state') == 'parked' and valid_park(row, ci, procs):
            parked += 1
        else:
            import scheduler_authority
            blockers += int(scheduler_authority.unresolved(row))
    blockers += sum(1 for _ in Path(ci).glob('projects/*/results/*.cleanup'))
    blockers += int(resuming)
    return dict(blockers=blockers, parked=parked)


def gated_launcher(command):
    gate = ('import os,sys; data=sys.stdin.buffer.read(1); '
            'data == b"1" or sys.exit(0); os.execv(sys.argv[1], sys.argv[1:])')
    return subprocess.Popen([sys.executable, '-c', gate, *command], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def stale_starting(row, procs, now):
    owner = row.get('resume_owner')
    birth = row.get('resume_owner_birth')
    launcher = procs.get(owner, {}) if owner else {}
    exact_live = bool(owner and birth and launcher.get('birth') == birth)
    return (row.get('state') == 'starting' and
            now - row.get('resume_starting_at', now) >= 30 and not exact_live)


def resume(queue, ci, procs):
    ci = Path(ci)
    launches = []
    audit(queue, ci, procs)
    import scheduler_process
    procs = scheduler_process.snapshot()
    now = time.time()
    with queue.transaction() as data:
        candidates = {token: dict(row, _token=token) for token, row in data['jobs'].items()
                      if row.get('state') in ('parked', 'starting')}
    validated = {token for token, row in candidates.items()
                 if row.get('state') == 'parked' and valid_park(row, ci, procs)}
    with queue.transaction() as data:
        now = time.time()
        for token, row in data['jobs'].items():
            if row.get('state') != 'parked':
                continue
            if token not in validated:
                raise ValueError('parked row is not safe to resume')
            prior = candidates[token]
            if any(row.get(key) != prior.get(key) for key in
                   ('sequence', 'lease_generation', 'park_envelope', 'deadline')):
                raise ValueError('parked row changed during audit')
            record = json.loads(envelope(ci, row).read_text())
            if record.get('token') != token or record.get('deadline') != row.get('deadline'):
                raise ValueError('resume envelope identity changed')
            nonce = uuid.uuid4().hex
            row.update(state='starting', resume_nonce=nonce, resume_starting_at=now,
                       updated_at=now)
            launches.append((token, envelope(ci, row).parent, record['argv'], nonce))
        # Intake and admission remain paused through the whole adoption wave.
        # Verified children can adopt their rows, but cannot receive a grant
        # until every saved row has left parked/starting.
        data['resuming'] = dict(version=PARK_VERSION, at=time.time())
    children = []
    import scheduler_process
    for token, directory, argv, nonce in launches:
        command = ['/bin/bash', str(directory/'bin/run.sh'), *argv, token, 'resume', nonce]
        child = gated_launcher(command)
        birth = None
        for _ in range(50):
            birth = scheduler_process.snapshot().get(child.pid, {}).get('birth')
            if birth:
                break
            time.sleep(.02)
        if not birth:
            child.stdin.close()
            raise ValueError('resume launcher identity unavailable')
        with queue.transaction() as data:
            row = data['jobs'].get(token, {})
            if (row.get('state') != 'starting' or row.get('resume_nonce') != nonce or
                    row.get('resume_reaping')):
                child.stdin.close()
                continue
            row.update(resume_owner=child.pid, resume_owner_birth=birth, updated_at=time.time())
        # Start an independent bounded observer before opening the launch gate.
        subprocess.Popen([sys.executable, str(Path(__file__).with_name('scheduler_resume.py')),
                          token, nonce], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        child.stdin.write(b'1')
        child.stdin.close()
        children.append(child)
    if launches:
        deadline = time.time() + 30
        while time.time() < deadline:
            with queue.transaction() as data:
                if all(data['jobs'].get(t, {}).get('state') != 'starting' for t, _, _, _ in launches):
                    break
            time.sleep(.1)
        else:
            import scheduler_resume
            for token, _, _, _ in launches:
                scheduler_resume.recover(queue, token)
            raise ValueError('resume adoption timed out; queued work preserved')
    with queue.transaction() as data:
        if any(row.get('state') in ('parked', 'starting') for row in data['jobs'].values()):
            raise ValueError('parked work remains')
        data.pop('resuming', None)
        data.pop('paused', None)
    control = json.loads(marker(ci).read_text()) if marker(ci).exists() else {}
    paths = [marker(ci)]
    if control.get('owned_freeze'):
        paths.append(ci/'freeze')
    for path in paths:
        if path.exists():
            target = ci/'logs'/(path.name + '.last')
            target.parent.mkdir(parents=True, exist_ok=True)
            path.replace(target)
    return len(launches)


def expire(queue, ci, project, run, now):
    import scheduler
    import scheduler_process
    audit(queue, ci, scheduler_process.snapshot())
    if not project or not run or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in project+run):
        raise ValueError('invalid parked identity')
    with queue.transaction() as data:
        matches = [(token, row) for token, row in data['jobs'].items()
                   if row.get('project') == project and row.get('run_id') == run]
        if len(matches) != 1:
            raise ValueError('parked identity is not unique')
        token, row = matches[0]
        if row.get('park_version') != PARK_VERSION:
            raise ValueError('parked deadline not eligible')
        if row.get('state') == 'parked' and now >= row.get('deadline', now+1):
            terminalize_parked(data, token, 75, now)
        elif not row.get('park_terminal') or row.get('state') not in scheduler.TERMINAL:
            raise ValueError('parked deadline not eligible')
        terminal = dict(row)
    return publish_result(ci, terminal)


def main():
    import resources
    action = sys.argv[1]
    queue = resources.Queue()
    ci = Path.home()/'ci'
    import scheduler_process
    procs = scheduler_process.snapshot()
    if action == 'pause':
        if resources.scheduler_version() != 'v2':
            return 75
        reason = (' '.join(sys.argv[2:]) or sys.stdin.read(121) or 'maintenance').strip()[:120]
        with admin_lock(ci):
            count = request_pause(queue, ci, reason, procs)
        print(json.dumps(dict(paused=count == 0, incompatible=count)))
        if count:
            return 75
    elif action == 'status':
        result = audit(queue, ci, procs)
        with queue.transaction() as data:
            result['paused'] = bool(data.get('paused') or data.get('resuming'))
        result['requested'] = marker(ci).exists()
        print(json.dumps(result, sort_keys=True))
    elif action == 'audit':
        print(audit(queue, ci, procs)['blockers'])
    elif action == 'resume':
        with admin_lock(ci):
            print(resume(queue, ci, procs))
    elif action == 'expire':
        if len(sys.argv) != 4:
            return 2
        return expire(queue, ci, sys.argv[2], sys.argv[3], time.time())
    else:
        return 2
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise SystemExit(74)
