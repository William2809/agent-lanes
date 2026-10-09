import unittest
from workflow_fixture import ROOT, WorkflowFixture


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture()

    def tearDown(self):
        self.f.close()

    def prepare(self, lanes=('a', 'b')):
        for lane in lanes:
            self.f.mq('claim', lane, lane + '.txt')
            self.f.commit(self.f.lanes[lane], lane + '.txt', lane + '\n')

    def test_wk_refuses_untagged_launch_before_reserving_or_creating_lane(self):
        for args in (('-d', 'a', '-o', 'a.txt'), ('-w', '-o', 'free'),
                     ('-d', 'a', '-m', 'review')):
            with self.subTest(args=args):
                result = self.f.wk('untagged', *args, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn('-t FEATURE:REASON is required', result.stderr)
        self.assertEqual(self.f.calls(), [])
        self.assertFalse((self.f.state / 'claims/a').exists())
        self.assertFalse((self.f.home / 'worktrees/demo/untagged').exists())
        self.assertFalse((self.f.home / '.claude/state/workers.tsv').exists())

    def test_wk_allows_explicit_untagged_override(self):
        result = self.f.wk('untagged', '-d', 'a', '-o', 'a.txt',
                           env={'WK_ALLOW_UNTAGGED': '1'})
        self.f.wait_worker('untagged')
        self.assertIn('started untagged:', result.stdout)
        self.assertEqual(len(self.f.calls()), 1)
        self.assertEqual((self.f.state / 'claims/a').read_text(), 'a.txt\n')
        self.assertFalse((self.f.home / '.claude/state/worker-tags.tsv').exists())

    def test_parallel_claims_never_double_reserve(self):
        for i in range(6):
            self.f.add_lane('c' + str(i))
        commands = [[str(ROOT / 'bin/mq'), 'claim', lane, 'shared'] for lane in self.f.lanes]
        results = self.f.parallel(commands)
        self.assertEqual(sum(rc == 0 for rc, _, _ in results), 1, results)
        owners = [p.name for p in (self.f.state / 'claims').glob('*') if 'shared' in p.read_text()]
        self.assertEqual(len(owners), 1)

    def test_parallel_claim_extensions_keep_every_entry(self):
        commands = [[str(ROOT / 'bin/mq'), 'claim', 'a', 'free/' + str(i)] for i in range(12)]
        self.assertTrue(all(rc == 0 for rc, _, _ in self.f.parallel(commands)))
        self.assertEqual(set((self.f.state / 'claims/a').read_text().splitlines()),
                         {'free/' + str(i) for i in range(12)})

    def test_parallel_enqueue_has_no_lost_or_duplicate_entries(self):
        for i in range(6):
            self.f.add_lane('c' + str(i))
        self.prepare(tuple(self.f.lanes))
        commands = [[str(ROOT / 'bin/mq'), 'add', lane] for lane in self.f.lanes for _ in range(2)]
        results = self.f.parallel(commands)
        self.assertTrue(all(rc == 0 for rc, _, _ in results), results)
        self.assertEqual(sorted(row[0] for row in self.f.queue()), sorted(self.f.lanes))
        self.assertTrue(all(len(row) == 4 for row in self.f.queue()))
        results = self.f.parallel([[str(ROOT / 'bin/mq'), 'drop', lane] for lane in self.f.lanes])
        self.assertTrue(all(rc == 0 for rc, _, _ in results), results)
        self.assertEqual(self.f.queue(), [])

    def test_claim_checks_actual_unclaimed_edits(self):
        self.f.commit(self.f.lanes['b'], 'shared/file.txt', 'b\n')
        result = self.f.mq('claim', 'a', 'shared', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('touched by b', result.stdout)
        self.assertFalse((self.f.state / 'claims/a').exists())

    def test_outside_claim_conflict_names_owner_and_refuses(self):
        self.prepare(('a',))
        self.f.mq('claim', 'b', 'blocked')
        self.f.commit(self.f.lanes['a'], 'blocked/file.txt', 'oops\n')
        result = self.f.mq('add', 'a', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('claimed by b', result.stderr)
        self.assertEqual(self.f.queue(), [])
        self.assertEqual((self.f.state / 'claims/a').read_text(), 'a.txt\n')
        self.assertIn('\toutside-claim\t', (self.f.state / 'events').read_text())

    def test_free_outside_claim_auto_extends_and_versions_events(self):
        self.prepare(('a',))
        self.f.commit(self.f.lanes['a'], 'free/file with spaces.txt', 'free\n')
        self.f.mq('add', 'a')
        self.assertIn('free/file with spaces.txt', (self.f.state / 'claims/a').read_text())
        events = (self.f.state / 'events').read_text().splitlines()
        self.assertTrue(any('\tclaim-extended\t' in e for e in events))
        # Outside a git checkout (an exported tree) mq records tool=unknown.
        revision = self.f.run('git', 'rev-parse', 'HEAD', cwd=ROOT, check=False).stdout.strip() or 'unknown'
        self.assertTrue(all(len(e.split('\t')) == 4 and e.endswith('tool=' + revision) for e in events))
        self.assertIn('a  ready  0', self.f.mq('ls').stdout)

    def test_parallel_auto_extensions_never_double_claim(self):
        self.prepare()
        for lane in ('a', 'b'):
            self.f.commit(self.f.lanes[lane], 'free.txt', lane + '\n')
        results = self.f.parallel([[str(ROOT / 'bin/mq'), 'add', lane] for lane in ('a', 'b')])
        self.assertEqual(sum(rc == 0 for rc, _, _ in results), 1, results)
        owners = [p for p in (self.f.state / 'claims').glob('*') if 'free.txt' in p.read_text()]
        self.assertEqual(len(owners), 1)
        self.assertEqual(len(self.f.queue()), 1)

    def test_bounce_skips_latest_legacy_read_only_worker(self):
        self.prepare(('a',))
        self.f.worker('writer', 'a', 'danger-full-access', at=1)
        self.f.worker('reviewer', 'a', 'read-only', at=2)
        self.f.mq('add', 'a')
        result = self.f.mq('run')
        self.assertIn('BOUNCED a to writer:', result.stdout)
        self.assertIn('produced no new commit', result.stdout)
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertTrue(self.f.calls()[0]['resume'])
        self.assertEqual(self.f.calls()[0]['sandbox'], 'danger-full-access')
        result = self.f.mq('add', 'a', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('repair has no new commit', result.stderr)

    def test_latest_writer_uses_registration_order_with_equal_timestamps(self):
        self.prepare(('a',))
        self.f.worker('z-older', 'a', 'danger-full-access', at=1)
        self.f.worker('a-newer', 'a', 'workspace-write', at=1)
        self.f.worker('reviewer', 'a', 'read-only', at=1)
        self.f.mq('add', 'a')
        self.assertIn('BOUNCED a to a-newer:', self.f.mq('run').stdout)

    def test_bounce_uses_recorded_role_before_header_exists(self):
        self.prepare(('a',))
        self.f.worker('writer', 'a', 'workspace-write', at=1)
        self.f.worker('reviewer', 'a', 'read-only', recorded=True, at=2)
        self.f.mq('add', 'a')
        result = self.f.mq('run')
        self.assertIn('BOUNCED a to writer:', result.stdout)

    def test_read_only_only_lane_gets_writable_replacement(self):
        self.prepare(('a',))
        self.f.worker('reviewer', 'a', 'read-only', at=2)
        self.f.mq('add', 'a')
        result = self.f.mq('run')
        self.assertIn('BOUNCED a to a-mq1:', result.stdout)
        self.assertFalse(self.f.calls()[0]['resume'])
        self.assertEqual(self.f.calls()[0]['sandbox'], 'danger-full-access')
        rows = (self.f.home / '.claude/state/workers.tsv').read_text().splitlines()
        self.assertTrue(all(len(r.split('\t')) == 4 for r in rows))
        tags = (self.f.home / '.claude/state/worker-tags.tsv').read_text().splitlines()
        self.assertTrue(all(len(r.split('\t')) == 6 for r in tags))

    def test_manual_requeue_requires_head_movement(self):
        self.prepare(('a',))
        self.f.mq('add', 'a')
        (self.f.state / 'queue').write_text('a\tbounced\t1\twriter\n')
        head = self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['a'])
        (self.f.state / 'bounce-head.a').write_text(head + '\n')
        self.assertNotEqual(self.f.mq('add', 'a', check=False).returncode, 0)
        self.f.commit(self.f.lanes['a'], 'a.txt', 'a\nrepair\n')
        self.f.mq('add', 'a')
        self.assertEqual(self.f.queue()[0][1], 'ready')

    def test_parked_uncommitted_recovery_requires_head_movement(self):
        self.prepare(('a',))
        self.f.mq('add', 'a')
        (self.f.state / 'queue').write_text('a\tparked\t1\twriter\n')
        (self.f.state / 'parked.a').write_text('uncommitted\n')
        (self.f.state / 'bounce-head.a').write_text(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['a']) + '\n')
        self.f.mq('run')
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertEqual((self.f.state / 'parked.a').read_text(), 'no new commit\n')

    def test_writer_cap_is_atomic_and_read_only_is_unlimited(self):
        env = {'WK_MAX_WRITERS': '1', 'FAKE_WORKER_MODE': 'hold'}
        results = self.f.parallel([[str(ROOT / 'bin/wk'), lane, '-d', lane, '-o', lane + '.txt', '-t', 'fixture:new']
                                   for lane in ('a', 'b')], env=env)
        self.assertEqual(sum(rc == 0 for rc, _, _ in results), 1, results)
        self.assertTrue(any('writing workers' in err for _, _, err in results))
        for name in ('review1', 'review2'):
            self.f.wk(name, '-d', 'a', '-m', 'review', '-t', 'fixture:review', env=env)
        self.assertEqual(len(self.f.calls()), 3)
        self.assertEqual(sum(c['sandbox'] != 'read-only' for c in self.f.calls()), 1)
        # A raised cap and the explicit bypass each permit another lane's writer.
        other = 'b' if results[0][0] == 0 else 'a'
        self.f.wk('override', '-d', other, '-t', 'fixture:new', env={**env, 'WK_WRITERS_OK': '1'})
        self.assertEqual(len(self.f.calls()), 4)

    def test_w_writer_cap_blocks_before_creating_worktree(self):
        self.f.wk('writer', '-d', 'a', '-t', 'fixture:new', env={'FAKE_WORKER_MODE': 'hold'})
        result = self.f.wk('newlane', '-w', '-o', 'free', '-t', 'fixture:new',
                           env={'WK_MAX_WRITERS': '1'}, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('writing workers', result.stderr)
        self.assertFalse((self.f.home / 'worktrees/demo/newlane').exists())
        self.f.wk('newlane', '-w', '-o', 'free', '-t', 'fixture:new',
                  env={'WK_MAX_WRITERS': '2'})
        self.assertTrue((self.f.home / 'worktrees/demo/newlane').exists())

    def test_parallel_autorun_handoff_keeps_one_runner(self):
        self.f.add_lane('c')
        self.prepare(('b', 'c'))
        self.f.shim('land', '#!/bin/sh\n/bin/sleep 1\necho "LANDED fixture"\n')
        results = self.f.parallel([[str(ROOT / 'bin/mq'), 'add', lane] for lane in ('b', 'c')],
                                  env={'MQ_NO_AUTORUN': '0'})
        self.assertTrue(all(rc == 0 for rc, _, _ in results), results)
        self.assertEqual(sum('runner started' in out for _, out, _ in results), 1, results)
        import time
        until = time.monotonic() + 10
        while time.monotonic() < until and (self.f.state / 'run.lock').exists():
            time.sleep(0.05)
        self.assertEqual(self.f.queue(), [])
        events = (self.f.state / 'events').read_text().splitlines()
        self.assertEqual(sum('\tlanding\t' in row for row in events), 2)

    def test_one_writer_per_worktree_is_atomic(self):
        results = self.f.parallel([[str(ROOT / 'bin/wk'), name, '-d', 'a', '-o', 'a.txt', '-t', 'fixture:new']
                                   for name in ('writer1', 'writer2')],
                                  env={'WK_FORCE': '0', 'FAKE_WORKER_MODE': 'hold'})
        self.assertEqual(sum(rc == 0 for rc, _, _ in results), 1, results)
        self.assertTrue(any('already writing' in err for _, _, err in results))

    def test_wk_parallel_claim_reservation_refuses_overlap(self):
        results = self.f.parallel([[str(ROOT / 'bin/wk'), lane, '-d', lane, '-o', 'shared', '-t', 'fixture:new']
                                   for lane in ('a', 'b')], env={'WK_FORCE': '0'})
        self.assertEqual(sum(rc == 0 for rc, _, _ in results), 1, results)
        self.assertTrue(any('paths owned elsewhere' in err for _, _, err in results))

    def test_dead_fixer_gets_one_durable_retry(self):
        self.prepare(('a',))
        self.f.worker('writer', 'a', 'danger-full-access')
        self.f.mq('add', 'a')
        result = self.f.mq('run', env={'FAKE_WORKER_MODE': 'die'})
        self.assertIn('BOUNCED a to a-mq1r: fixer retry', result.stdout)
        self.assertIn('PARKED a: fixer died twice', result.stdout)
        self.assertEqual(len(self.f.calls()), 2)
        self.assertTrue((self.f.state / 'retry.a.1').exists())
        self.assertEqual(self.f.queue()[0][1:3], ['parked', '1'])
        events = (self.f.state / 'events').read_text()
        self.assertEqual(events.count('\tfixer-retry\t'), 1)
        # Simulate a restart with the dead retry still bounced: the marker must prevent a third launch.
        (self.f.state / 'queue').write_text('a\tbounced\t1\ta-mq1r\n')
        self.f.mq('run', env={'FAKE_WORKER_MODE': 'die'})
        self.assertEqual(len(self.f.calls()), 2)
        self.assertEqual(self.f.queue()[0][1], 'parked')

    def test_requeue_cooldown_survives_and_manual_add_bypasses_it(self):
        import time
        self.prepare(('b',))
        self.f.mq('add', 'b')
        (self.f.state / 'bounce-head.b').write_text(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['b']) + '\n')
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\nrepair\n')
        (self.f.state / 'queue').write_text('b\tparked\t1\twriter\n')
        (self.f.state / 'parked.b').write_text('uncommitted\n')
        (self.f.state / 'requeue.b').write_text(str(int(time.time())) + '\n')
        self.f.mq('run', env={'MQ_REQUEUE_COOLDOWN': '300'})
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertNotIn('\trequeued\t', (self.f.state / 'events').read_text())
        self.f.mq('add', 'b', env={'MQ_REQUEUE_COOLDOWN': '300'})
        self.assertEqual(self.f.queue()[0][1], 'ready')
        self.assertIn('LANDED', self.f.mq('run').stdout)

    def test_low_disk_prevents_land_until_disk_recovers(self):
        import subprocess
        import time
        self.prepare(('b',))
        self.f.mq('add', 'b')
        self.f.shim('diskguard', '#!/bin/sh\necho "disk: 1 GB"\n')
        proc = subprocess.Popen([str(ROOT / 'bin/mq'), 'run'], cwd=self.f.repo,
                                env={**self.f.env, 'DISKGUARD_PAUSE': '5'},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            until = time.monotonic() + 10
            while time.monotonic() < until:
                if '\tdisk-low\t' in (self.f.state / 'events').read_text():
                    break
                time.sleep(0.05)
            events = (self.f.state / 'events').read_text()
            self.assertIn('\tdisk-low\t', events)
            self.assertNotIn('\tlanding\t', events)
            self.assertEqual(self.f.queue()[0][1], 'ready')
            self.f.shim('diskguard.new', '#!/bin/sh\necho "disk: 999 GB"\n')
            (self.f.shims / 'diskguard.new').replace(self.f.shims / 'diskguard')
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn('LANDED', out)
            self.assertEqual(self.f.queue(), [])
            self.assertEqual((self.f.state / 'events').read_text().count('\tdisk-low\t'), 1)
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.communicate(timeout=5)

    def test_stale_state_and_runner_locks_are_reclaimed(self):
        self.prepare(('b',))
        for kind in ('state.lock', 'run.lock'):
            p = self.f.state / kind
            p.mkdir(); (p / 'pid').write_text('99999999\n')
        self.f.mq('add', 'b')
        result = self.f.mq('run')
        self.assertIn('LANDED', result.stdout)
        self.assertEqual(self.f.queue(), [])
        self.assertFalse((self.f.state / 'run.lock').exists())


if __name__ == '__main__':
    unittest.main()
