"""Regression tests for a queue review (batch 1): land target/tree checks,
land's raw check tail, and mq's transient retry of a whole train."""
import subprocess
import sys
import unittest
from workflow_fixture import ROOT, WorkflowFixture
import test_bounce_train as bt

LANES = bt.LANES

LAND = ROOT / 'skills/wt-dev/bin/land'


class LandTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(('a',))
        self.addCleanup(self.f.close)
        self.f.shim('migcheck', '#!/bin/sh\nexit 0\n')
        self.f.commit(self.f.lanes['a'], 'a.txt', 'a\n')
        self.lane_head = self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['a'])

    def land(self, check, **env):
        self.f.env['LAND_CHECK'] = check
        return subprocess.run(['sh', str(LAND), 'a', '--keep'], cwd=self.f.repo, env={**self.f.env, **env},
                              capture_output=True, text=True, timeout=60)

    def test_target_switched_during_check_fails(self):
        before = self.f.git('rev-parse', 'main')
        self.f.git('branch', 'other')
        r = self.land('git -C "$FIXTURE_ROOT/demo" switch -q other')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAILED target moved', r.stdout)
        self.assertEqual(self.f.git('rev-parse', 'main'), before)
        self.assertEqual(self.f.git('rev-parse', 'other'), before)

    def test_land_refuses_a_branch_other_than_the_queue_target(self):
        self.f.git('switch', '-q', '-c', 'other')
        r = self.land('true', LAND_TARGET='refs/heads/main')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAILED target moved', r.stdout)
        self.assertNotIn('stacked', r.stdout)

    def test_detached_main_checkout_is_a_moved_target_for_the_queue(self):
        self.f.git('switch', '-q', '--detach')
        r = self.land('true', LAND_TARGET='refs/heads/main')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAILED target moved', r.stdout)
        r = self.land('true')
        self.assertIn('FAILED main checkout is not on a branch', r.stdout)

    def test_check_editing_a_tracked_file_fails(self):
        before = self.f.git('rev-parse', 'main')
        r = self.land('printf edited > base.txt')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('base.txt', r.stdout)
        self.assertIn('FAILED check changed tracked files', r.stdout)
        self.assertEqual(self.f.git('rev-parse', 'main'), before)
        self.assertEqual((self.f.lanes['a'] / 'base.txt').read_text(), 'base\n')
        self.assertEqual(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['a']), self.lane_head)

    def test_check_committing_fails(self):
        r = self.land('git commit -q --allow-empty -m sneaky')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAILED check changed tracked files', r.stdout)
        self.assertEqual(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['a']), self.lane_head)

    def test_untracked_check_output_still_lands(self):
        r = self.land('mkdir -p coverage && echo x > coverage/out.txt')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('LANDED', r.stdout)

    def test_raw_check_tail_keeps_markers_the_summary_drops(self):
        tail = self.f.root / 'land.tail'
        for marker in ('upload to R2: ECONNRESET', 'S3 503 SlowDown'):
            with self.subTest(marker=marker):
                r = self.land(f"echo '{marker}'; exit 1", LAND_TAIL=str(tail))
                self.assertEqual(r.returncode, 1)
                self.assertNotIn(marker, r.stdout)
                self.assertEqual(tail.read_text(), marker + '\n')
        # A later green land never leaves a stale tail behind.
        r = self.land('true', LAND_TAIL=str(tail))
        self.assertIn('LANDED', r.stdout)
        self.assertEqual(tail.read_text(), '')

    def test_raw_check_tail_is_bounded(self):
        tail = self.f.root / 'land.tail'
        r = self.land("i=0; while [ $i -lt 500 ]; do printf '%0400d\\n' $i; i=$((i+1)); done; exit 1",
                      LAND_TAIL=str(tail))
        self.assertEqual(r.returncode, 1)
        lines = tail.read_text().splitlines()
        self.assertLessEqual(len(lines), 80)
        self.assertTrue(all(len(line) <= 300 for line in lines))


