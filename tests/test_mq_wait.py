"""mq-wait reads the queue run log: solo, train, and park-before-landing attempts."""
# Allow 60 s for fixture scheduling at host load 40-140; product deadlines stay separate.
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from workflow_fixture import ROOT


class MqWaitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='mq-wait-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.log = self.home / '.claude/state/mq/demo/run.log'
        self.log.parent.mkdir(parents=True)

    def wait(self, name, text):
        self.log.write_text(text)
        return subprocess.run(['sh', str(ROOT / 'bin/mq-wait'), name, 'demo'], capture_output=True, text=True,
                              env={**os.environ, 'HOME': str(self.home)}, timeout=30)

    def test_train_landing_ends_a_members_wait(self):
        result = self.wait('b', 'landing b\nBOUNCED b to b: check rc=1\nlanding train: a b c\n'
                                'train a b c: LANDED 1234567 on main\nmq done: landed 3; 0 parked\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('train a b c: LANDED 1234567', result.stdout)

    def test_a_longer_name_in_a_train_is_not_this_lane(self):
        result = self.wait('b', 'landing b\nb: LANDED 1111111 on main\nlanding train: a bb\n'
                                'train a bb: LANDED 2222222 on main\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('2222222', result.stdout)

    def test_park_before_any_landing_ends_the_wait(self):
        result = self.wait('a', 'PARKED a: check policy changed: review (ci.yml); then mq add a\n'
                                'mq done: landed 0; 1 parked a\n')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('PARKED a: check policy changed', result.stdout)


    def waiting(self, name, text):
        self.log.write_text(text)
        # Signal only after the real waiter has scanned its initial state and reached
        # its first poll sleep. A fixed startup delay races loaded hosts.
        hooks = self.home / 'python-hooks'
        hooks.mkdir()
        ready = self.home / 'wait.ready'
        (hooks / 'sitecustomize.py').write_text(
            'import os, pathlib, time\n'
            'original_sleep = time.sleep\n'
            'def observed_sleep(delay):\n'
            '    pathlib.Path(os.environ["MQ_WAIT_READY"]).touch()\n'
            '    original_sleep(delay)\n'
            'time.sleep = observed_sleep\n')
        proc = subprocess.Popen(['sh', str(ROOT / 'bin/mq-wait'), name, 'demo'], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env={**os.environ, 'HOME': str(self.home), 'MQ_WAIT_POLL': '1',
                                     'PYTHONPATH': str(hooks), 'MQ_WAIT_READY': str(ready)})
        def close():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=60)
        self.addCleanup(close)
        until = time.monotonic() + 60
        while not ready.exists() and proc.poll() is None:
            self.assertLess(time.monotonic(), until, 'waiter did not reach its first poll')
            time.sleep(.02)
        return proc

    def append(self, text):
        with self.log.open('a') as f:
            f.write(text)

    def test_retry_that_started_since_the_last_poll_replaces_the_bounce(self):
        proc = self.waiting('b', 'mq done: landed 1; 0 parked\nlanding b\n')
        self.append('BOUNCED b to b: check rc=1\nlanding b\n')
        time.sleep(2.5)
        self.assertIsNone(proc.poll())
        self.append('b: LANDED 3333333 on main\n')
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn('b: LANDED 3333333', out)

    def test_run_that_ends_before_the_lane_starts_ends_the_wait(self):
        proc = self.waiting('b', 'mq done: landed 0; 0 parked\nlanding a\n')
        time.sleep(1.5)
        self.assertIsNone(proc.poll())
        self.append('mq: stopping, infra runner unreachable rc=3; lanes stay queued\nmq done: landed 0; 0 parked\n')
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn('mq done:', out)

    def test_legacy_ready_queue_does_not_return_its_old_success(self):
        (self.log.parent / 'queue').write_text('b\tready\t0\t\n')
        proc = self.waiting('b', 'landing b\nb: LANDED aaaaaaa on main\nmq done: landed 1; 0 parked\n')
        self.assertIsNone(proc.poll())
        self.append('landing b\nb: LANDED bbbbbbb on main\n')
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, out)
        self.assertNotIn('aaaaaaa', out)

    def test_legacy_parked_queue_is_terminal_without_a_new_log_line(self):
        (self.log.parent / 'queue').write_text('b\tparked\t0\t\n')
        result = self.wait('b', 'landing b\nb: LANDED aaaaaaa on main\n')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('PARKED b', result.stdout)
        self.assertNotIn('aaaaaaa', result.stdout)

    def test_printed_lines_skip_longer_names(self):
        result = self.wait('b', 'landing b\nPARKED bb: x\nBOUNCED bbb to k: y\nb: LANDED 4444444 on main\n')
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('bb', result.stdout)


if __name__ == '__main__':
    unittest.main()
