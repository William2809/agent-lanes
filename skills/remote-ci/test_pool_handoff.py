"""Brief requirements: a pool check still queued on one runner moves once to a ready runner.
Synthetic ledgers and fake runner hosts behind an ssh stub; no real check suite runs.
"""
import copy
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock

KIT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(KIT / 'remote'))
import resources as r
import scheduler as s
import scheduler_withdraw as w
from test_pool import module

LIMITS = dict(MAX_JOBS=2, MAX_WORKERS=4, CPU_RESERVE=2, MEM_RESERVE_MB=2048, WORKER_MB=2048)
METRICS = dict(cores=10, load=0, available_mb=16000, total_mb=24000, pressure=1, swap_mb=0)
PROCS = {10: dict(start='birth', group=10, rss=0)}


def request(token, **extra):
    row = dict(project=token, lane='lane', sha='a' * 40, tree=token, mode='run',
               command_digest='b' * 64, slots=1, workers=1, memory_mb=2048, leases=[], generation=1)
    row.update(extra)
    return row


class Withdraw(unittest.TestCase):
    def setUp(self):
        self.data = dict(sequence=0, jobs={})

    def submit(self, token, **extra):
        s.submit(self.data, token, 10, 'birth', 100, 1100, request(token, **extra))
        return self.data['jobs'][token]

    def tick(self, now=100):
        s.schedule(self.data, METRICS, LIMITS, PROCS, now)

    def test_queued_row_ends_moved_and_is_never_granted(self):
        self.submit('holder', project='p')
        self.tick()
        row = self.submit('queued', project='p')
        self.tick()
        self.assertEqual(row['state'], 'queued')
        w.withdraw(self.data, 'queued', row['lease_generation'], 'pc', 101)
        self.tick(102)
        s.complete(self.data, 'holder', 0, 103)
        self.tick(104)
        self.assertEqual((row['state'], row['result'], row['moved_to']), ('finished', 79, 'pc'))
        self.assertFalse(any(row.get(key) for key in w.GRANT))

    def test_grant_first_refuses_withdraw_and_keeps_the_grant(self):
        row = self.submit('job')
        self.tick()
        before = copy.deepcopy(row)
        with self.assertRaises(ValueError):
            w.withdraw(self.data, 'job', row['lease_generation'], 'pc', 101)
        self.assertEqual(row, before)
        self.assertEqual(row['state'], 'running')

    def test_withdraw_first_wins_over_a_later_grant(self):
        row = self.submit('job')
        w.withdraw(self.data, 'job', row['lease_generation'], 'pc', 100)
        self.tick(101)
        self.assertEqual((row['state'], row['result']), ('finished', 79))
        self.assertNotIn('workers', row)

    def test_racing_grant_and_withdraw_on_the_ledger_lock(self):
        """Both orders through real Queue transactions: exactly one side wins."""
        outcomes = set()
        for attempt in range(12):
            with tempfile.TemporaryDirectory() as temp:
                queue = r.Queue(temp)
                with queue.transaction() as data:
                    data.update(copy.deepcopy(self.data))
                    s.submit(data, 'job', 10, 'birth', 100, 1100, request('job'))
                    generation = data['jobs']['job']['lease_generation']
                start = threading.Barrier(2)
                result = {}

                def grant():
                    start.wait()
                    with queue.transaction() as data:
                        s.schedule(data, METRICS, LIMITS, PROCS, 101)

                def withdraw():
                    start.wait()
                    try:
                        with queue.transaction() as data:
                            w.withdraw(data, 'job', generation, 'pc', 101)
                        result['moved'] = True
                    except ValueError:
                        result['moved'] = False
                threads = [threading.Thread(target=grant), threading.Thread(target=withdraw)]
                if attempt % 2:
                    threads.reverse()
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(60)
                with queue.transaction() as data:
                    row = data['jobs']['job']
                if result['moved']:
                    self.assertEqual((row['state'], row['result']), ('finished', 79))
                    self.assertNotIn('workers', row)
                else:
                    self.assertEqual(row['state'], 'running')
                    self.assertNotIn('moved_to', row)
                outcomes.add(result['moved'])
        self.assertTrue(outcomes)

    def test_refusals_leave_the_row_unchanged(self):
        self.submit('leader', project='p')
        follower = self.submit('follower', project='p', tree='leader', generation=2)
        self.assertEqual(follower['follow'], 'leader')
        cases = {
            'leader': lambda row: None,  # Followers would inherit 79 without moving.
            'stale': lambda row: row.update(lease_generation=row['lease_generation'] + 1),
            'cancelled': lambda row: row.update(cancel=True),
            'legacy': lambda row: row.pop('version'),
            'recovery': lambda row: row.update(recovery_of='old'),
            'touched': lambda row: row.update(touched=True),
        }
        for name, change in cases.items():
            with self.subTest(name):
                row = self.data['jobs']['leader'] if name == 'leader' else self.submit(name)
                generation = row['lease_generation']
                change(row)
                before = copy.deepcopy(self.data)
                with self.assertRaises(ValueError):
                    w.withdraw(self.data, name, generation, 'pc', 101)
                self.assertEqual(self.data, before)
        # A follower gets its leader's result for free: moving it would only duplicate work.
        before = copy.deepcopy(self.data)
        with self.assertRaises(ValueError):
            w.withdraw(self.data, 'follower', follower['lease_generation'], 'pc', 101)
        self.assertEqual(self.data, before)

    def cli(self, *argv, version='v2'):
        stderr = io.StringIO()
        with mock.patch.object(r, 'scheduler_version', return_value=version), \
                mock.patch.object(sys, 'argv', ['resources.py', *argv]), redirect_stderr(stderr):
            return r.main(), stderr.getvalue()

    def test_cli_withdraw_needs_v2_and_reports_refusal(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, HOME=temp):
            queue = r.Queue()
            with queue.transaction() as data:
                s.submit(data, 'job', 10, 'birth', 100, 1100, request('job'))
                generation = data['jobs']['job']['lease_generation']
            args = ('withdraw', '--token', 'job', '--generation', str(generation), '--to', 'pc')
            code, err = self.cli(*args, version='v1')
            self.assertEqual(code, 1, err)
            self.assertIn('withdraw refused', err)
            code, err = self.cli('withdraw', '--token', 'job', '--generation', str(generation), '--to', 'pc;x')
            self.assertEqual(code, 1, err)
            with queue.transaction() as data:
                self.assertEqual(data['jobs']['job']['state'], 'queued')
            self.assertEqual(self.cli(*args), (0, ''))
            code, err = self.cli(*args)  # One withdraw only: the row is terminal now.
            self.assertEqual(code, 1)
            with queue.transaction() as data:
                self.assertEqual((data['jobs']['job']['result'], data['jobs']['job']['moved_to']), (79, 'pc'))

    def test_withdrawn_acquire_names_the_destination_and_returns_79(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, HOME=temp):
            queue = r.Queue()
            with queue.transaction() as data:
                data['jobs']['job'] = dict(version=2, state='finished', result=79, moved_to='pc')
            path = Path(temp) / 'request.json'
            path.write_text('{}')
            with mock.patch.object(r, 'local_allowed'), mock.patch.object(r, 'policy', return_value=LIMITS), \
                    mock.patch.object(r, 'acquire_v2', return_value=dict(result=79, state='finished')):
                code, err = self.cli('acquire', '--token', 'job', '--owner', '1', '--deadline', '9',
                                     '--request-file', str(path))
            self.assertEqual(code, 79)
            self.assertEqual(err.strip(), '== moved to pc')

    def test_moved_submission_keeps_its_first_submission_time(self):
        fakes = {
            'scheduler_process': types.SimpleNamespace(
                reconcile=lambda queue: None,
                snapshot=lambda: {os.getpid(): dict(start='birth', birth='b', group=1, rss=0)}),
            'scheduler_maintenance': types.SimpleNamespace(start_recovery=lambda queue: None),
            'scheduler_legacy': types.SimpleNamespace(import_cleanup=lambda data, root, now: None,
                                                      blocker=lambda root, procs: None),
            'scheduler_events': types.SimpleNamespace(emit_transition=lambda *args: None),
        }
        for submitted, expected in ((900, 900), (5000, 1000), (None, 1000)):
            with self.subTest(submitted=submitted), tempfile.TemporaryDirectory() as temp, \
                    mock.patch.dict(os.environ, HOME=temp), mock.patch.dict(sys.modules, fakes), \
                    mock.patch.object(r, 'scheduler_sample', return_value=METRICS), \
                    mock.patch.object(r.time, 'time', return_value=1000):
                queue = r.Queue()
                outcome = r.acquire_v2(queue, 'job', os.getpid(), 2000, LIMITS, request('job'),
                                       submitted=submitted)
                self.assertIn('workers', outcome)
                with queue.transaction() as data:
                    self.assertEqual(data['jobs']['job']['submitted_at'], expected)


