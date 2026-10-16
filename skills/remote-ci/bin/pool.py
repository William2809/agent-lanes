#!/usr/bin/env python3
"""Runner selection and durable assignments. Policy belongs in host config."""
import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid


class Infra(Exception):
    pass


def number(value, default, minimum=0):
    result = float(default if value is None else value)
    if not math.isfinite(result) or result < minimum:
        raise Infra('invalid runner policy')
    return result


def registry(value, env):
    runners = []
    names = set()
    for entry in value.split():
        name, sep, rest = entry.partition('=')
        route, *caps = rest.split(':')
        if not sep or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', name) or name in names:
            raise Infra('invalid runner identity')
        host = route.rsplit('@', 1)[-1].lower()
        if (not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', route)
                or host in ('local', 'localhost', '::1', '0.0.0.0') or host.startswith('127.')
                or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', cap) for cap in caps)):
            raise Infra('invalid runner route or capability; remote runners only')
        prefix = 'CI_RUNNER_' + name + '_'
        enabled = env.get(prefix + 'ENABLED', '1')
        if enabled not in ('0', '1'):
            raise Infra('invalid runner enabled policy')
        runners.append(dict(name=name, route=route, caps=set(caps), enabled=enabled == '1',
            seconds=number(env.get(prefix + 'EXPECTED_SECONDS'), 300, 1),
            cache_bonus=number(env.get(prefix + 'CACHE_BONUS'), 30),
            wake_timeout=number(env.get(prefix + 'WAKE_TIMEOUT'), 60, 1),
            wol=env.get(prefix + 'WOL_COMMAND', '')))
        names.add(name)
    return runners


def score(runner, status):
    # Queue status has no duration history yet. Use an explicit policy estimate.
    # Prefer a supplied ETA when a newer scheduler can provide it.
    jobs = number(status.get('active'), 0) + number(status.get('waiting'), 0)
    slots = number(status.get('policy', {}).get('MAX_JOBS'), 1, 1)
    rounds = max(0, math.ceil((jobs + 1) / slots) - 1)
    if status.get('can_admit') is False:
        rounds = max(1, rounds)
    eta = number(status.get('expected_wait_seconds'), rounds * runner['seconds'])
    # A v2 pressure cooldown (can_admit false) lasts at least cooldown_seconds.
    try:
        cooldown = number(status.get('cooldown_seconds'), 0)
    except (Infra, TypeError, ValueError):
        cooldown = 0  # Optional field: older kits omit it; a bad value is ignored.
    eta = max(eta, cooldown)
    load = number(status.get('load'), 0) / number(status.get('cores'), 1, 1)
    pressure = number(status.get('pressure'), 1, 1)
    return (eta + runner['seconds'] + load * runner['seconds']
            + (runner['seconds'] if pressure != 1 else 0)
            - (min(runner['cache_bonus'], runner['seconds']) if status.get('warm') else 0))


def held(status):
    return bool(status.get('frozen') or status.get('draining'))


NOT_SET_UP = 'project not set up on an eligible runner (remote-ci setup RUNNER)'


def absent(status):
    return status.get('absent') is True


def can_admit(status):
    if held(status) or number(status.get('waiting'), 0):
        return False
    if 'can_admit' in status:
        return status['can_admit'] is True
    # Compatibility with older kits; current kits account for workers/memory.
    return (number(status.get('active'), 0)
            < number(status.get('policy', {}).get('MAX_JOBS'), 1, 1)
            and number(status.get('pressure'), 1, 1) == 1)


def probe_batches(runners, probe):
    """Concurrent probes share one second; a ready reply need not wait for others."""
    deadline = time.monotonic() + 1
    cancel = threading.Event()
    replies = queue.Queue()

    def worker(runner):
        try:
            status = probe(dict(runner, deadline=deadline, cancel=cancel))
            replies.put((runner, status, None))
        except Exception as error:
            replies.put((runner, None, error))

    threads = [threading.Thread(target=worker, args=(runner,), daemon=True) for runner in runners]
    try:
        for thread in threads:
            thread.start()
        pending = len(threads)
        while pending and time.monotonic() < deadline:
            try:
                batch = [replies.get(timeout=max(.001, deadline - time.monotonic()))]
            except queue.Empty:
                break
            while True:
                try:
                    batch.append(replies.get_nowait())
                except queue.Empty:
                    break
            pending -= len(batch)
            for _, _, error in batch:
                if error is not None:
                    raise error
            yield [(runner, status) for runner, status, _ in batch]
    finally:
        cancel.set()
        # Real probes poll cancellation and reap their SSH child. Never join a
        # blocked callback for its network timeout or once per runner.
        cleanup = time.monotonic() + .1
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=max(0, cleanup - time.monotonic()))


