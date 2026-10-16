"""Regression and failure-injection checks for a queue review."""
# Allow 60 s for fixture scheduling at host load 40-140; product deadlines stay separate.
import os
import signal
import subprocess
import time
import unittest
from workflow_fixture import ROOT, WorkflowFixture


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture()

    def tearDown(self):
        self.f.close()

    def prepare(self, lane='a'):
        self.f.mq('claim', lane, lane + '.txt')
        self.f.commit(self.f.lanes[lane], lane + '.txt', lane + '\n')

    def wait_for(self, predicate):
        until = time.monotonic() + 60
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.05)
        self.fail('fixture condition did not arrive')

    def test_crashed_autorun_handoff_is_reclaimed(self):
        self.prepare('b')
        self.f.shim('land', '#!/bin/sh\ntouch "$FIXTURE_ROOT/landing"\n/bin/sleep 2\necho "LANDED fixture"\n')
        self.f.mq('add', 'b', env={'MQ_NO_AUTORUN': '0'})
        self.wait_for(lambda: (self.f.root / 'landing').exists())
        lock = self.f.state / 'run.lock'
        pid = int((lock / 'pid').read_text())  # runner started by this test's mq add
        os.kill(pid, signal.SIGKILL)
        self.wait_for(lambda: not subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                                                 capture_output=True, text=True).stdout.strip())
        old = time.time() - 20
        os.utime(lock / 'handoff', (old, old))
        self.f.shim('land', '#!/bin/sh\necho "LANDED fixture"\n')
        self.assertIn('LANDED', self.f.mq('run').stdout)
        self.assertEqual(self.f.queue(), [])
        self.assertFalse(lock.exists())

    def test_missing_pid_and_abandoned_reaper_recover(self):
        self.prepare('b')
        for name in ('state.lock', 'run.lock'):
            lock = self.f.state / name
            (lock / 'reap').mkdir(parents=True)
            (lock / 'reap/pid').write_text('99999999\n')
            old = time.time() - 20
            os.utime(lock, (old, old))
        self.f.mq('add', 'b')
        self.assertIn('LANDED', self.f.mq('run').stdout)
        self.assertEqual(self.f.queue(), [])

    def test_live_reaper_is_respected_then_recovered(self):
        self.prepare('b')
        owner = subprocess.Popen(['/bin/sleep', '120'])
        lock = self.f.state / 'run.lock'
        (lock / 'reap').mkdir(parents=True)
        (lock / 'pid').write_text('99999999\n')
        (lock / 'reap/pid').write_text(str(owner.pid) + '\n')
        try:
            self.f.mq('add', 'b')
            # The fixture has no reap/birth file, so it models an older reaper.
            expected = (f'mq: an older runner (pid {owner.pid}) still holds the queue; '
                        'it will be picked up when it exits\n')
            self.assertEqual(self.f.mq('run').stdout, expected)
            self.assertEqual(self.f.queue()[0][1], 'ready')
        finally:
            owner.terminate()
            owner.wait(timeout=60)
        self.assertIn('LANDED', self.f.mq('run').stdout)

    def test_failed_claim_sort_preserves_ownership_and_refuses_enqueue(self):
        self.prepare()
        self.f.commit(self.f.lanes['a'], 'free/file with spaces.txt', 'free\n')
        original = (self.f.state / 'claims/a').read_text()
        self.f.shim('sort', '#!/bin/sh\ncase "$*" in *claims/.claim.*) echo partial; exit 1 ;; esac\nexec /usr/bin/sort "$@"\n')
        for args in (('claim', 'a', 'extra'), ('add', 'a')):
            with self.subTest(args=args):
                result = self.f.mq(*args, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.f.state / 'claims/a').read_text(), original)
                self.assertEqual(self.f.queue(), [])
        self.assertNotIn('\tclaim-extended\t', (self.f.state / 'events').read_text())

    def test_failed_claim_read_preserves_ownership(self):
        self.prepare()
        original = (self.f.state / 'claims/a').read_text()
        self.f.shim('cat', '#!/bin/sh\ncase "$1" in */claims/a) echo partial; exit 1 ;; esac\nexec /bin/cat "$@"\n')
        self.assertNotEqual(self.f.mq('claim', 'a', 'extra', check=False).returncode, 0)
        self.assertEqual((self.f.state / 'claims/a').read_text(), original)

    def test_failed_claim_write_does_not_publish(self):
        # Inject a failed path write into the same helper used by claim and add.
        source = (ROOT / 'bin/mq').read_text()
        helper = source[source.index('_extend_claim() ('):source.index('\n_claim() {')]
        self.prepare()
        original = (self.f.state / 'claims/a').read_text()
        script = helper + '\n' + 'st=$1\nprintf() { return 1; }\n_extend_claim a extra\n'
        result = self.f.run('/bin/sh', '-c', script, 'probe', str(self.f.state), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.f.state / 'claims/a').read_text(), original)

    def test_reused_worker_name_never_resumes_another_lane(self):
        self.prepare()
        self.f.worker('writer', 'a', 'danger-full-access', at=1)
        self.f.worker('writer', 'b', 'danger-full-access', at=2)
        self.f.mq('add', 'a')
        self.assertIn('BOUNCED a to a-mq1:', self.f.mq('run').stdout)
        self.assertFalse(self.f.calls()[0]['resume'])
        self.assertEqual(self.f.calls()[0]['cwd'], str(self.f.lanes['a']))

    def test_current_session_directory_checked_even_with_lane_registry_row(self):
        self.prepare()
        log = self.f.worker('writer', 'a', 'danger-full-access')
        log.write_text(log.read_text().replace(str(self.f.lanes['a']), str(self.f.lanes['b'])))
        result = self.f.wk('writer', '-r', 'repair', env={'WK_RESUME_DIR': str(self.f.lanes['a'])}, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('session does not match writable lane', result.stderr)
        self.f.mq('add', 'a')
        self.assertIn('BOUNCED a to a-mq1:', self.f.mq('run').stdout)
        self.assertEqual(len(self.f.calls()), 1)
        self.assertEqual(self.f.calls()[0]['cwd'], str(self.f.lanes['a']))

    def test_blocked_setup_reserves_capacity_without_blocking_other_launches(self):
        self.f.worker('resume', 'b', 'danger-full-access')
        shim = (self.f.shims / 'wt-dev').read_text()
        shim = shim.replace("    name = sys.argv[2]", """    name = sys.argv[2]
    if name == 'blocked':
        import time
        (root / 'setup-wait').touch()
        while not (root / 'setup-release').exists(): time.sleep(0.05)""")
        self.f.shim('wt-dev', shim)
        proc = subprocess.Popen([str(ROOT / 'bin/wk'), 'blocked', '-w', '-o', 'free', '-t', 'fixture:new'],
                                cwd=self.f.repo, env={**self.f.env, 'WK_MAX_WRITERS': '2'},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        proc.stdin.write('fixture task\n'); proc.stdin.close(); proc.stdin = None
        try:
            self.wait_for(lambda: (self.f.root / 'setup-wait').exists())
            self.f.wk('review', '-d', 'a', '-m', 'review', '-t', 'fixture:review', env={'WK_MAX_WRITERS': '0'})
            self.f.wait_worker('review')
            self.f.wk('resume', '-r', 'continue', env={'WK_MAX_WRITERS': '2'})
            self.f.wait_worker('resume')
            self.f.wk('other', '-d', 'b', '-t', 'fixture:new',
                      env={'WK_MAX_WRITERS': '2', 'FAKE_WORKER_MODE': 'hold'})
            refused = self.f.wk('third', '-w', '-o', 'third', '-t', 'fixture:new',
                                env={'WK_MAX_WRITERS': '2'}, check=False)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('writing workers', refused.stderr)
            self.assertFalse((self.f.home / 'worktrees/demo/third').exists())
            # Other launches finished while setup still waits for our release.
            self.assertFalse((self.f.root / 'setup-release').exists())
            self.assertIsNone(proc.poll())
        finally:
            (self.f.root / 'setup-release').touch()
            out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn('started blocked:', out)
        self.f.wait_worker('blocked')
        self.assertEqual([p for p in (self.f.state / 'pending').glob('*') if p.read_text()], [])  # emptied, not moved

    def test_failed_setup_releases_reservation_and_dead_slots_do_not_count(self):
        shim = (self.f.shims / 'wt-dev').read_text().replace("    name = sys.argv[2]", "    sys.exit(1)")
        self.f.shim('wt-dev', shim)
        refused = self.f.wk('failed', '-w', '-o', 'free', '-t', 'fixture:new', check=False)
        self.assertNotEqual(refused.returncode, 0)
        pending = self.f.state / 'pending'
        self.assertEqual([p for p in pending.glob('*') if p.read_text()], [])  # emptied, not moved
        (pending / 'dead').write_text('99999999\t/nonexistent\n')
        self.f.wk('writer', '-d', 'a', '-t', 'fixture:new', env={'WK_MAX_WRITERS': '1'})
        self.f.wait_worker('writer')

    def refused_runner(self):
        # Deferred-only runners now stay alive; stop only this test's runner after refusal.
        self.f.shim('sleep', '#!/bin/sh\ncase \"$1\" in 30) touch \"$FIXTURE_ROOT/refused-idle\"; exec /bin/sleep 0.05 ;; *) exec /bin/sleep \"$@\" ;; esac\n')
        proc = subprocess.Popen([str(ROOT / 'bin/mq'), 'run'], cwd=self.f.repo,
                                env={**self.f.env, 'WK_MAX_WRITERS': '0', 'MQ_REQUEUE_COOLDOWN': '300'},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # The journal clears before mq prints its refusal; wait for the idle boundary.
            self.wait_for(lambda: (self.f.root / 'refused-idle').exists())
            self.assertTrue((self.f.state / 'deferred.a').exists())
            self.assertFalse((self.f.state / 'launch.a').exists())
            self.assertIsNone(proc.poll())
        finally:
            if proc.poll() is None: proc.terminate()
            out, err = proc.communicate(timeout=60)
        return out

    def test_initial_fixer_refusal_can_retry_unchanged_lane(self):
        self.prepare()
        self.f.mq('add', 'a')
        refused = self.refused_runner()
        self.assertIn('fixer launch deferred', refused)
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertFalse((self.f.state / 'bounce-head.a').exists())
        self.assertEqual(self.f.calls(), [])
        # No new commit is possible yet: mq add must schedule the refused launch.
        self.f.mq('add', 'a', env={'MQ_REQUEUE_COOLDOWN': '300'})
        self.assertEqual(self.f.queue()[0][1], 'parked')
        result = self.f.mq('run', env={'WK_MAX_WRITERS': '1', 'FAKE_WORKER_MODE': 'repair'})
        self.assertIn('BOUNCED a to a-mq1:', result.stdout)
        self.assertIn('LANDED', result.stdout)
        self.assertEqual(len(self.f.calls()), 1)
        self.assertEqual(self.f.queue(), [])

    def test_refused_dead_fixer_retry_is_not_consumed(self):
        self.prepare()
        self.f.mq('add', 'a')
        log = self.f.worker('dead', 'a', 'danger-full-access')
        log.write_text(log.read_text().split('tokens used')[0])
        (self.f.state / 'queue').write_text('a\tbounced\t1\tdead\n')
        (self.f.state / 'bounce-head.a').write_text(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['a']) + '\n')
        self.refused_runner()
        self.assertFalse((self.f.state / 'retry.a.1').exists())
        self.assertEqual(self.f.calls(), [])
        self.f.mq('add', 'a')
        self.assertEqual(self.f.queue()[0][1], 'parked')
        (self.f.root / 'failed-a').touch()
        result = self.f.mq('run', env={'WK_MAX_WRITERS': '1', 'FAKE_WORKER_MODE': 'repair'})
        self.assertIn('BOUNCED a to a-mq1r: fixer retry', result.stdout)
        self.assertIn('LANDED', result.stdout)
        self.assertTrue((self.f.state / 'retry.a.1').exists())
        self.assertEqual(len(self.f.calls()), 1)
        self.assertEqual((self.f.state / 'events').read_text().count('\tfixer-retry\t'), 1)

    def legacy_queue(self, *, repaired=False, ambiguous=False):
        self.prepare()
        before = self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['a'])
        reflog = self.f.git('reflog', 'show', '--format=%gD', '--date=unix', '-1', 'HEAD', cwd=self.f.lanes['a'])
        commit_at = int(reflog.split('{')[1].split('}')[0])
        at = commit_at if ambiguous else commit_at + 10
        self.f.worker('legacy', 'a', 'danger-full-access', at=at)
        self.f.mq('add', 'a')
        (self.f.state / 'queue').write_text('a\tbounced\t1\tlegacy\n')
        with (self.f.state / 'events').open('a') as f:
            f.write(f'{at}\ta\tbounced\tsimulated preflight\n')
        (self.f.root / 'failed-a').touch()
        if repaired:
            (self.f.lanes['a'] / 'a.txt').write_text('a\nlegacy repair\n')
            self.f.git('add', '--', 'a.txt', cwd=self.f.lanes['a'])
            self.f.run('git', 'commit', '-q', '-m', 'legacy repair', cwd=self.f.lanes['a'],
                       env={'GIT_COMMITTER_DATE': f'@{at + 10} +0000'})
        return before

    def test_legacy_committed_repair_migrates_for_runner_and_manual_add(self):
        for manual in (False, True):
            with self.subTest(manual=manual):
                if manual:
                    self.f.close(); self.f = WorkflowFixture()
                before = self.legacy_queue(repaired=True)
                if manual:
                    self.f.mq('add', 'a')
                    self.assertEqual((self.f.state / 'bounce-head.a').read_text().strip(), before)
                result = self.f.mq('run')
                self.assertIn('LANDED', result.stdout)
                self.assertEqual(self.f.calls(), [])
                self.assertEqual(self.f.queue(), [])
                self.assertIn('baseline from reflog', (self.f.state / 'events').read_text())

    def test_legacy_known_unchanged_repair_still_cannot_requeue(self):
        self.legacy_queue()
        self.assertNotEqual(self.f.mq('add', 'a', check=False).returncode, 0)
        self.assertIn('produced no new commit', self.f.mq('run').stdout)
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertEqual(self.f.calls(), [])

    def test_ambiguous_legacy_baseline_requires_fresh_repair_commit(self):
        self.legacy_queue(repaired=True, ambiguous=True)
        result = self.f.mq('run', env={'FAKE_WORKER_MODE': 'repair'})
        self.assertIn('BOUNCED a to a-mq1:', result.stdout)
        self.assertIn('LANDED', result.stdout)
        self.assertEqual(len(self.f.calls()), 1)
        self.assertFalse(self.f.calls()[0]['resume'])
        self.assertIn('fresh repair required', (self.f.state / 'events').read_text())

    def test_missing_legacy_history_requires_fresh_repair_on_manual_add(self):
        self.prepare()
        self.f.worker('legacy', 'a', 'danger-full-access')
        self.f.mq('add', 'a')
        (self.f.state / 'queue').write_text('a\tbounced\t1\tlegacy\n')
        (self.f.root / 'failed-a').touch()
        # No bounced event means no defensible historical baseline.
        self.f.mq('add', 'a')
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertFalse((self.f.state / 'bounce-head.a').exists())
        result = self.f.mq('run', env={'FAKE_WORKER_MODE': 'noop'})
        self.assertIn('produced no new commit', result.stdout)
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertEqual(len(self.f.calls()), 1)
        self.assertNotEqual(self.f.mq('add', 'a', check=False).returncode, 0)

    def test_legacy_parked_repair_also_requires_migrated_head(self):
        self.legacy_queue()
        (self.f.state / 'queue').write_text('a\tparked\t1\tlegacy\n')
        (self.f.state / 'parked.a').write_text('uncommitted\n')
        result = self.f.mq('add', 'a', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('repair has no new commit', result.stderr)
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertTrue((self.f.state / 'bounce-head.a').exists())


if __name__ == '__main__':
    unittest.main()
