"""Regression tests for a queue review (batch 2): land restores interrupted trains,
preflight failures carry a candidate SHA and blame."""
import os
import signal
import subprocess
import time
import unittest
from workflow_fixture import ROOT, WorkflowFixture
import test_bounce_train as bt

LAND = ROOT / 'skills/wt-dev/bin/land'


class RestoreTests(unittest.TestCase):
    """Lanes live under a folder with a space; land a b stacks b on a."""
    def setUp(self):
        self.f = WorkflowFixture(())
        self.addCleanup(self.f.close)
        self.f.shim('migcheck', '#!/bin/sh\nexit 0\n')
        lanes = self.f.root / 'lane dir'
        self.f.shim('wt-dev', f'''#!/bin/sh
case "$1" in path) echo "{lanes}/$2" ;; install) ;; *) exit 2 ;; esac
''')
        self.heads = {}
        for lane in ('a', 'b'):
            p = lanes / lane
            self.f.git('worktree', 'add', '-q', '-b', 'wt/' + lane, str(p))
            self.f.commit(p, lane + '.txt', lane + '\n')
            self.heads[lane] = self.f.git('rev-parse', 'HEAD', cwd=p)
        self.b = lanes / 'b'

    def start(self, check):
        out = self.f.root / 'land.out'
        with open(out, 'w') as f:
            p = subprocess.Popen(['sh', str(LAND), 'a', 'b', '--keep'], cwd=self.f.repo,
                                 env={**self.f.env, 'LAND_CHECK': check}, stdout=f, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        return p, out

    def assert_restored(self, text):
        self.assertEqual(self.f.git('rev-parse', 'HEAD', cwd=self.b), self.heads['b'], text)
        self.assertFalse((self.b / 'a.txt').exists())
        self.assertEqual(self.f.git('status', '--porcelain', cwd=self.b), '')
        self.assertIn('restored 2 worktree(s)', text)
        self.assertNotIn('NOT restored', text)

    def test_failed_check_restores_lanes_whose_path_has_a_space(self):
        p, out = self.start('exit 1')
        p.wait(timeout=60)
        text = out.read_text()
        self.assertEqual(p.returncode, 1, text)
        self.assertTrue(text.splitlines()[-1].startswith('FAILED check rc=1'), text)
        self.assert_restored(text)

    def test_sigterm_during_the_check_restores_the_stack(self):
        p, out = self.start('sleep 30')
        until = time.monotonic() + 30
        while 'stacked b' not in out.read_text():
            self.assertLess(time.monotonic(), until, out.read_text())
            time.sleep(0.05)
        time.sleep(0.3)
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except PermissionError:
            # macOS can refuse a group signal (EPERM) while a group member is in some states; land's trap still runs.
            os.kill(p.pid, signal.SIGTERM)
        p.wait(timeout=30)
        text = out.read_text()
        self.assertEqual(p.returncode, 143, text)
        self.assertEqual(text.splitlines()[-1], 'FAILED interrupted (TERM)', text)
        self.assert_restored(text)
        self.assertEqual(self.f.git('rev-parse', 'HEAD', cwd=self.f.root / 'lane dir/a'), self.heads['a'])

    def test_green_land_keeps_the_landed_commits(self):
        p, out = self.start('true')
        p.wait(timeout=60)
        text = out.read_text()
        self.assertIn('LANDED', text)
        self.assertNotIn('restored', text)
        self.assertEqual(self.f.git('rev-parse', 'HEAD', cwd=self.b), self.f.git('rev-parse', 'main'))


class QueueInterruptTests(unittest.TestCase):
    setUp = bt.BounceTrainTests.setUp

    def test_runner_stops_and_keeps_lanes_queued_when_land_is_interrupted(self):
        self.f.shim('land', f'''#!/bin/sh
echo "$*" >>"$FIXTURE_ROOT/land.calls"; echo $$ >"$FIXTURE_ROOT/land.pid"
exec sh "{LAND}" "$@"
''')
        heads = {lane: self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes[lane]) for lane in bt.LANES}
        self.f.env['LAND_CHECK'] = 'kill -TERM "$(cat "$FIXTURE_ROOT/land.pid")"; sleep 1'
        self.f.mq('add', *bt.LANES)
        # A stop without landing exits non-zero (typed outcomes, v0.8.4); lanes stay queued.
        res = self.f.mq('run', check=False); out = res.stdout
        self.assertNotEqual(res.returncode, 0, out)
        self.assertIn('mq: stopping, interrupted (TERM); lanes stay queued', out)
        self.assertEqual((self.f.root / 'land.calls').read_text().splitlines(), [' '.join(bt.LANES)])
        self.assertEqual([row[1] for row in self.f.queue()], ['ready'] * 4)
        self.assertEqual(list(self.f.root.glob('*.message')), [])
        self.assertEqual({lane: self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes[lane]) for lane in bt.LANES}, heads)


