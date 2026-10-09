"""Regressions for the PR #9 review (v0.8.1): worker records, pause identity, record locks."""
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest
from workflow_fixture import ROOT, WorkflowFixture
from test_run_records import CODEX, SCRIPTS


class V081Tests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(('a',))
        self.addCleanup(self.f.close)
        self.f.shim('codex', CODEX)

    def log(self, name, repo='demo'):
        return self.f.home / '.claude/state/logs' / repo / (name + '.log')

    def side(self, name, suffix, repo='demo'):
        return Path(str(self.log(name, repo)) + suffix)

    def wait_for(self, path, timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if path.exists():
                return path.read_text()
            time.sleep(0.02)
        self.fail(f'{path} did not appear')

    def record(self, name, repo='demo'):
        return dict(line.split('=', 1) for line in self.side(name, '.run', repo).read_text().splitlines())

    def paused(self):
        return self.f.home / '.claude/state/paused-workers.tsv'

    def other_repo(self):
        other = self.f.root / 'other'
        other.mkdir()
        self.f.git('init', '-q', '-b', 'main', cwd=other)
        self.f.commit(other, 'base.txt', 'base\n')
        return other

    def test_same_name_launch_in_another_repo_keeps_pause(self):
        self.f.wk('r1', '-d', 'a', '-t', 'fixture:new', env={'FAKE_WORKER_MODE': 'hold'})
        self.f.run(str(SCRIPTS / 'batches.sh'), 'pause', 'r1')
        self.wait_for(self.side('r1', '.exit'))
        run_id = self.record('r1')['run_id']
        other = self.other_repo()
        self.f.run(str(ROOT / 'bin/wk'), 'r1', '-t', 'fixture:new', input='other task\n', cwd=other)
        self.wait_for(self.side('r1', '.exit', 'other'))
        self.assertIn(f'r1\t{self.f.lanes["a"]}\t{run_id}', self.paused().read_text())
        result = self.f.run(str(SCRIPTS / 'batches.sh'), 'resume', 'r1')
        self.assertEqual(result.stdout.strip(), 'resumed r1')
        self.assertNotEqual(self.record('r1')['run_id'], run_id)
        self.assertNotIn('r1\t', self.paused().read_text())

    def test_pause_marks_the_attempt_record(self):
        self.f.wk('r1', '-d', 'a', '-t', 'fixture:new', env={'FAKE_WORKER_MODE': 'hold'})
        self.wait_for(Path(str(self.log('r1')) + '.pid'))
        self.f.run(str(SCRIPTS / 'batches.sh'), 'pause', 'r1')
        self.assertRegex(self.wait_for(self.side('r1', '.exit')), r'^rc=143 ')
        self.assertRegex(self.record('r1')['paused_at'], r'^\d+$')
        self.f.run(str(ROOT / 'bin/wk'), 'r1', '-r', 'continue')
        archived = [p for p in self.log('r1').parent.glob('r1.*.log.run')]
        self.assertEqual(len(archived), 1)
        self.assertIn('paused_at=', archived[0].read_text())

    def test_killed_resume_leaves_no_record_writer(self):
        self.f.wk('r1', '-d', 'a', '-t', 'fixture:new')
        self.wait_for(self.side('r1', '.exit'))
        # The second record-lock acquisition (after archive) is the new record's; hold it there.
        self.f.shim('mkdir', '''#!/bin/sh
case "$*" in *r1.log.record.lock)
  n=$(cat "$FIXTURE_ROOT/lock-count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$FIXTURE_ROOT/lock-count"
  [ "$n" != 2 ] || { : >"$FIXTURE_ROOT/blocked"; /bin/sleep 2; } ;;
esac
exec /bin/mkdir "$@"
''')
        wk = subprocess.Popen([str(ROOT / 'bin/wk'), 'r1', '-r', 'continue'], cwd=self.f.repo,
                              env=self.f.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_for(self.f.root / 'blocked')
        wk.kill()
        wk.wait()
        time.sleep(3)
        self.assertFalse(self.side('r1', '.run').exists(), 'a record writer outlived the killed wk')

    def test_killed_launch_stops_its_run_worker(self):
        self.f.shim('mkdir', '''#!/bin/sh
case "$*" in *r2.log.record.lock)
  [ -f "$FIXTURE_ROOT/blocked" ] || { : >"$FIXTURE_ROOT/blocked"; /bin/sleep 2; } ;;
esac
exec /bin/mkdir "$@"
''')
        wk = subprocess.Popen([str(ROOT / 'bin/wk'), 'r2', '-d', 'a', '-t', 'fixture:new'], cwd=self.f.repo,
                              env=self.f.env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, text=True)
        wk.stdin.write('fixture task\n')
        wk.stdin.close()
        self.wait_for(self.f.root / 'blocked')
        wk.kill()
        wk.wait()
        time.sleep(3)
        self.assertFalse(self.side('r2', '.run').exists(), 'run-worker recorded a cancelled launch')
        self.assertFalse((self.f.root / 'codex-args').exists(), 'run-worker started a cancelled launch')

    def test_relaunch_waits_for_supervisor_publication(self):
        self.f.wk('r1', '-d', 'a', '-t', 'fixture:new', env={'FAKE_WORKER_MODE': 'hold'})
        worker = int(self.wait_for(self.side('r1', '.pid')).strip())
        child = int(self.wait_for(self.f.root / 'codex-child.pid').strip())
        found = subprocess.run(['pgrep', '-f', f'supervise-worker.sh {self.log("r1")}'],
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(len(found), 1)
        supervisor = int(found[0])
        os.kill(supervisor, signal.SIGSTOP)
        try:
            os.kill(child, signal.SIGTERM)
            until = time.monotonic() + 10
            while time.monotonic() < until:
                try:
                    os.kill(worker, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail('worker did not exit')
            self.assertEqual(self.f.run(str(ROOT / 'bin/wk'), 'r1', '-d', 'a', '-t', 'fixture:new',
                                        input='again\n', check=False).returncode, 1)
        finally:
            os.kill(supervisor, signal.SIGCONT)
        self.assertRegex(self.wait_for(self.side('r1', '.exit')), r'^rc=0 ')
        self.assertIn('Changed: sidecar report', self.side('r1', '.last').read_text())

    def test_cancelled_resume_keeps_the_pause(self):
        self.f.wk('r1', '-d', 'a', '-t', 'fixture:new', env={'FAKE_WORKER_MODE': 'hold'})
        self.wait_for(self.side('r1', '.pid'))
        self.f.run(str(SCRIPTS / 'batches.sh'), 'pause', 'r1')
        self.wait_for(self.side('r1', '.exit'))
        before = self.paused().read_text()
        result = self.f.run(str(ROOT / 'bin/wk'), 'r1', '-r', 'continue', check=False,
                            env={'WK_LAUNCH_INTENT': str(self.f.root / 'cancelled-intent')})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.paused().read_text(), before)

    def test_legacy_pause_row_of_another_directory_does_not_pause(self):
        self.f.wk('r1', '-d', 'a', '-t', 'fixture:new')
        self.wait_for(self.side('r1', '.exit'))
        self.paused().write_text('r1\t/elsewhere\n')
        status = self.f.run(str(SCRIPTS / 'batches.sh'), '--all', check=False).stdout
        self.assertRegex(status, r'r1 +done')
        self.paused().write_text(f'r1\t{self.f.lanes["a"]}\n')
        status = self.f.run(str(SCRIPTS / 'batches.sh'), '--all', check=False).stdout
        self.assertRegex(status, r'r1 +paused')

    def test_wait_workers_waits_for_supervisor_publication(self):
        self.f.wk('r1', '-d', 'a', '-t', 'fixture:new', env={'FAKE_WORKER_MODE': 'hold'})
        worker = int(self.wait_for(self.side('r1', '.pid')).strip())
        child = int(self.wait_for(self.f.root / 'codex-child.pid').strip())
        supervisor = int(self.record('r1')['supervisor_pid'])
        os.kill(supervisor, signal.SIGSTOP)
        try:
            os.kill(child, signal.SIGTERM)
            until = time.monotonic() + 10
            while time.monotonic() < until:
                try:
                    os.kill(worker, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            with self.assertRaises(subprocess.TimeoutExpired):
                subprocess.run(['sh', str(SCRIPTS / 'wait-workers.sh'), str(self.log('r1'))],
                               env=self.f.env, capture_output=True, timeout=2)
        finally:
            os.kill(supervisor, signal.SIGCONT)
        self.wait_for(self.side('r1', '.exit'))
        done = subprocess.run(['sh', str(SCRIPTS / 'wait-workers.sh'), str(self.log('r1'))],
                              env=self.f.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(done.stdout.strip(), 'all workers finished')


if __name__ == '__main__':
    unittest.main()