def select(runners, needs, probe, wake=None, clock=time.monotonic, sleep=time.sleep):
    eligible = [r for r in runners if r['enabled'] and needs <= r['caps']]
    if not eligible:
        raise Infra('no eligible runner')
    statuses = {}
    batches = probe_batches(eligible, probe)
    try:
        for batch in batches:
            statuses.update((r['name'], status) for r, status in batch)
            ready = [(score(r, statuses[r['name']]), r['name'], r) for r in eligible
                     if statuses.get(r['name']) is not None and not absent(statuses[r['name']])
                     and can_admit(statuses[r['name']])]
            if ready:
                return min(ready, key=lambda row: row[:2])[2]
    finally:
        batches.close()
    candidates, awake, frozen = [], [], []
    missing = False
    for runner in eligible:
        status = statuses.get(runner['name'])
        if status is not None and absent(status):
            missing = True  # Reachable but never set up: not a candidate, wake or fallback.
        elif status is not None:
            row = (score(runner, status), False, runner['name'], runner)
            if held(status):
                frozen.append(row)
            else:
                awake.append(row)
                candidates.append(row)
        elif wake is not None and 'wol' in runner['caps'] and runner['wol']:
            # Unknown cache/load: estimate an empty, cold runner after wake.
            candidates.append((runner['wake_timeout'] + score(runner, {}),
                               True, runner['name'], runner))
    fallback = min(awake or frozen, key=lambda row: row[:3])[3] if awake or frozen else None
    if not candidates and fallback is not None:
        return fallback  # All responding hosts are held: preserve the client's wait.
    if not candidates:
        raise Infra(NOT_SET_UP if missing else 'runner unreachable')
    # An awake runner wins a tie, then identity keeps ordering stable.
    _, asleep, _, runner = min(candidates, key=lambda row: row[:3])
    if not asleep:
        return runner
    # One bounded wake attempt; no scheduler admission or capacity reservation.
    deadline = clock() + runner['wake_timeout']
    try:
        wake(dict(runner, deadline=deadline))
        while clock() < deadline:
            status = probe(dict(runner, deadline=deadline))
            if status is not None and absent(status):
                missing = True
                break
            if status is not None:
                if held(status) and fallback is not None:
                    return fallback
                return runner
            sleep(min(1, max(0, deadline - clock())))
    except (Infra, OSError, subprocess.SubprocessError):
        pass
    if fallback is not None:
        return fallback
    raise Infra(NOT_SET_UP if missing else 'runner asleep')


