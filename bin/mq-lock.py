#!/usr/bin/env python3
"""Keep one kernel lock across runner startup, exec, and landing children."""
import fcntl
import os
from pathlib import Path
import subprocess
import shlex
import sys
import time


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ''


def live_records(lock):
    # Legacy records have no birth. Keep them live until the PID exits or the user clears them.
    seen = set()
    while lock.is_dir() and lock.resolve() not in seen:
        seen.add(lock.resolve())
        owner = read(lock / 'pid')
        for name in ('pid', 'handoff', 'mover'):
            value = read(lock / name)
            if not value.isdecimal() or int(value) <= 0:
                continue
            if name == 'handoff' and value == owner:
                continue  # The same owner, not a second process.
            birth = read(lock / ('birth' if name == 'pid' else name + '_birth'))
            pid = int(value)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            if birth:
                current = subprocess.run(['ps', '-p', value, '-o', 'lstart='],
                                         env={**os.environ, 'LC_ALL': 'C'}, text=True,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
                if current and current != birth:
                    continue
            yield lock / name, pid, birth
        lock = lock / 'reap'


def older_owner(lock, own):
    return next((pid for _, pid, birth in live_records(lock)
                 if pid != own or not birth), None)


def waiting(pid):
    print(f'mq: an older runner (pid {pid}) still holds the queue; it will be picked up when it exits')


def main():
    if sys.argv[1] == 'older':
        pid = older_owner(Path(sys.argv[2]), int(sys.argv[3]))
        if pid is not None:
            waiting(pid)
        return 0 if pid is not None else 1
    if sys.argv[1] == 'status':
        lock = Path(sys.argv[2])
        records = [(path.relative_to(lock), pid) for path, pid, birth in live_records(lock) if not birth]
        for name, pid in records:
            print(f'mq: older queue lock {name} pid {pid} has no start time; PID may have been reused')
        if records:
            print('mq: if these PIDs belong to other processes, clear with: mv '
                  + shlex.quote(str(lock)) + ' "$(mktemp -d ' + shlex.quote(str(lock) + '.stale.XXXXXX') + ')/" && mq run')
        return 0
    if sys.argv[1] == 'check':
        held, expected = os.fstat(9), os.stat(sys.argv[2])
        return 0 if (held.st_dev, held.st_ino) == (expected.st_dev, expected.st_ino) else 1
    if sys.argv[1] == 'land':
        # Old children do not hold the guard. Check their shared PID file at every land,
        # including retries, after candidate selection and immediately before spawning.
        try:
            lock = Path(os.environ['MQ_RUN_LOCK'])
            identity = lock.stat()
            owned = (f'{identity.st_dev}:{identity.st_ino}' == os.environ['MQ_RUN_LOCK_ID']
                     and (lock / 'pid').read_text().strip() == os.environ['MQ_RUN_OWNER']
                     and (lock / 'birth').read_text().strip() == os.environ['MQ_RUN_BIRTH'])
        except (OSError, KeyError):
            owned = False
        if not owned:
            print('FAILED runner busy ownership changed')
            return 1
        # Keep the inherited lock in this supervisor, not in refresh servers or check daemons.
        rc = subprocess.call(sys.argv[2:], close_fds=True,
                             env={**os.environ, 'MQ_RUN_OWNER': ''})
        return rc if rc >= 0 else 128 - rc
    mode, script = sys.argv[1:]
    common = subprocess.check_output(
        ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
        stderr=subprocess.DEVNULL, text=True).strip()
    state = Path.home() / '.claude/state/mq' / Path(common).parent.name
    # Never unlink this file. All contenders must open the same inode.
    guard = os.open(Path(common) / 'mq-run.guard', os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + 5
    while True:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            # Earlier guard-enabled releases still used birth-less owner records.
            old = next((pid for _, pid, birth in live_records(state / 'run.lock') if not birth), None)
            if old is not None:
                waiting(old)
                return
            # A crashed owner may have a landing child finishing. Wait only for that case.
            dead = False
            try:
                pid = int((state / 'run.lock/pid').read_text())
                if pid > 0:
                    os.kill(pid, 0)
            except ProcessLookupError:
                dead = True
            except (OSError, ValueError):
                pass
            if mode == 'run' and dead and time.monotonic() < deadline:
                time.sleep(0.05)
                continue
            if mode == 'run':
                print('mq: runner already active; mq add is enough')
            return
    # A free native guard does not authorize takeover from a live old runner or launcher.
    pid = older_owner(state / 'run.lock', os.getpid())
    if pid is not None:
        waiting(pid)
        return
    # Reserve a known descriptor. Fixer launches close it; landing children keep it.
    if guard != 9:
        os.dup2(guard, 9)
        os.close(guard)
    os.set_inheritable(9, True)
    if mode == 'detach':
        state.mkdir(parents=True, exist_ok=True)
        ready_read, ready_write = os.pipe()
        if os.fork():
            os.close(ready_write)
            if os.read(ready_read, 1) != b'1':
                raise OSError('runner failed before registration')
            print(f'mq: runner started (log: {state / "run.log"})')
            return
        os.close(ready_read)
        os.setsid()
        # Publish the child's PID before add returns, so status readers see copying as active.
        lock = state / 'run.lock'
        try:
            lock.mkdir()
        except FileExistsError:
            pass  # The shell still checks an existing legacy owner before landing.
        else:
            birth = subprocess.check_output(['ps', '-p', str(os.getpid()), '-o', 'lstart='],
                                            env={**os.environ, 'LC_ALL': 'C'}, text=True).strip()
            (lock / 'birth').write_text(birth + '\n')
            (lock / 'pid').write_text(f'{os.getpid()}\n')
            (lock / 'handoff').write_text(f'{os.getpid()}\n')
        os.write(ready_write, b'1')
        os.close(ready_write)
        source = os.open(os.devnull, os.O_RDONLY)
        log = os.open(state / 'run.log', os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.dup2(source, 0)
        os.dup2(log, 1)
        os.dup2(log, 2)
        # The detached run makes its own complete bundle.
        os.environ.pop('MQ_COPY', None)
        os.environ.pop('MQ_COPY_OWNER', None)
    # exec retains the PID. Descendants have a different PID and must acquire anew.
    os.environ['MQ_RUN_OWNER'] = str(os.getpid())
    os.environ['MQ_RUN_GUARD'] = str(Path(common) / 'mq-run.guard')
    os.execvp('sh', ['sh', script, 'run'])


if __name__ == '__main__':
    try:
        sys.exit(main() or 0)
    except (OSError, subprocess.CalledProcessError):
        print('mq: cannot start runner', file=sys.stderr)
        sys.exit(1)
