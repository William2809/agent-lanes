"""One landing path: real Git and state locks; full checks use a local stub."""
# Allow 60 s for fixture scheduling at host load 40-140; product deadlines stay separate.
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
import time as clock
from datetime import date, datetime, time
from workflow_fixture import ROOT, WorkflowFixture

LAND = Path(os.environ.get('ONEPATH_LAND', ROOT / 'skills/wt-dev/bin/land'))
STATS = Path(os.environ.get('ONEPATH_STATS', ROOT / 'skills/loop-stats/loop-stats.py'))
spec = importlib.util.spec_from_file_location('onepath_stats', STATS)
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)


class OneLandingPathTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(('a', 'b'))
        self.addCleanup(self.f.close)
        self.f.shim('migcheck', '#!/bin/sh\nexit 0\n')
        for lane, directory in self.f.lanes.items():
            self.f.commit(directory, lane + '.txt', lane + '\n')
        for key in ('LAND_TARGET', 'LAND_DIRECT', 'LAND_REASON'):
            self.f.env.pop(key, None)
        self.f.env['LAND_CHECK'] = 'true'

    def land(self, *lanes, **env):
        return self.f.run('sh', str(LAND), *lanes, '--keep', check=False, env=env)

    def untouched(self, result, before, heads, tail):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), before)
        for lane, sha in heads.items():
            self.assertEqual(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes[lane]), sha)
            self.assertEqual(self.f.git('status', '--porcelain', cwd=self.f.lanes[lane]), '')
        self.assertEqual(tail.read_text(), 'keep\n')
        self.assertFalse(self.f.state.exists())

    def test_land_refuses_without_queue_before_touching_anything(self):
        """R1: no queue target or override; refs, trees, tail and state stay intact."""
        before = self.f.git('rev-parse', 'HEAD')
        heads = {lane: self.f.git('rev-parse', 'HEAD', cwd=p) for lane, p in self.f.lanes.items()}
        tail = self.f.root / 'tail'
        tail.write_text('keep\n')
        result = self.land('a', LAND_TAIL=str(tail))
        self.assertEqual(result.stdout, 'FAILED land runs from the merge queue: mq add WT '
                         '(direct: LAND_DIRECT=1 LAND_REASON="why")\n')
        self.assertEqual(result.stderr, '')
        self.untouched(result, before, heads, tail)

    def test_direct_without_reason_refuses_before_touching_anything(self):
        """R1: an absent or empty reason cannot authorize a direct landing."""
        before = self.f.git('rev-parse', 'HEAD')
        heads = {lane: self.f.git('rev-parse', 'HEAD', cwd=p) for lane, p in self.f.lanes.items()}
        tail = self.f.root / 'tail'
        tail.write_text('keep\n')
        for env in ({}, {'LAND_REASON': ''}):
            with self.subTest(env=env):
                result = self.land('a', LAND_DIRECT='1', LAND_TAIL=str(tail), **env)
                self.assertEqual(len(result.stdout.splitlines()), 1)
                self.assertIn('LAND_REASON', result.stdout)
                self.untouched(result, before, heads, tail)

    def test_direct_train_lands_and_writes_one_event(self):
        """R1: a green direct train writes one four-field event with all lanes and SHA."""
        result = self.land('a', 'b', LAND_DIRECT='1', LAND_REASON='owner\tapproved\nrepair')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = (self.f.state / 'events').read_text().splitlines()
        self.assertEqual(len(rows), 1)
        at, lanes, event, detail = rows[0].split('\t')
        self.assertGreater(int(at), 0)
        self.assertEqual(lanes, 'a,b')
        self.assertEqual(event, 'landed-direct')
        sha = self.f.git('rev-parse', 'HEAD')
        self.assertTrue(detail.startswith(sha + ' '), detail)
        self.assertIn('lanes=a,b', detail)
        self.assertIn('reason=owner approved repair', detail)
        self.assertIn('a.txt', self.f.git('ls-tree', '--name-only', 'HEAD'))
        self.assertIn('b.txt', self.f.git('ls-tree', '--name-only', 'HEAD'))
        self.assertFalse((self.f.state / 'state.lock').exists())
        events = stats.read_events(self.f.home, 'demo')
        reflog = stats.reflog_landings(self.f.repo)
        day = date.today()
        start = datetime.combine(day, time()).timestamp()
        d = stats.daily([], events, reflog, start, start+86400, day.isoformat())
        self.assertEqual(d['outside_queue'], dict(total=1, direct=1, other=0))

    def test_direct_event_waits_for_queue_state_lock_but_merge_does_not(self):
        """R1: the landed-direct event waits for mq's shared state lock; the merge (and its git hooks,
        which may call mq) does not hold or wait on it (v0.8.4 review). ff-only guards the branch."""
        lock = self.f.state / 'state.lock'
        lock.mkdir(parents=True)
        (lock / 'pid').write_text(str(os.getpid()) + '\n')
        before = self.f.git('rev-parse', 'HEAD')
        out = self.f.root / 'lock-wait.log'
        with out.open('w') as stream:
            proc = subprocess.Popen(['sh', str(LAND), 'a', '--keep'], cwd=self.f.repo,
                env={**self.f.env, 'LAND_DIRECT': '1', 'LAND_REASON': 'test'},
                stdout=stream, stderr=subprocess.STDOUT)
        try:
            until = clock.monotonic() + 60
            while 'check green' not in out.read_text() and proc.poll() is None:
                self.assertLess(clock.monotonic(), until, out.read_text())
                clock.sleep(0.05)
            until = clock.monotonic() + 60
            while self.f.git('rev-parse', 'HEAD') == before:
                self.assertLess(clock.monotonic(), until, 'merge waited on the state lock: ' + out.read_text())
                clock.sleep(0.05)
            clock.sleep(0.2)
            self.assertIsNone(proc.poll(), out.read_text())  # still waiting to append the event
            self.assertEqual((self.f.state / 'events').read_text(), '')
        finally:
            self.f.run(str(ROOT / 'bin/ctrash'), str(lock))
            proc.wait(timeout=40)
        self.assertEqual(proc.returncode, 0, out.read_text())
        self.assertEqual(len((self.f.state / 'events').read_text().splitlines()), 1)

    def test_post_merge_interrupt_records_direct_event_once(self):
        """R1: a successful merge records one direct event even if post-land stops it."""
        self.f.shim('land', f'#!/bin/sh\necho $$ >"$FIXTURE_ROOT/land.pid"\n'
                           f'exec sh "{LAND}" "$@" --keep\n')
        hook = self.f.repo / 'tools/post-land.sh'
        hook.parent.mkdir()
        hook.write_text('#!/bin/sh\nkill -TERM "$(cat "$FIXTURE_ROOT/land.pid")"\n')
        hook.chmod(0o755)
        result = self.f.run('land', 'a', env={'LAND_DIRECT': '1', 'LAND_REASON': 'test'})
        self.assertIn('interrupted after the merge', result.stdout)
        rows = (self.f.state / 'events').read_text().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].split('\t')[2], 'landed-direct')
        self.assertTrue(rows[0].split('\t')[3].startswith(self.f.git('rev-parse', 'HEAD') + ' '))

    def test_real_reflog_counts_queue_direct_and_hand_merge(self):
        """R2: real branch reflogs and mq events yield one direct and one hand landing."""
        directory = self.f.add_lane('c')
        self.f.commit(directory, 'c.txt', 'c\n')
        self.f.shim('land', f'#!/bin/sh\nexec sh "{LAND}" "$@" --keep\n')
        self.f.mq('add', 'a')
        self.f.mq('run')
        self.assertEqual(self.land('b', LAND_DIRECT='1', LAND_REASON='test').returncode, 0)
        self.f.git('merge', '-q', '--no-ff', '-m', 'hand integration', 'wt/c')
        reflog = stats.reflog_landings(self.f.repo)
        self.assertEqual(len(reflog), 3)
        day = date.today()
        start = datetime.combine(day, time()).timestamp()
        events = stats.read_events(self.f.home, 'demo')
        d = stats.daily([], events, reflog, start, start+86400, day.isoformat())
        self.assertEqual(d['landings'], 3)
        self.assertEqual(d['queue']['events']['landed'], 1)
        self.assertEqual(d['outside_queue'], dict(total=2, direct=1, other=1))
        self.assertIn('Outside queue: 2 (direct 1, other 1)',
                      stats.render(dict(repo='demo', day=d['day'], daily=d, trend=[d])))
        output = json.loads(self.f.run('python3', str(STATS), '--json', '--days', '1').stdout)
        self.assertEqual(output['daily']['outside_queue'], dict(total=2, direct=1, other=1))
        self.assertEqual(output['trend'][0]['outside_queue'], dict(total=2, direct=1, other=1))
        text = self.f.run('python3', str(STATS), '--days', '1').stdout
        self.assertIn('Outside queue: 2 (direct 1, other 1)', text)

    def test_target_alone_does_not_authorize_landing(self):
        """A branch selector is not proof of queue ownership."""
        before = self.f.git('rev-parse', 'HEAD')
        result = self.land('a', LAND_TARGET='refs/heads/main')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('FAILED land runs from the merge queue', result.stdout)
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), before)
        self.assertFalse((self.f.state / 'events').exists())

    def test_real_queue_lands_without_a_direct_event(self):
        self.f.shim('land', f'#!/bin/sh\nexec sh "{LAND}" "$@" --keep\n')
        self.f.mq('add', 'a')
        result = self.f.mq('run', env={'LAND_DIRECT': '1', 'LAND_REASON': ''})
        self.assertIn('LANDED ', result.stdout)
        self.assertNotIn('landed-direct', (self.f.state / 'events').read_text())

    def test_failed_direct_check_has_no_landed_event(self):
        """R1: a red direct check changes no integration ref and writes no landing event."""
        before = self.f.git('rev-parse', 'HEAD')
        result = self.land('a', LAND_DIRECT='1', LAND_REASON='test', LAND_CHECK='exit 1')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), before)
        path = self.f.state / 'events'
        self.assertFalse(path.exists() and path.read_text())


if __name__ == '__main__':
    unittest.main()