class Mover(unittest.TestCase):
    def setUp(self):
        self.move = module('pool_move', KIT / 'bin/pool_move.py')
        self.pool = self.move.pool
        self.runners = self.pool.registry('mini=mini-route:unit pc=pc-route:unit bare=bare-route', {})
        self.temp = tempfile.TemporaryDirectory()
        self.out = str(Path(self.temp.name) / 'move')
        self.calls = []
        self.sleeps = []

    def tearDown(self):
        self.temp.cleanup()

    def status(self, **row):
        jobs = {'proj-run1-42': dict(dict(version=2, state='queued', run_id='run1', lease_generation=5), **row)}
        return json.dumps(dict(active=1, waiting=1, jobs=jobs))

    def remote(self, statuses, withdraw_rc=0):
        statuses = list(statuses)

        def call(route, command):
            self.calls.append((route, command))
            if ' withdraw ' in command:
                return types.SimpleNamespace(returncode=withdraw_rc, stdout='')
            return types.SimpleNamespace(returncode=0, stdout=statuses.pop(0) if len(statuses) > 1 else statuses[0])
        return call

    def probe(self, answers):
        answers = {name: list(values) for name, values in answers.items()}

        def call(runner, project):
            self.calls.append((runner['name'], 'probe'))
            values = answers[runner['name']]
            ready = values.pop(0) if len(values) > 1 else values[0]
            return dict(active=0, waiting=0, load=0, cores=4, pressure=1,
                        policy={'MAX_JOBS': 1}, can_admit=ready) if ready is not None else None
        return call

    def run_move(self, statuses, answers, withdraw_rc=0, loops=6):
        left = [loops]

        def alive():
            left[0] -= 1
            return left[0] >= 0
        return self.move.move(self.runners, 'mini', {'unit'}, 'proj', 'run1', 60, 5, self.out,
                              probe=self.probe(answers), remote=self.remote(statuses, withdraw_rc),
                              sleep=self.sleeps.append, alive=alive)

    def withdraws(self):
        return [command for _, command in self.calls if ' withdraw ' in command]

    def test_row_states(self):
        queued = self.move.queued
        self.assertEqual(queued(json.loads(self.status()), 'run1'), ('proj-run1-42', 5))
        # A follower gets its identical leader's result for free: moving it would only duplicate work.
        self.assertIs(queued(json.loads(self.status(state='following', follow='x')), 'run1'), False)
        self.assertIsNone(queued(json.loads(self.status()), 'other'))
        for row in (dict(state='running'), dict(workers=1), dict(slot=1), dict(process={'group': 1}),
                    dict(state='finished'), dict(version=None), dict(cancel=True)):
            with self.subTest(row=row):
                self.assertIs(queued(json.loads(self.status(**row)), 'run1'), False)

    def test_moves_after_two_ready_probes_with_one_withdraw(self):
        self.assertEqual(self.run_move([self.status()], {'pc': [True]}), 'moved')
        self.assertEqual(Path(self.out).read_text().strip(), 'pc')
        self.assertEqual(self.withdraws(), ['python3 ~/ci/projects/proj/runs/run1/bin/resources.py '
                                            'withdraw --token proj-run1-42 --generation 5 --to pc'])
        self.assertEqual(self.sleeps[:2], [60, 5])
        self.assertEqual([c for c in self.calls if c[1] == 'probe'], [('pc', 'probe')] * 2)
        # The status read and the withdraw go to the source; the needs-less runner is never probed.
        self.assertTrue(all(route == 'mini-route' for route, command in self.calls if command != 'probe'))

    def test_one_flap_does_not_move(self):
        result = self.run_move([self.status()], {'pc': [True, False, False]}, loops=3)
        self.assertEqual(result, 'none')
        self.assertEqual(self.withdraws(), [])
        self.assertFalse(Path(self.out).exists())

    def test_started_or_unready_destination_never_withdraws(self):
        self.assertEqual(self.run_move([self.status(state='running', workers=1)], {'pc': [True]}), 'started')
        self.assertNotIn(('pc', 'probe'), self.calls)
        self.assertEqual(self.run_move([self.status()], {'pc': [None]}, loops=3), 'none')
        self.assertEqual(self.withdraws(), [])

    def test_refused_withdraw_is_the_only_attempt(self):
        self.assertEqual(self.run_move([self.status()], {'pc': [True]}, withdraw_rc=1), 'kept')
        self.assertEqual(len(self.withdraws()), 1)

    def test_no_other_eligible_runner_means_no_work(self):
        runners = self.pool.registry('mini=mini-route:unit pc=pc-route:unit', {'CI_RUNNER_pc_ENABLED': '0'})
        result = self.move.move(runners, 'mini', {'unit'}, 'proj', 'run1', 60, 5, self.out,
                                probe=lambda *a: self.fail('probe'), remote=lambda *a: self.fail('ssh'),
                                sleep=lambda s: self.fail('sleep'))
        self.assertEqual(result, 'none')