def publish(path, data):
    pending = path.with_suffix('.new')
    with pending.open('w') as stream:
        json.dump(data, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def exclusive(path):
    with path.open('a') as lock:
        deadline = time.monotonic() + 90
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Infra('assignment busy')
                time.sleep(.1)
        yield


def assign(root, job, choose):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', job):
        raise Infra('invalid assignment job')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (job + '.json')
    with exclusive(root / (job + '.lock')):
        row = json.loads(path.read_text()) if path.exists() else dict(job=job, state='pending')
        if row['state'] == 'assigned':
            return row
        publish(path, row)
        runner = choose()
        row.update(state='assigned', runner=runner['name'], route=runner['route'], generation=uuid.uuid4().hex)
        publish(path, row)
        return row


KEEP = 100  # Shas remembered per project for follow-ups (fail SHA, blame SHA).


def record(root, project, sha, runner):
    """Remember which runner ran a project's sha; follow-ups read the same logs."""
    if not re.fullmatch(r'[0-9a-f]{40,64}', sha):
        raise Infra('invalid sha')
    folder = root / 'projects'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (project + '.json')
    with exclusive(folder / (project + '.lock')):
        row = json.loads(path.read_text()) if path.exists() else {}
        runs = [run for run in row.get('runs', []) if run[0] != sha][-(KEEP - 1):]
        publish(path, dict(latest=runner, runs=runs + [[sha, runner]]))


def recorded(root, project, target):
    """Runner name for 'latest' or a sha prefix (newest match), else None."""
    try:
        row = json.loads((root / 'projects' / (project + '.json')).read_text())
        if target == 'latest':
            return row.get('latest')
        return next((name for sha, name in reversed(row.get('runs', []))
                     if sha.startswith(target)), None)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def probe(runner, project, setup=False):
    # A runner without this project cannot run it: report that before any hold.
    command = 'true' if setup else (
        'if ! test -d ~/ci/projects/%s/repo.git; then echo \'{"absent":true}\'; exit; fi; '
        'if test -f ~/ci/freeze; then echo \'{"frozen":true}\'; exit; fi; '
        'owner=$(cat ~/ci/drain 2>/dev/null); case "$owner" in ""|0|*[!0-9]*) ;; '
        '*) if kill -0 "$owner" 2>/dev/null; then echo \'{"draining":true}\'; exit; fi ;; esac; '
        '(python3 ~/ci/bin/resources.py status --json 2>/dev/null || '
        'python3 ~/ci/bin/resources.py status) && '
        'if test -d ~/ci/projects/%s/work/node_modules || '
        'test -d ~/ci/projects/%s/work-slot1/node_modules; then echo warm; fi' % (project, project, project))
    deadline = min(time.monotonic() + 5, runner.get('deadline', time.monotonic() + 5))
    cancel = runner.get('cancel')
    try:
        with subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3',
                               runner['route'], command], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True) as child:
            try:
                while time.monotonic() < deadline and not (cancel and cancel.is_set()):
                    try:
                        stdout, _ = child.communicate(timeout=min(.02, max(.001, deadline - time.monotonic())))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                else:
                    return None
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait()
        if child.returncode:
            return None
        if setup:
            return {}
        lines = stdout.splitlines()
        status = json.loads(lines[0])
        status['warm'] = 'warm' in lines[1:]
        score(runner, status)  # Fail closed on malformed metrics.
        return status
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, TypeError, AttributeError, Infra):
        return None


def wake(runner):
    child = None
    try:
        child = subprocess.Popen(['/bin/bash', '-c', runner['wol']], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        if child.wait(timeout=max(.01, runner['deadline'] - time.monotonic())):
            raise Infra('runner asleep')
    except (OSError, subprocess.SubprocessError):
        raise Infra('runner asleep')
    finally:
        if child is not None:
            # A timed-out shell can leave children behind. Reap its whole group.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--runner')
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--job')
    parser.add_argument('--record', metavar='SHA', help='with --runner: that runner ran SHA')
    parser.add_argument('--lookup', metavar='SHA|latest', help='the runner that ran it, else scored')
    parser.add_argument('--state', default=str(Path.home() / '.local/state/remote-ci/pool'))
    args = parser.parse_args()
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*', args.project):
            raise Infra('invalid project')
        runners = registry(os.environ.get('CI_RUNNERS', ''), os.environ)
        if args.runner:
            runners = [r for r in runners if r['name'] == args.runner]
        if args.record:
            if not runners or not args.runner:
                raise Infra('record needs a registered --runner')
            record(Path(args.state), args.project, args.record, args.runner)
            return 0
        if args.lookup:
            if not re.fullmatch(r'latest|[0-9a-f]{7,64}', args.lookup):
                raise Infra('invalid lookup')
            # An explicit runner wins; otherwise the runner that holds the logs, without probing.
            name = None if args.runner else recorded(Path(args.state), args.project, args.lookup)
            match = [r for r in runners if r['name'] == name]
            if match:
                print(name, match[0]['route'], '-', 'last-check' if args.lookup == 'latest' else 'ran-sha')
                return 0
        needs = set(os.environ.get('REMOTE_CI_NEEDS', '').split())
        choose = lambda: select(runners, needs, lambda r: probe(r, args.project, args.setup), wake)
        if args.lookup:
            row = choose()
            print(row['name'], row['route'], '-', 'named' if args.runner else 'scored')
        elif args.job:
            row = assign(Path(args.state), args.job, choose)
            print(row['runner'], row['route'], row['generation'])
        else:
            row = choose()
            print(row['name'], row['route'], '-')
        return 0
    except (Infra, OSError, ValueError, KeyError, TypeError):
        # Never include a route, command, environment or arbitrary exception text.
        error = sys.exc_info()[1]
        print('remote-ci: ' + (str(error) if isinstance(error, Infra) else 'runner pool unavailable'), file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
