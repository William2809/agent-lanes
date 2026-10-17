#!/usr/bin/env python3
"""Crash-recoverable, one-host remote-ci kit upgrade state machine."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

import resources
import scheduler_authority
import scheduler_pause
import scheduler_process


TERMINAL = {'ready', 'aborted'}
IDENTITY = ('version', 'protocol', 'project', 'run_id', 'sha', 'tree', 'sequence',
            'submitted_at', 'deadline', 'generation', 'command_digest')


def ci_root():
    return Path.home() / 'ci'


def journal_path():
    return ci_root() / 'upgrade.json'


@contextmanager
def host_lock(timeout=10):
    ci = ci_root()
    ci.mkdir(parents=True, exist_ok=True)
    with (ci/'upgrade.lock').open('a+') as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('upgrade controller is busy')
                time.sleep(.1)
        yield


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.new')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load():
    return json.loads(journal_path().read_text())


def save(state):
    state['updated_at'] = time.time()
    state.pop('last_error', None)
    atomic_json(journal_path(), state)


def fail(state, message):
    state['last_error'] = message
    state['updated_at'] = time.time()
    atomic_json(journal_path(), state)
    raise ValueError(message)


def safe_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value) or value in ('.', '..'):
        raise ValueError('invalid upgrade ID')
    return value


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def payload_manifest(directory):
    root = Path(directory)
    result = {}
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_file() and path.name not in ('VERSION', 'PROTOCOL', 'SOURCE'):
            result[path.name] = file_hash(path)
    return result


def immutable_copy(source, target):
    if target.exists():
        raise ValueError('upgrade stage already exists')
    shutil.copytree(source, target)
    for path in target.rglob('*'):
        if path.is_file():
            path.chmod(0o555 if os.access(path, os.X_OK) else 0o444)
        elif path.is_dir():
            path.chmod(0o555)
    target.chmod(0o555)


def clone_stage(source, target):
    shutil.copytree(source, target)
    for path in [target, *target.rglob('*')]:
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o755 if os.access(path, os.X_OK) else 0o644)


def kit_record(path):
    path = Path(path)
    if not path.exists():
        return None
    resolved = path.resolve()
    result = dict(path=str(resolved), manifest=payload_manifest(resolved))
    for name, key in (('VERSION', 'revision'), ('PROTOCOL', 'protocol'), ('SOURCE', 'source')):
        value = resolved/name
        result[key] = value.read_text().strip() if value.exists() else ''
    return result


def unresolved_snapshot(queue):
    accepted = {}
    with queue.transaction() as data:
        for token, row in data['jobs'].items():
            if scheduler_authority.unresolved(row):
                accepted[token] = {key: row.get(key) for key in IDENTITY}
                accepted[token]['_legacy_may_retire'] = row.get('version') != 2
    return accepted


def audit_snapshot(queue, expected):
    with queue.transaction() as data:
        for token, identity in expected.items():
            row = data['jobs'].get(token)
            if row is None:
                if identity.get('_legacy_may_retire'):
                    run = identity.get('run_id')
                    if not isinstance(run, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', run) or run in ('.', '..'):
                        raise ValueError('retired legacy queue record has no safe run identity: %s' % token)
                    receipts = list(ci_root().glob('projects/*/results/%s.exit' % run))
                    if (len(receipts) != 1 or not receipts[0].is_file() or
                            receipts[0].is_symlink()):
                        raise ValueError('retired legacy queue record has no unique completion receipt: %s' % token)
                    try:
                        result = receipts[0].read_text().strip()
                    except OSError:
                        result = ''
                    if not re.fullmatch(r'[0-9]+', result):
                        raise ValueError('retired legacy queue record has invalid completion receipt: %s' % token)
                    continue
                raise ValueError('accepted queue record is missing: %s' % token)
            if any(row.get(key) != identity.get(key) for key in IDENTITY):
                raise ValueError('accepted queue identity changed: %s' % token)


def freeze_record(path):
    stat = path.stat()
    return dict(sha256=file_hash(path), device=stat.st_dev, inode=stat.st_ino)


def pause_control():
    path = scheduler_pause.marker(ci_root())
    return json.loads(path.read_text()) if path.exists() else None


def freeze_owned(state):
    path = ci_root()/'freeze'
    if not path.exists():
        raise ValueError('upgrade freeze marker is missing; refusing resume')
    current = freeze_record(path)
    control = pause_control()
    return (current['sha256'] == state['freeze']['sha256'] and control is not None and
            control.get('owned_freeze') is True and
            control.get('reason') == state['pause_reason'])


def preserve_changed_freeze():
    path = scheduler_pause.marker(ci_root())
    control = json.loads(path.read_text())
    control['owned_freeze'] = False
    atomic_json(path, control)


def pause_marker_owned(state):
    control = pause_control()
    return (control is not None and control.get('owned_freeze') is True and
            control.get('reason') == state.get('pause_reason'))


def find_installed(state):
    candidates = []
    for path in (ci_root()/'kits').glob(state['revision'] + '-*'):
        try:
            if ((path/'VERSION').read_text().strip() == state['revision'] and
                    (path/'PROTOCOL').read_text().strip() == state['protocol'] and
                    (path/'SOURCE').read_text().strip() == state['source']):
                candidates.append(path)
        except OSError:
            pass
    if not candidates:
        raise ValueError('installed candidate marker not found')
    return max(candidates, key=lambda path: path.stat().st_mtime_ns).resolve()


def candidate_selected(state):
    try:
        target = find_installed(state)
        return (ci_root()/'kit').exists() and (ci_root()/'kit').resolve() == target
    except (OSError, ValueError):
        return False


def verify_installed(state):
    target = find_installed(state)
    kit = ci_root()/'kit'
    if not kit.exists() or kit.resolve() != target:
        raise ValueError('host kit does not select the installed candidate')
    if payload_manifest(target) != state['manifest']:
        raise ValueError('installed candidate payload differs from prepared stage')
    bin_dir = ci_root()/'bin'
    for name, digest in state['manifest'].items():
        if Path(name).suffix in ('.py', '.sh'):
            path = bin_dir/name
            if not path.is_file() or file_hash(path) != digest:
                raise ValueError('public bin differs from installed kit: %s' % name)
    hook = target/'pre-receive.sh'
    if hook.exists():
        expected = file_hash(hook)
        for repo in (ci_root()/'projects').glob('*/repo.git'):
            public = repo/'hooks/pre-receive'
            if not public.is_file() or file_hash(public) != expected:
                raise ValueError('repository protocol hook differs from installed kit: %s' % repo.parent.name)
    return target


def installer():
    installed = ci_root()/'kit/install-kit.sh'
    return installed if installed.exists() else Path(__file__).with_name('install-kit.sh')


def install_count():
    result = subprocess.run(['/bin/bash', str(installer()), 'count'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('cannot count unresolved runner work')
    value = result.stdout.strip()
    if not value.isdigit():
        raise ValueError('invalid unresolved runner count')
    return int(value)


def prepare(identifier, supplied, revision, protocol, source):
    identifier = safe_id(identifier)
    if not re.fullmatch(r'[0-9a-f]+', revision+protocol+source):
        raise ValueError('revision, protocol and source must be hexadecimal')
    ci = ci_root()
    source_path = Path(supplied).resolve()
    if not source_path.is_dir():
        raise ValueError('candidate stage is not a directory')
    manifest = payload_manifest(source_path)
    if journal_path().exists():
        prior = load()
        same = (prior.get('id') == identifier and prior.get('revision') == revision and
                prior.get('protocol') == protocol and prior.get('source') == source and
                prior.get('manifest') == manifest)
        if same:
            return prior
        if prior.get('phase') not in TERMINAL or prior.get('id') == identifier:
            raise ValueError('another upgrade is active: %s' % prior.get('id', 'unknown'))
    if (ci/'freeze').exists():
        raise ValueError('runner is already frozen')
    previous = kit_record(ci/'kit')
    if previous and previous['protocol'] != protocol:
        raise ValueError('protocol migration is not supported by rolling upgrade')
    stage = ci/'upgrades'/identifier/('stage-%s-%s' %
                                      (hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:12],
                                       time.time_ns()))
    stage.parent.mkdir(parents=True, exist_ok=True)
    immutable_copy(source_path, stage)
    state = dict(schema='remote-ci.upgrade/v1', id=identifier, phase='prepared',
                 revision=revision, protocol=protocol, source=source,
                 stage=str(stage), manifest=manifest, previous=previous,
                 accepted=unresolved_snapshot(resources.Queue()), original_freeze_absent=True,
                 created_at=time.time())
    save(state)
    return state


def advance(identifier):
    state = load()
    if state.get('id') != safe_id(identifier):
        raise ValueError('upgrade ID does not match active journal')
    if state['phase'] in TERMINAL:
        return state
    queue = resources.Queue()
    try:
        if state['phase'] == 'prepared':
            state.update(phase='pausing', pause_reason='rolling upgrade %s' % identifier)
            save(state)
            return state
        if state['phase'] == 'pausing':
            freeze = ci_root()/'freeze'
            if freeze.exists():
                control = pause_control()
                expected = ('pause: %s\n' % state['pause_reason']).encode()
                if (control is None or control.get('reason') != state['pause_reason'] or
                        control.get('owned_freeze') is not True or freeze.read_bytes() != expected):
                    fail(state, 'freeze appeared outside this upgrade; refusing to adopt it')
                with queue.transaction() as data:
                    compatible_pause = bool(data.get('paused'))
                incompatible = 0 if compatible_pause else max(1, state.get('incompatible', 0))
            else:
                incompatible = scheduler_pause.request_pause(
                    queue, ci_root(), state['pause_reason'], scheduler_process.snapshot())
            audit_snapshot(queue, state['accepted'])
            # Freeze is now visible, so this closes the prepare-to-pause intake
            # window and records every compatible request accepted before it.
            state['accepted'].update(unresolved_snapshot(queue))
            state.update(phase='draining', pause_mode='drain' if incompatible else 'park',
                         incompatible=incompatible,
                         freeze=freeze_record(ci_root()/'freeze'))
            save(state)
            return state
        if state['phase'] == 'draining':
            if not freeze_owned(state):
                fail(state, 'upgrade freeze changed; restore its recorded pause marker before install')
            if install_count():
                return state
            audit_snapshot(queue, state['accepted'])
            # The installer atomically selects the kit before this journal can
            # record the transition. Reconcile that narrow crash window instead
            # of publishing a second copy of the same candidate.
            try:
                target = verify_installed(state)
            except ValueError:
                target = None
            if target is not None:
                state.update(phase='installed', target=str(target))
                save(state)
                return state
            state['phase'] = 'installing'
            save(state)
            return state
        if state['phase'] == 'installing':
            if not freeze_owned(state):
                fail(state, 'upgrade freeze changed; restore its recorded pause marker before install')
            if install_count():
                fail(state, 'runner work appeared after the install drain')
            audit_snapshot(queue, state['accepted'])
            try:
                target = verify_installed(state)
            except ValueError:
                target = None
            if target is not None:
                state.update(phase='installed', target=str(target))
                save(state)
                return state
            attempt = ci_root()/'upgrades'/identifier/('install-%s' % time.time_ns())
            clone_stage(state['stage'], attempt)
            result = subprocess.run(['/bin/bash', str(attempt/'install-kit.sh'), 'install',
                                     str(attempt), state['revision'], state['protocol'], state['source']],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                fail(state, 'candidate install failed; host remains frozen')
            state.update(phase='installed', target=str(find_installed(state)))
            save(state)
            return state
        if state['phase'] == 'installed':
            verify_installed(state)
            audit_snapshot(queue, state['accepted'])
            owned = freeze_owned(state)
            if not owned:
                if not pause_marker_owned(state):
                    fail(state, 'pause ownership changed; refusing to rewrite operator maintenance')
                preserve_changed_freeze()
                state['manual_freeze_preserved'] = True
            state['phase'] = 'resuming'
            save(state)
            return state
        if state['phase'] == 'resuming':
            verify_installed(state)
            audit_snapshot(queue, state['accepted'])
            control = pause_control()
            with queue.transaction() as data:
                queue_paused = bool(data.get('paused') or data.get('resuming'))
            if control is not None or queue_paused:
                if not (ci_root()/'freeze').exists():
                    fail(state, 'pause remains recorded but upgrade freeze is missing')
                if not state.get('manual_freeze_preserved') and not freeze_owned(state):
                    fail(state, 'upgrade freeze changed before resume')
                if state.get('manual_freeze_preserved') and control and control.get('owned_freeze'):
                    fail(state, 'manual freeze ownership was not relinquished')
                scheduler_pause.resume(queue, ci_root(), scheduler_process.snapshot())
            # A crash after resume archives pause.json and clears both queue
            # gates. That completed state is the only missing-freeze case accepted.
            with queue.transaction() as data:
                if data.get('paused') or data.get('resuming'):
                    fail(state, 'scheduler pause remains after resume')
            if scheduler_pause.marker(ci_root()).exists():
                fail(state, 'pause marker remains after resume')
            audit_snapshot(queue, state['accepted'])
            state['phase'] = 'ready'
            save(state)
            return state
        fail(state, 'unknown upgrade phase: %s' % state['phase'])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if state.get('last_error') != str(error):
            state['last_error'] = str(error)
            state['updated_at'] = time.time()
            atomic_json(journal_path(), state)
        raise


def abort(identifier):
    state = load()
    if state.get('id') != safe_id(identifier):
        raise ValueError('upgrade ID does not match active journal')
    if state['phase'] == 'aborted':
        return state
    if state['phase'] == 'ready':
        raise ValueError('completed upgrade cannot be aborted')
    if state['phase'] in ('installed', 'resuming') or candidate_selected(state):
        raise ValueError('installed upgrade cannot be aborted safely')
    if state['phase'] in ('pausing', 'draining', 'installing'):
        if state['phase'] == 'installing':
            raise ValueError('installing upgrade cannot be aborted safely')
        if state['phase'] == 'pausing':
            control = pause_control()
            if (not (ci_root()/'freeze').exists() or control is None or
                    control.get('reason') != state.get('pause_reason')):
                state['phase'] = 'aborted'
                save(state)
                return state
        if not freeze_owned(state):
            if not pause_marker_owned(state):
                raise ValueError('pause ownership changed; refusing to rewrite operator maintenance')
            preserve_changed_freeze()
            state['manual_freeze_preserved'] = True
        scheduler_pause.resume(resources.Queue(), ci_root(), scheduler_process.snapshot())
        audit_snapshot(resources.Queue(), state['accepted'])
    state['phase'] = 'aborted'
    save(state)
    return state


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise ValueError('usage: upgrade_host.py prepare|advance|status|abort')
    action = argv.pop(0)
    with host_lock():
        if action == 'prepare' and len(argv) == 5:
            result = prepare(*argv)
        elif action == 'advance' and len(argv) == 1:
            result = advance(argv[0])
        elif action == 'abort' and len(argv) == 1:
            result = abort(argv[0])
        elif action == 'status' and not argv:
            result = load() if journal_path().exists() else dict(phase='idle')
        else:
            raise ValueError('usage: upgrade_host.py prepare ID STAGE REV PROTOCOL SOURCE | advance ID | status | abort ID')
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('upgrade-host: %s' % error, file=sys.stderr)
        raise SystemExit(74)