class TrainTransientTests(unittest.TestCase):
    """One transient storage fault in a 4-lane train: retry the same train once, not 4 solo lands."""
    setUp = bt.BounceTrainTests.setUp
    run_train = bt.BounceTrainTests.run_train
    configure_fault = bt.TransientRetryTests.configure_fault
    assert_retry_count = bt.TransientRetryTests.assert_retry_count

    def test_train_transient_retries_the_same_train_once(self):
        self.configure_fault('R2 InternalError', failures=1)
        self.run_train()
        self.assertEqual(self.calls, [' '.join(LANES)] * 2)
        self.assertEqual(self.output.count('TRANSIENT train:'), 1)
        self.assertEqual(sum(row[2] == 'transient-retry' for row in self.events), 1)
        self.assertFalse(any(row[2] in ('train-failed', 'train-red') for row in self.events))
        self.assertEqual(self.f.queue(), [])

    def test_train_transient_twice_splits_as_before(self):
        self.configure_fault('R2 InternalError', failures=2)
        self.run_train()
        self.assertEqual(self.calls, [' '.join(LANES)] * 2 + list(LANES))
        self.assertEqual(self.output.count('TRANSIENT train:'), 1)
        self.assertIn('mq: train of 4 red', self.output)
        self.assertEqual(self.f.queue(), [])

    def test_local_only_marker_retries_train(self):
        # The fault exists only in the local check output; remote diagnostics are unavailable.
        self.configure_fault('noise', failures=1)
        self.f.shim('remote-ci', '#!/bin/sh\nexit 1\n')
        check = self.f.root / 'check.py'
        check.write_text(check.read_text().replace("print('FAIL invoice browser fixture')",
                                                  "print('upload to R2 failed: ECONNRESET')"))
        self.run_train()
        self.assertEqual(self.calls, [' '.join(LANES)] * 2)
        self.assertEqual(self.output.count('TRANSIENT train:'), 1)

    def test_long_local_tail_does_not_hide_the_remote_marker(self):
        # Remote diagnostics hold the marker; land's local tail is 80 ordinary lines.
        self.configure_fault('R2 InternalError', failures=1)
        check = self.f.root / 'check.py'
        check.write_text(check.read_text().replace("print('FAIL invoice browser fixture')",
                                                  "print('\\n'.join('line %d' % i for i in range(200)))"))
        self.run_train(queue_lanes=('glossary',))
        self.assert_retry_count(1)
        self.assertEqual(self.f.queue(), [])

    def test_stale_tail_from_an_earlier_land_is_ignored(self):
        # An older land (no LAND_TAIL support) must not be classified from a previous tail.
        self.configure_fault('AssertionError: invoice status differs', failures=2)
        self.f.shim('land', f'''#!/bin/sh
echo "$*" >>"$FIXTURE_ROOT/land.calls"
env -u LAND_TAIL sh "{ROOT / 'skills/wt-dev/bin/land'}" "$@"
''')
        self.f.state.mkdir(parents=True, exist_ok=True)
        (self.f.state / 'check.tail').write_text('R2 InternalError\n')
        self.run_train(queue_lanes=('glossary',))
        self.assert_retry_count(0)

    def test_runner_stops_when_the_main_checkout_leaves_its_branch(self):
        before = self.f.git('rev-parse', 'main')
        self.f.git('branch', 'other')
        self.f.env['LAND_CHECK'] = 'git -C "$FIXTURE_ROOT/demo" switch -q other'
        self.f.mq('add', *LANES)
        # A stop without landing exits non-zero (typed outcomes, v0.8.4); lanes stay queued.
        res = self.f.mq('run', check=False); out = res.stdout
        self.assertNotEqual(res.returncode, 0, out)
        calls = (self.f.root / 'land.calls').read_text().splitlines()
        self.assertEqual(calls, [' '.join(LANES)])
        self.assertIn('mq: stopping, target moved', out)
        self.assertEqual([row[1] for row in self.f.queue()], ['ready'] * 4)
        self.assertEqual(self.f.git('rev-parse', 'main'), before)
        self.assertEqual(self.f.git('rev-parse', 'other'), before)

    def test_local_only_marker_retries_solo(self):
        self.configure_fault('noise', failures=1)
        self.f.shim('remote-ci', '#!/bin/sh\nexit 1\n')
        check = self.f.root / 'check.py'
        check.write_text(check.read_text().replace("print('FAIL invoice browser fixture')",
                                                  "print('S3 503 SlowDown')"))
        self.run_train(queue_lanes=('glossary',))
        self.assert_retry_count(1)
        self.assertEqual(self.f.queue(), [])


if __name__ == '__main__':
    unittest.main()
