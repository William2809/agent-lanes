"""Bound a submission transport and its child group, including inherited pipes."""
import os
import select
import signal
import subprocess
import sys
import time


def run(seconds, command):
    if seconds <= 0 or not command:
        return 74
    stopped = [False]
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.__setitem__(0, True))
    reader, writer = os.pipe()
    gate_read, gate_write = os.pipe()
    child = None
    result = 74
    completed = False
    try:
        # Keep the group leader alive until all transport descendants are stopped.
        # Its own alarm bounds orphan lifetime if this supervisor disappears.
        child = subprocess.Popen([
            sys.executable, '-c',
            'import os,signal,subprocess,sys; out=int(sys.argv[1]); gate=int(sys.argv[2]); '
            'signal.signal(signal.SIGALRM,lambda *_: os.killpg(0,signal.SIGKILL)); '
            'signal.signal(signal.SIGTERM,lambda *_: None); '
            'signal.alarm(int(sys.argv[3])); '
            'rc=subprocess.Popen(sys.argv[4:]).wait(); '
            'os.write(out,str(rc).encode()); os.close(out); os.read(gate,1)',
            str(writer), str(gate_read), str(seconds + 2), *command,
        ], pass_fds=(writer, gate_read), start_new_session=True)
        os.close(writer)
        writer = None
        deadline = time.monotonic() + seconds
        while not stopped[0]:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if select.select([reader], [], [], min(left, .2))[0]:
                value = os.read(reader, 32)
                completed = bool(value)
                result = int(value) if value else 74
                result = result if result >= 0 else 128 - result
                break
        if not completed:
            print('lost-run: submission deadline or transport interruption', file=sys.stderr)
    finally:
        if child is not None:
            # Do not reap the leader until after KILL: its PID anchors group ownership.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass
                if sig == signal.SIGTERM:
                    time.sleep(.1)
            child.wait(timeout=2)
        for fd in (reader, writer, gate_read, gate_write):
            if fd is not None:
                os.close(fd)
    return result


if __name__ == '__main__':
    try:
        sys.exit(run(int(sys.argv[1]), sys.argv[2:]))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        sys.exit(74)