FAKE_RUN = r'''#!/bin/bash
# A real SSH server launches on another host, outside the client transport group.
if [ -z "${FAKE_RUN_DETACHED:-}" ]; then
  FAKE_RUN_DETACHED=1 python3 -c 'import subprocess,sys; subprocess.Popen(["/bin/bash", *sys.argv[1:]], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)' "$0" "$@"
  exit $?
fi
p=$HOME/ci/projects/$1 run=$4
printf '%s\n' "$*" >>"$HOME/run.args"
cp "$p/runs/$run/conf" "$HOME/run.conf"
log=$p/logs/$run.log
mkdir -p "$p/logs" "$p/results"
echo "== resources queued scheduler=v2" >"$log"
rc=0
if [ -f "$HOME/hold" ]; then
  printf '{"%s-%s-%s": {"version": 2, "state": "queued", "run_id": "%s", "lease_generation": 5}}\n' \
    "$1" "$run" "$$" "$run" >"$HOME/jobs.json"
  end=$(( $(date +%s) + $(cat "$HOME/hold") ))
  while [ "$(date +%s)" -lt "$end" ]; do
    if [ -f "$HOME/withdrawn" ]; then echo "== moved to $(cat "$HOME/withdrawn")" >>"$log"; rc=79; break; fi
    [ ! -f "$HOME/attempted" ] || break
    sleep 0.2
  done
fi
echo "== finished rc=$rc" >>"$log"
echo "$rc" >"$p/results/$run.exit"
'''

