"""Run one infrastructure command to a monotonic deadline and reap its tracked tree."""
import argparse
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import resources
from process_tree import Tree, birth, identity


def snapshot():
    procs = resources.processes()
    for pid, row in procs.items():
        try:
            row['birth'], row['parent'] = identity(pid)
        except (OSError, ValueError, IndexError):
            row['birth'] = None
    return procs


def reap_descendants(tree):
    """Reap verified descendants while the pipe-gated leader stays pinned."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        members = tree.observe(snapshot())
        targets = [pid for pid in members if pid != tree.root]
        if not targets:
            return True
        for pid in targets:
            try:
                if birth(pid) == tree.known[pid]:
                    os.kill(pid, sig)
            except (ProcessLookupError, FileNotFoundError):
                pass
            except (OSError, ValueError, IndexError):
                return False
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if not [pid for pid in tree.observe(snapshot()) if pid != tree.root]:
                return True
            time.sleep(.05)
    return not [pid for pid in tree.observe(snapshot()) if pid != tree.root]


def run(seconds, phase, timeout_code, reap_code, ownership, token, allow_success_descendants, command, legacy_cleanup=False):
    interrupted = [False]
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda _s, _f: interrupted.__setitem__(0, True))
    gate_reader, gate_writer = os.pipe()
    result_reader, result_writer = os.pipe()
    child = None
    tree = None
    code = timeout_code
    clean = False
    registered = False
    try:
        registered = resources.Queue().register_infra_tree(
            token, ownership, allow_missing=legacy_cleanup and phase == 'cleanup')
        child = subprocess.Popen([
            sys.executable, '-c',
            'import os,signal,subprocess,sys; gate=int(sys.argv[1]); result=int(sys.argv[2]); '
            'signal.signal(signal.SIGALRM,lambda *_: os.killpg(0,signal.SIGKILL)); '
            'signal.alarm(int(sys.argv[3])); '
            'ok=os.read(gate,1); sys.exit(74) if ok != b"1" else None; '
            'rc=subprocess.Popen(sys.argv[4:]).wait(); '
            'os.write(result,str(rc).encode()); os.close(result); os.read(gate,1)',
            str(gate_reader), str(result_writer), str(seconds + 2), *command,
        ], pass_fds=(gate_reader, result_writer), start_new_session=True)
        os.close(result_writer)
        result_writer = None
        tree = Tree(child.pid)
        if child.pid not in tree.known:
            return reap_code
        tree.save(ownership)
        os.write(gate_writer, b'1')
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or interrupted[0]:
                print('== infrastructure stopped phase=%s rc=%s' % (phase, timeout_code), flush=True)
                code = timeout_code
                break
            if select.select([result_reader], [], [], min(0.2, remaining))[0]:
                result = os.read(result_reader, 32)
                code = int(result) if result else timeout_code
                if code < 0:
                    code = 128 + abs(code)
                if allow_success_descendants and code == 0:
                    os.write(gate_writer, b'1')
                    child.wait(timeout=2)
                    clean = True
                elif reap_descendants(tree):
                    os.write(gate_writer, b'1')
                    child.wait(timeout=2)
                    clean = not tree.observe(snapshot())
                break
            tree.observe(snapshot())
            tree.save(ownership)
    finally:
        os.close(gate_reader)
        if result_writer is not None:
            os.close(result_writer)
        os.close(result_reader)
        if tree and not clean:
            clean = tree.reap(snapshot)
        elif child is None:
            clean = True
        os.close(gate_writer)
        if child:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                clean = False
        if not clean:
            code = reap_code
        else:
            Path(ownership).unlink(missing_ok=True)
            if registered:
                try:
                    resources.Queue().clear_infra_tree(token, ownership)
                except ValueError:
                    code = reap_code
    return code


def reap_marker(path, token=None):
    marker = Path(path)
    if not marker.exists():
        return 0
    tree = Tree.load(marker)
    if not tree.reap(snapshot):
        return 76
    marker.unlink(missing_ok=True)
    if token:
        resources.Queue().clear_infra_tree(token, str(marker), allow_missing=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, default=1)
    parser.add_argument('--phase', default='cleanup')
    parser.add_argument('--timeout-code', type=int, default=74)
    parser.add_argument('--reap-code', type=int, default=76)
    parser.add_argument('--ownership')
    parser.add_argument('--token')
    parser.add_argument('--reap-marker')
    parser.add_argument('--allow-success-descendants', action='store_true')
    parser.add_argument('--legacy-cleanup', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if args.reap_marker:
        return reap_marker(args.reap_marker, args.token)
    if args.timeout < 1 or not command or not args.ownership or not args.token:
        return 74
    return run(args.timeout, args.phase, args.timeout_code, args.reap_code,
               args.ownership, args.token, args.allow_success_descendants, command, args.legacy_cleanup)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        sys.exit(76)
