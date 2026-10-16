#!/usr/bin/env python3
"""Keep one kernel lock across runner startup, exec, and landing children."""
import fcntl
import os
from pathlib import Path
import subprocess
import shlex
import signal
import select
import socket
import struct
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


def owns_guard(path):
    try:
        held, expected = os.fstat(9), os.stat(path)
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            return False
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def owns_queue():
    try:
        lock = Path(os.environ['MQ_RUN_LOCK'])
        identity = lock.stat()
        return (owns_guard(os.environ['MQ_RUN_GUARD']) and
                f'{identity.st_dev}:{identity.st_ino}' == os.environ['MQ_RUN_LOCK_ID'] and
                read(lock / 'pid') == os.environ['MQ_RUN_OWNER'] and
                read(lock / 'birth') == os.environ['MQ_RUN_BIRTH'])
    except (OSError, KeyError):
        return False


def receive_guard():
    # A one-use channel passes the native descriptor only to the Python entry gate.
    # Shell wrappers and overridden land commands never inherit the lock itself.
    try:
        fd = int(os.environ.pop('MQ_LAND_CHANNEL'))
        with socket.socket(fileno=fd) as channel:
            channel.settimeout(60)
            channel.sendall(b'land')
            data, ancillary, _, _ = channel.recvmsg(16, socket.CMSG_SPACE(struct.calcsize('i')))
        for level, kind, value in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                guard = struct.unpack('i', value[:struct.calcsize('i')])[0]
                if guard != 9:
                    os.dup2(guard, 9)
                    os.close(guard)
                return data == b'guard'
    except (KeyError, OSError, ValueError):
        pass
    return False


def supervise(args, env, handoff=False):
    # Forward ordinary interruption and keep the lock until the shell restores its lanes.
    server = channel = None
    if handoff:
        server, channel = socket.socketpair()
        env['MQ_LAND_CHANNEL'] = str(channel.fileno())
    child = subprocess.Popen(args, env=env, close_fds=True,
                             pass_fds=(channel.fileno(),) if channel else ())
    if channel:
        channel.close()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: child.send_signal(signum))
    if server:
        with server:
            while child.poll() is None:
                if select.select([server], [], [], 0.1)[0]:
                    if server.recv(16) == b'land':
                        server.sendmsg([b'guard'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack('i', 9))])
                    break
    rc = child.wait()
    return rc if rc >= 0 else 128 - rc


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
        return 0 if owns_guard(sys.argv[2]) else 1
    if sys.argv[1] == 'land':
        if not owns_queue():
            print('FAILED runner busy ownership changed')
            return 1
        # Only the landing entry gate receives the descriptor. Its supervisor keeps it
        # out of checks, hooks and background servers for the entire operation.
        return supervise(sys.argv[2:], os.environ.copy(), handoff=True)
    if sys.argv[1] == 'enter':
        script, *args = sys.argv[2:]
        common = subprocess.check_output(
            ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], text=True).strip()
        guard_path = str(Path(common) / 'mq-run.guard')
        queued = receive_guard() and owns_queue() and owns_guard(guard_path)
        if not queued:
            if os.environ.get('LAND_DIRECT') != '1':
                print('FAILED land runs from the merge queue: mq add WT '
                      '(direct: LAND_DIRECT=1 LAND_REASON="why")')
                return 1
            if not os.environ.get('LAND_REASON'):
                print('FAILED LAND_DIRECT=1 needs LAND_REASON="why"')
                return 1
            guard = os.open(guard_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('FAILED runner busy (landing guard held)')
                return 1
            state = Path.home() / '.claude/state/mq' / Path(common).parent.name
            if older_owner(state / 'run.lock', os.getpid()) is not None:
                print('FAILED runner busy (older queue owner)')
                return 1
        env = {k: v for k, v in os.environ.items() if not k.startswith('MQ_RUN_')}
        env['LAND_DIRECT'] = '0' if queued else '1'
        # Always enter through this gate, never trust an environment flag as a bypass.
        # Read a single script snapshot so the body and this gate cannot split during an upgrade.
        body = Path(script).read_text().split('# LAND_BODY\n', 1)[1]
        return supervise(['sh', '-c', body, script, *args], env)
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