FAKE_RESOURCES = r'''import json, sys
from pathlib import Path
home = Path.home()
if sys.argv[1] == 'status':
    counter = home / 'status-calls'
    calls = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(calls + 1))
    jobs = json.loads((home / 'jobs.json').read_text()) if (home / 'jobs.json').exists() else {}
    print(json.dumps(dict(active=0, waiting=0, load=0, cores=4, pressure=1, policy={'MAX_JOBS': 1},
                          can_admit=calls >= int((home / 'ready-after').read_text()), jobs=jobs)))
elif sys.argv[1] == 'withdraw':
    with (home / 'withdraw.args').open('a') as stream:
        stream.write(' '.join(sys.argv[2:]) + '\n')
    if (home / 'withdraw-unsupported').exists():
        (home / 'attempted').touch()
        sys.exit(2)
    (home / 'withdrawn').write_text(sys.argv[sys.argv.index('--to') + 1])
'''

SSH = r'''#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac; done
home=$FAKE_HOSTS/$1; shift
[ -d "$home" ] || exit 255
command="$*"
command=${command/#git-receive-pack /git receive-pack }
cd "$home" && HOME=$home exec /bin/bash -c "$command"
'''


class Client(unittest.TestCase):
    """remote-ci check against two fake hosts: a (source) and b (destination)."""

    def host(self, name, scheduler, ready_after, hold=None):
        home = self.hosts / (name + '-route')
        project = home / 'ci/projects/fixture'
        for folder in ('ci/kit', 'ci/bin', '.config/agent-lanes', 'ci/projects/fixture/logs',
                       'ci/projects/fixture/results'):
            (home / folder).mkdir(parents=True, exist_ok=True)
        (home / '.config/agent-lanes/config').write_text('AGENT_CI_SCHEDULER=%s\n' % scheduler)
        (home / 'ci/kit/VERSION').write_text('fake1\n')
        (home / 'ci/kit/run.sh').write_text(FAKE_RUN)
        for path in (home / 'ci/kit/resources.py', home / 'ci/bin/resources.py'):
            path.write_text(FAKE_RESOURCES)
        (home / 'ready-after').write_text(str(ready_after))
        if hold is not None:
            (home / 'hold').write_text(str(hold))
        (project / '.ready').touch()
        subprocess.run(['git', 'init', '-q', '--bare', str(project / 'repo.git')], check=True)
        subprocess.run(['git', '-C', str(project / 'repo.git'), 'config', 'receive.advertisePushOptions', 'true'],
                       check=True)
        hook = project / 'repo.git/hooks/pre-receive'
        hook.write_text('#!/bin/sh\ncat >/dev/null\n'
                        'if [ -f "$FAKE_HOSTS/a-route/withdrawn" ]; then echo after; else echo before; fi '
                        '>>"$HOME/pushes"\n')
        hook.chmod(0o755)
        return home

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.hosts = root / 'hosts'
        self.repo = root / 'client'
        self.repo.mkdir()
        tools = root / 'tools'
        tools.mkdir()
        (tools / 'ssh').write_text(SSH)
        (tools / 'ssh').chmod(0o755)
        (root / 'config').write_text('CI_RUNNERS="a=a-route:unit b=b-route:unit"\n')
        self.conf = 'REMOTE_CI_NAME=fixture\nREMOTE_CI_NEEDS=unit\nREMOTE_CI_CHECK="echo full"\n'
        (self.repo / '.remote-ci.conf').write_text(self.conf)
        self.env = dict(os.environ, HOME=str(root), AGENT_LANES_CONFIG=str(root / 'absent'),
                        PATH=str(tools) + os.pathsep + os.environ['PATH'], FAKE_HOSTS=str(self.hosts),
                        REMOTE_CI_MOVE_AFTER='0', REMOTE_CI_MOVE_GAP='0.2',
                        GIT_AUTHOR_NAME='F', GIT_AUTHOR_EMAIL='user@example.invalid',
                        GIT_COMMITTER_NAME='F', GIT_COMMITTER_EMAIL='user@example.invalid')
        self.env['AGENT_LANES_CONFIG'] = str(root / 'config')
        for name in ('REMOTE_CI_RUNNER', 'REMOTE_CI_NEEDS', 'CI_RUNNERS', 'REMOTE_CI_MOVE', 'REMOTE_CI_CONF'):
            self.env.pop(name, None)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'add', '.remote-ci.conf'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-qm', 'fixture'], check=True, env=self.env)
        self.state = root / '.local/state/remote-ci/pool'

    def tearDown(self):
        self.temp.cleanup()

    def check(self, *args, **env):
        return subprocess.run(['/bin/bash', str(KIT / 'bin/remote-ci'), 'check', *args],
                              env=dict(self.env, **env), cwd=self.repo, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)

    def read(self, host, name):
        path = self.hosts / (host + '-route') / name
        return path.read_text() if path.exists() else ''

    def test_queued_run_moves_once_with_the_same_snapshot(self):
        a = self.host('a', 'v2', 0, hold=60)
        b = self.host('b', 'v2', 1)
        (self.repo / 'dirty.txt').write_text('uncommitted\n')
        self.started = int(time.time())
        result = self.check('--worktree', '--summary', '--force', '--run', 'echo targeted')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        moved = re.search(r'remote-ci: moved (\S+) from a to b after (\d+)s queued', output)
        self.assertTrue(moved, output)
        self.assertIn('== moved to b', (a / 'ci/projects/fixture/logs' / (moved[1] + '.log')).read_text())
        self.assertRegex(self.read('a', 'withdraw.args'),
                         r'^--token fixture-%s-\d+ --generation 5 --to b\n$' % re.escape(moved[1]))
        # Nothing reached the destination before the source acknowledged the withdraw.
        self.assertEqual(self.read('b', 'pushes'), 'after\n')
        first, = self.read('a', 'run.args').splitlines()
        second, = self.read('b', 'run.args').splitlines()
        first, second = first.split(' '), second.split(' ')
        # Same sha, command and --force; the first submission time travels as argument 9.
        self.assertEqual((first[1], first[4], first[5]), (second[1], second[4], second[5]))
        self.assertEqual(first[5], '1')
        self.assertEqual(first[8], second[8])
        launched = (a / 'run.args').stat().st_mtime
        self.assertTrue(self.started - 1 <= int(second[8]) <= launched, (self.started, second[8], launched))
        self.assertEqual(self.read('a', 'run.conf'), self.conf)
        self.assertEqual(self.read('b', 'run.conf'), self.conf)
        first_meta, = (a / 'ci/projects/fixture/runs').glob('*/submission.json')
        second_meta, = (b / 'ci/projects/fixture/runs').glob('*/submission.json')
        self.assertEqual(json.loads(first_meta.read_text()), json.loads(second_meta.read_text()))
        snapshot = subprocess.run(['git', '-C', str(self.repo), 'show', first[1] + ':dirty.txt'],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(snapshot.stdout, 'uncommitted\n')
        pool = module('pool', KIT / 'bin/pool.py')
        self.assertEqual(pool.recorded(self.state, 'fixture', 'latest'), 'b')
        self.assertEqual(pool.recorded(self.state, 'fixture', first[1]), 'b')

    def test_queued_v2_source_moves_to_v1_destination(self):
        self.host('a', 'v2', 0, hold=60)
        self.host('b', 'v1', 1)
        result = self.check('--run', 'echo targeted')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('from a to b', result.stdout)
        self.assertEqual(self.read('b', 'pushes'), 'after\n')
        first = self.read('a', 'run.args').split(' ')
        second = self.read('b', 'run.args').split(' ')
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[8], second[8])

    def test_no_move_from_v1_source_or_when_disabled(self):
        for scheduler, env in (('v1', {}), ('v2', {'REMOTE_CI_MOVE': '0'})):
            with self.subTest(scheduler=scheduler, env=env):
                self.hosts = Path(tempfile.mkdtemp(dir=self.temp.name))
                self.env['FAKE_HOSTS'] = str(self.hosts)
                self.host('a', scheduler, 0, hold=3)
                self.host('b', 'v1', 1)  # Ready from its second probe: only a mover would use it.
                result = self.check('--run', 'echo targeted', **env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('moved', result.stdout)
                self.assertEqual(self.read('a', 'withdraw.args'), '')
                self.assertEqual(self.read('b', 'run.args'), '')

    def test_unsupported_withdraw_keeps_waiting_on_the_source(self):
        a = self.host('a', 'v2', 0, hold=60)
        (a / 'withdraw-unsupported').touch()
        self.host('b', 'v1', 1)
        result = self.check('--summary', '--run', 'echo targeted')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('moved', result.stdout)
        self.assertEqual(len(self.read('a', 'withdraw.args').splitlines()), 1)
        self.assertEqual(self.read('b', 'run.args'), '')

    def test_hung_push_ends_at_the_submission_deadline(self):
        a = self.host('a', 'v2', 0)
        hook = a / 'ci/projects/fixture/repo.git/hooks/pre-receive'
        hook.write_text('#!/bin/sh\ncat >/dev/null\nsleep 40 & child=$!; echo "$child" >"$HOME/pipe-child"; wait "$child"\n')
        start = time.monotonic()
        # A PIPE is intentional: descendants must not keep the agent's read blocked.
        result = subprocess.run(['/bin/bash', str(KIT / 'bin/remote-ci'), 'check', '--run', 'echo targeted'],
                                env=dict(self.env, REMOTE_CI_RUNNER='a', REMOTE_CI_WAIT_TIMEOUT='8'),
                                cwd=self.repo, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=60)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 74, output)
        self.assertLess(time.monotonic() - start, 20, output)
        self.assertEqual(self.read('a', 'run.args'), '')


if __name__ == '__main__':
    unittest.main()