class PreflightBlameTests(unittest.TestCase):
    setUp = bt.BounceTrainTests.setUp
    run_train = bt.BounceTrainTests.run_train

    def preflight(self, body):
        tools = self.f.repo / 'tools'
        tools.mkdir()
        guard = tools / 'land-preflight.sh'
        guard.write_text('#!/bin/sh\n' + body)
        guard.chmod(0o755)

    def test_preflight_failure_names_its_candidate(self):
        self.preflight('[ ! -f glossary.txt ] || { echo "FAIL glossary.test.ts > keeps status"; exit 3; }\n')
        self.f.env['LAND_TAIL'] = str(self.f.root / 'tail')
        r = subprocess.run(['sh', str(LAND), 'glossary', '--keep'], cwd=self.f.repo, env=self.f.env,
                           capture_output=True, text=True, timeout=60)
        last = r.stdout.splitlines()[-1]
        self.assertRegex(last, r'^FAILED preflight rc=3 on [0-9a-f]{7,} \(main\.\.[0-9a-f]{7,}\)$')
        self.assertIn('FAIL glossary.test.ts', (self.f.root / 'tail').read_text())

    def test_preflight_failure_blames_the_lane_that_touches_the_failing_file(self):
        self.f.commit(self.f.lanes['glossary'], 'glossary.test.ts', 'test\n')
        self.preflight('[ ! -f glossary.txt ] || { echo "FAIL glossary.test.ts > keeps status"; exit 1; }\n')
        self.run_train()
        self.assertEqual(self.calls, [' '.join(bt.LANES), 'split1 split2 gridtype', 'glossary'])
        self.assertIn('suspect glossary (touches failing file glossary.test.ts)', self.output)
        self.assertIn('test: keeps status', self.output)
        self.assertFalse((self.f.root / 'diagnostic.calls').exists())

    def test_preflight_without_file_names_keeps_the_serial_fallback(self):
        self.preflight('[ ! -f glossary.txt ] || { echo "lint failed"; exit 1; }\n')
        self.run_train()
        self.assertEqual(self.calls, [' '.join(bt.LANES), *bt.LANES])
        self.assertIn('no clear suspect', self.output)

    def test_install_that_edits_a_tracked_file_names_the_install_step(self):
        self.f.shim('wt-dev', (self.f.shims / 'wt-dev').read_text().replace(
            "elif cmd == 'install': pass",
            "elif cmd == 'install': (lanes / sys.argv[2] / 'base.txt').write_text('lockfile drift\\n')"))
        r = subprocess.run(['sh', str(LAND), 'glossary', '--keep'], cwd=self.f.repo, env=self.f.env,
                           capture_output=True, text=True, timeout=60)
        self.assertRegex(r.stdout.splitlines()[-1], r'^FAILED install changed tracked files on [0-9a-f]{7,} ')
        self.assertEqual(len(self.f.root.joinpath('checks.jsonl').read_text().splitlines())
                         if self.f.root.joinpath('checks.jsonl').exists() else 0, 0)


if __name__ == '__main__':
    unittest.main()
