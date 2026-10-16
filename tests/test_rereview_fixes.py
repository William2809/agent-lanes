"""Crash and integration regressions from a second queue review."""
# Allow 60 s for fixture scheduling at host load 40-140; product deadlines stay separate.
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
import unittest
from workflow_fixture import ROOT, WorkflowFixture


class RereviewFixTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture()

    def tearDown(self):
        self.f.close()

    def prepare(self):
        self.f.mq('claim', 'a', 'a.txt')
        self.f.commit(self.f.lanes['a'], 'a.txt', 'a\n')
        self.f.mq('add', 'a')

    def wait_for(self, predicate):
        until = time.monotonic() + 60
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.05)
        self.fail('fixture condition did not arrive')

    def test_resume_caps_xhigh_but_preserves_override_and_lower_effort(self):
        for name, saved, env, expected in (
                ('capped', 'xhigh', {}, 'high'),
                ('allowed', 'xhigh', {'WK_ALLOW_XHIGH': '1'}, 'xhigh'),
                ('lower', 'low', {}, 'low')):
            with self.subTest(name=name):
                log = self.f.worker(name, 'a', 'danger-full-access')
                log.write_text(log.read_text().replace('reasoning effort: high', 'reasoning effort: ' + saved))
                self.f.wk(name, '-r', 'continue', env=env)
                self.f.wait_worker(name)
                self.assertEqual(self.f.calls()[-1]['effort'], expected)

    def test_resume_caps_header_changed_while_waiting_for_launch_lock(self):
        log = self.f.worker('writer', 'a', 'danger-full-access')
        lock = self.f.state / 'launch.lock'
        lock.mkdir(parents=True)
        (lock / 'pid').write_text(str(os.getpid()) + '\n')
        self.f.shim('sleep', '#!/bin/sh\ntouch "$FIXTURE_ROOT/lock-wait"\nexec /bin/sleep "$@"\n')
        proc = subprocess.Popen([str(ROOT / 'bin/wk'), 'writer', '-r', 'continue'],
                                cwd=self.f.repo, env=self.f.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for(lambda: (self.f.root / 'lock-wait').exists())
            log.write_text(log.read_text().replace('reasoning effort: high', 'reasoning effort: xhigh'))
            self.f.run('ctrash', str(lock))
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, out + err)
            self.f.wait_worker('writer')
            self.assertEqual(self.f.calls()[-1]['effort'], 'high')
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.communicate(timeout=60)

    def dead_fixer(self):
        self.prepare()
        log = self.f.worker('dead', 'a', 'danger-full-access')
        log.write_text(log.read_text().split('tokens used')[0])
        (self.f.state / 'queue').write_text('a\tbounced\t1\tdead\n')
        (self.f.state / 'bounce-head.a').write_text(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['a']) + '\n')

    def runner(self, env=None, *, output=subprocess.PIPE):
        return subprocess.Popen([str(ROOT / 'bin/mq'), 'run'], cwd=self.f.repo,
                                env={**self.f.env, **(env or {})}, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=output, text=True)

    def assert_guard_free(self):
        # Brief 3: a surviving fixer launcher must not keep the runner guard.
        probe = self.f.run('python3', '-c', """
import fcntl, os
fd = os.open('.git/mq-run.guard', os.O_RDWR)
try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError: raise SystemExit(1)
""", check=False)
        self.assertEqual(probe.returncode, 0, 'orphan launcher retained the runner guard')

    def pause_launch(self, *, after):
        self.f.shim('wk', f"""#!/usr/bin/env python3
import os, pathlib, subprocess, sys, time
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
intent = pathlib.Path(os.environ['WK_LAUNCH_INTENT'])
(root / 'wrapper.pid').write_text(str(os.getpid()))
if {after!r}:
    result = subprocess.run([{str(ROOT / 'bin/wk')!r}, *sys.argv[1:]])
else:
    (intent / 'pid').write_text(str(os.getpid()))
(root / 'boundary').touch()
while not (root / 'release').exists(): time.sleep(0.02)
if not {after!r}: result = subprocess.run([{str(ROOT / 'bin/wk')!r}, *sys.argv[1:]])
sys.exit(result.returncode)
""")

    def test_runner_crash_after_accepted_retry_does_not_launch_twice(self):
        self.dead_fixer()
        self.pause_launch(after=True)
        # The killed runner's output is unused; its launcher may outlive it.
        # Gap: the accepted worker and paused wrapper are fixture actors.
        proc = self.runner({'FAKE_WORKER_MODE': 'die'}, output=subprocess.DEVNULL)
        try:
            self.wait_for(lambda: (self.f.root / 'boundary').exists())
            intent = Path((self.f.state / 'launch.a').read_text().strip())
            self.assertEqual(intent.joinpath('meta').read_text().split('\t')[2], '1')
            self.assertEqual(self.f.queue()[0][3], 'dead')
            self.assertFalse((self.f.state / 'retry.a.1').exists())
            # Acceptance precedes worker execution. Keep the crash at the paused
            # publication boundary, but wait for the actor before counting it.
            self.f.wait_worker('a-mq1r')
            self.assertEqual(len(self.f.calls()), 1)
            os.kill(proc.pid, signal.SIGKILL)  # only this test's runner
            proc.wait(timeout=60)
            self.assert_guard_free()
            result = self.f.mq('run', env={'FAKE_WORKER_MODE': 'die'})
            self.assertIn('fixer died twice', result.stdout)
            self.assertEqual(len(self.f.calls()), 1)
            self.assertEqual(self.f.queue()[0][3], 'a-mq1r')
            self.assertTrue((self.f.state / 'retry.a.1').exists())
            self.assertFalse((self.f.state / 'launch.a').exists())
        finally:
            (self.f.root / 'release').touch()
            if proc.poll() is None: proc.terminate()
            proc.communicate(timeout=60)
        self.assertEqual(self.f.queue()[0][1], 'parked')

    def test_runner_crash_before_fork_keeps_unaccepted_retry_available(self):
        self.dead_fixer()
        self.pause_launch(after=False)
        # Brief 3: an orphan launcher can hold pipes during bounded state-lock recovery.
        # This test kills the runner and checks state; it does not consume its output.
        # Gap: wk is a paused fixture wrapper, not a production worker.
        proc = self.runner({'FAKE_WORKER_MODE': 'die'}, output=subprocess.DEVNULL)
        try:
            self.wait_for(lambda: (self.f.root / 'boundary').exists())
            wrapper = int((self.f.root / 'wrapper.pid').read_text())  # child started by this runner
            os.kill(proc.pid, signal.SIGKILL)
            os.kill(wrapper, signal.SIGKILL)
            proc.wait(timeout=60)
            proc.communicate(timeout=60)
            self.assert_guard_free()
            self.assertEqual(self.f.calls(), [])
            self.assertFalse((self.f.state / 'retry.a.1').exists())
            self.f.shim('wk.new', '#!/bin/sh\nexec ' + shlex.quote(str(ROOT / 'bin/wk')) + ' "$@"\n')
            (self.f.shims / 'wk.new').replace(self.f.shims / 'wk')
            result = self.f.mq('run', env={'FAKE_WORKER_MODE': 'die'})
            self.assertIn('fixer died twice', result.stdout)
            self.assertEqual(len(self.f.calls()), 1)
            self.assertTrue((self.f.state / 'retry.a.1').exists())
        finally:
            (self.f.root / 'release').touch()
            if proc.poll() is None: proc.terminate()
            proc.communicate(timeout=60)

    def test_failed_acceptance_publication_reconciles_without_duplicate_launch(self):
        self.dead_fixer()
        self.f.shim('mv', '#!/bin/sh\ncase "$2" in */mq/demo/queue) if [ -f "$FIXTURE_ROOT/calls.jsonl" ] && [ ! -f "$FIXTURE_ROOT/publication-failed" ]; then touch "$FIXTURE_ROOT/publication-failed"; exit 1; fi ;; esac\nexec /bin/mv "$@"\n')
        result = self.f.mq('run', env={'FAKE_WORKER_MODE': 'die'})
        self.assertTrue((self.f.root / 'publication-failed').exists())
        self.assertEqual(len(self.f.calls()), 1)
        self.assertIn('fixer died twice', result.stdout)
        self.assertEqual(self.f.queue()[0][1:4], ['parked', '1', 'a-mq1r'])
        self.assertEqual((self.f.state / 'events').read_text().count('\tfixer-retry\t'), 1)
        self.assertFalse((self.f.state / 'launch.a').exists())

    def test_cancelled_late_launch_cannot_start_a_worker(self):
        result = self.f.wk('late', '-d', 'a', '-t', 'fixture:bounce',
                           env={'WK_LAUNCH_INTENT': str(self.f.state / 'cancelled')}, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('repair launch cancelled', result.stderr)
        self.assertEqual(self.f.calls(), [])

    def test_deferred_only_runner_lands_when_capacity_frees(self):
        self.prepare()
        self.f.wk('busy', '-d', 'b', '-t', 'fixture:new', env={'FAKE_WORKER_MODE': 'hold'})
        proc = self.runner({'WK_MAX_WRITERS': '1', 'MQ_REQUEUE_COOLDOWN': '2', 'FAKE_WORKER_MODE': 'repair'})
        try:
            self.wait_for(lambda: (self.f.state / 'deferred.a').exists())
            time.sleep(0.1)
            self.assertIsNone(proc.poll())
            self.assertEqual(len(self.f.calls()), 1)  # no fixer during the cooldown
            busy = int((self.f.home / '.claude/state/logs/demo/busy.log.pid').read_text())
            os.kill(busy, signal.SIGTERM)  # worker launched above by this fixture
            # Capacity release starts a repair and landing; allow 180 s under load.
            out, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, out + err)
            self.assertIn('LANDED', out)
            self.assertEqual(len(self.f.calls()), 2)
            self.assertEqual(self.f.queue(), [])
        finally:
            if proc.poll() is None: proc.terminate()
            proc.communicate(timeout=60)

    def test_deferred_recheck_survives_runner_crash_and_restart(self):
        self.prepare()
        self.f.wk('busy', '-d', 'b', '-t', 'fixture:new', env={'FAKE_WORKER_MODE': 'hold'})
        first = self.runner({'WK_MAX_WRITERS': '1', 'MQ_REQUEUE_COOLDOWN': '300', 'FAKE_WORKER_MODE': 'repair'})
        second = None
        try:
            self.wait_for(lambda: (self.f.state / 'deferred.a').exists() and not (self.f.state / 'launch.a').exists())
            self.assertIsNone(first.poll())
            os.kill(first.pid, signal.SIGKILL)
            first.wait(timeout=60); first.communicate(timeout=60)
            second = self.runner({'WK_MAX_WRITERS': '1', 'MQ_REQUEUE_COOLDOWN': '2', 'FAKE_WORKER_MODE': 'repair'})
            time.sleep(0.2)
            self.assertIsNone(second.poll())
            busy = int((self.f.home / '.claude/state/logs/demo/busy.log.pid').read_text())
            os.kill(busy, signal.SIGTERM)
            out, err = second.communicate(timeout=20)
            self.assertEqual(second.returncode, 0, out + err)
            self.assertIn('LANDED', out)
            self.assertEqual(self.f.queue(), [])
            self.assertEqual(len(self.f.calls()), 2)
        finally:
            for proc in (first, second):
                if proc is not None:
                    if proc.poll() is None: proc.terminate()
                    proc.communicate(timeout=60)


if __name__ == '__main__':
    unittest.main()
