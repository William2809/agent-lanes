"""Local runner fixtures. Real shlock/Git/process groups; stub suite, no database or SSH."""
import base64
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
KIT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('failure', KIT / 'bin/fail-summary.py')
failure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(failure)


@unittest.skipUnless(Path('/usr/bin/shlock').exists() and sys.platform == 'darwin',
                     'runner fixture uses macOS shlock and time -l')
class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='remote-ci-runner-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ci = self.home / 'ci'
        self.dir = self.ci / 'projects/fixture'
        for name in ('logs', 'results', 'slots'):
            (self.dir / name).mkdir(parents=True)
        self.bin = self.ci / 'bin'
        shutil.copytree(KIT / 'remote', self.bin)
        self.env = dict(os.environ, HOME=str(self.home))
        self.procs = []
        self.addCleanup(self.stop_runs)
        self.source = self.home / 'source'
        self.source.mkdir()
        self.git('-C', self.source, 'init', '-q')
        (self.source / 'source.txt').write_text('fixture\n')
        self.git('-C', self.source, 'add', 'source.txt')
        self.git('-C', self.source, '-c', 'user.name=Fixture', '-c',
                 'user.email=user@example.invalid', 'commit', '-qm', 'fixture')
        self.sha = self.git('-C', self.source, 'rev-parse', 'HEAD')
        self.tree = self.git('-C', self.source, 'rev-parse', 'HEAD^{tree}') + '-conf-kit'
        self.git('init', '--bare', '-q', self.dir / 'repo.git')
        (self.home / 'check').write_text('''#!/bin/bash
printf 'run\\n' >>"$HOME/checks"
touch "$HOME/started-$$"
while [ ! -f "$HOME/release" ]; do sleep 0.05; done
[ "$FIXTURE_RC" = 0 ] || echo 'FAIL src/check.test.ts > fixture'
exit "$FIXTURE_RC"
''')
        self.configure()

    def git(self, *args):
        return subprocess.run(['git', *map(str, args)], env=self.env, capture_output=True,
                              text=True, check=True).stdout.strip()

    def configure(self, rc=0, slots=1):
        (self.dir / 'conf').write_text(
            'REMOTE_CI_CHECK=\'bash "$HOME/check"\'\nREMOTE_CI_TIMEOUT=20\nREMOTE_CI_WAIT_TIMEOUT=100\n'
            f'REMOTE_CI_SLOTS={slots}\nexport FIXTURE_RC={rc}\n')

    def stop_runs(self):
        (self.home / 'release').touch()
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def start(self, run, force=False, command=''):
        self.git('-C', self.dir / 'repo.git', 'fetch', '-q', self.source,
                 f'HEAD:refs/heads/run/{run}')
        encoded = base64.b64encode(command.encode()).decode()
        proc = subprocess.Popen(['/bin/bash', str(self.bin / 'run.sh'), 'fixture',
                                 self.sha, self.tree, run, encoded, str(int(force))],
                                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail('fixture event did not arrive within 10 seconds')

    def log(self, run):
        path = self.dir / 'logs' / (run + '.log')
        return path.read_text() if path.exists() else ''

    def count(self):
        path = self.home / 'checks'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def assert_result(self, proc, run, rc):
        self.assertEqual(proc.wait(timeout=10), rc)
        self.assertEqual((self.dir / 'results' / (run + '.exit')).read_text().strip(), str(rc))

    def test_pass_appears_while_queued(self):
        (self.ci / 'lock').write_text(str(os.getpid()))
        proc = self.start('queued')
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        self.wait_for(lambda: claim.with_name(claim.name + '.run').exists())
        marker = self.dir / 'results' / ('tree-' + self.tree + '.pass')
        marker.write_text('previous-success\n')
        (self.ci / 'lock').unlink()
        self.assert_result(proc, 'queued', 0)
        self.assertIn('== cached: identical tree passed while queued', self.log('queued'))
        self.assertEqual(self.count(), 0)
        self.assertEqual(marker.read_text(), 'previous-success\n')
        self.assertFalse(claim.exists())

    def test_identical_runs_follow_success_and_failure_without_a_slot(self):
        for rc, slots in ((0, 1), (7, 2)):
            with self.subTest(rc=rc, slots=slots):
                self.configure(rc, slots)
                # Use a different tree key for each case, so a prior pass cannot hide a run.
                self.tree += f'-{rc}'
                release = self.home / 'release'
                release.unlink(missing_ok=True)
                before = self.count()
                owner_id = f'owner-{self.sha[:8]}-{rc}'
                follower_id = f'follower-{self.sha[:8]}-{rc}'
                owner = self.start(owner_id)
                self.wait_for(lambda: self.count() == before + 1)
                follower = self.start(follower_id)
                self.wait_for(lambda: '== following run ' + owner_id in self.log(follower_id))
                self.assertEqual(self.count(), before + 1)
                self.assertFalse((self.dir / 'slots/2.lock').exists())
                status = subprocess.run(['/bin/bash', str(self.bin / 'status.sh'), 'fixture',
                                         'fixture', str(slots)], env=self.env,
                                        capture_output=True, text=True, check=True).stdout
                self.assertIn('slot 1: busy', status)
                release.touch()
                self.assert_result(owner, owner_id, rc)
                self.assert_result(follower, follower_id, rc)
                self.assertEqual(self.count(), before + 1)
                claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
                self.assertFalse(claim.exists())
                self.assertFalse(claim.with_name(claim.name + '.run').exists())
                marker = self.dir / 'results' / ('tree-' + self.tree + '.pass')
                self.assertEqual(marker.exists(), rc == 0)
                if rc:
                    self.assertIn(f'Error: followed run rc={rc} (log ', self.log(follower_id))
                    # Force the follower to be latest even on a coarse filesystem clock.
                    os.utime(self.dir / 'logs' / (follower_id + '.log'),
                             ns=(time.time_ns() + 1000000,) * 2)
                    result = failure.latest(self.dir / 'logs', self.sha)
                    self.assertEqual(result['files'], [{'file': 'src/check.test.ts', 'package': ''}])
                    self.assertTrue(result['path'].endswith(owner_id + '.log'))
                    self.assertIn('FAIL src/check.test.ts', '\n'.join(
                        failure.latest(self.dir / 'logs', self.sha, tail=True)['summary']))

    def live_owner(self):
        path = self.home / 'owner/run.sh'
        path.parent.mkdir(exist_ok=True)
        path.write_text('#!/bin/bash\nwhile :; do sleep 1; done\n')
        proc = subprocess.Popen(['/bin/bash', str(path)], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def fake_clock(self):
        stubs = self.ci / 'node/fixture/bin'
        stubs.mkdir(parents=True)
        date = stubs / 'date'
        date.write_text('#!/bin/bash\nif [ "$1" = +%s ]; then value=$(cat "$HOME/clock"); '
                        'printf "%s\\n" "$value" >>"$HOME/clock-reads"; printf "%s\\n" "$value"; '
                        'else exec /bin/date "$@"; fi\n')
        date.chmod(0o755)
        with (self.dir / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_NODE=fixture\n')
        self.stub('sleep', 'touch "$HOME/waiting-$PPID"\nexec /bin/sleep "$@"\n')
        self.advance_clock(0)

    def clock_reads(self):
        path = self.home / 'clock-reads'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def advance_clock(self, seconds):
        new = self.home / 'clock.new'
        new.write_text(str(seconds) + '\n')
        new.replace(self.home / 'clock')

    def test_reused_pid_missing_metadata_and_live_follow_have_deadlines(self):
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        metadata = claim.with_name(claim.name + '.run')
        claim.write_text(str(os.getpid()))  # A live Python process, not run.sh.
        metadata.write_text(f'{os.getpid()} reused-owner\n')
        (self.home / 'release').touch()
        reused = self.start('reused-pid')
        self.assert_result(reused, 'reused-pid', 0)
        (self.home / 'release').unlink()
        self.assertNotIn('following run', self.log('reused-pid'))
        self.assertIn('stale tree claim', self.log('reused-pid'))
        self.assertEqual(self.count(), 1)
        owner = self.live_owner()
        claim.write_text(str(owner.pid))
        metadata.unlink(missing_ok=True)
        self.fake_clock()
        missing = self.start('missing-metadata')
        self.wait_for(lambda: (self.home / f'waiting-{missing.pid}').exists())
        self.advance_clock(100)
        self.assert_result(missing, 'missing-metadata', 75)
        self.assertIn('total deadline', self.log('missing-metadata'))
        self.advance_clock(0)
        metadata.write_text(f'{owner.pid} stalled-owner\n')
        before = self.clock_reads()
        follower = self.start('stalled-follower')
        self.wait_for(lambda: 'following run stalled-owner' in self.log('stalled-follower'))
        self.wait_for(lambda: self.clock_reads() >= before + 4)
        self.advance_clock(100)
        self.assert_result(follower, 'stalled-follower', 75)
        self.assertIn('total wait exceeded 100 seconds', self.log('stalled-follower'))
        self.assertEqual(self.count(), 1)
        self.assertFalse((self.ci / 'lock').exists())
        self.assertTrue(claim.exists())

    def test_follower_total_deadline_is_separate_and_includes_claim_wait(self):
        owner = self.live_owner()
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        claim.write_text(str(owner.pid))
        metadata = claim.with_name(claim.name + '.run')
        self.fake_clock()
        with (self.dir / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_WAIT_TIMEOUT=100\n')
        follower = self.start('total-follower')
        self.wait_for(lambda: (self.home / f'waiting-{follower.pid}').exists())
        # Time spent awaiting the claim metadata must count toward the same deadline.
        self.advance_clock(70)
        metadata.write_text(f'{owner.pid} total-owner\n')
        self.wait_for(lambda: 'following run total-owner' in self.log('total-follower'))
        before = self.clock_reads()
        self.advance_clock(95)  # Follow time > command timeout, total time < wait timeout.
        self.wait_for(lambda: self.clock_reads() > before)
        time.sleep(0.2)
        self.assertIsNone(follower.poll(), 'per-command timeout must not limit total wait')
        self.advance_clock(100)
        self.assert_result(follower, 'total-follower', 75)
        self.assertIn('total wait exceeded 100 seconds', self.log('total-follower'))
        self.assertTrue(claim.exists())
        self.assertEqual(self.count(), 0)

    def test_claim_metadata_must_match_live_pid(self):
        owner = self.live_owner()
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        claim.write_text(str(owner.pid))
        metadata = claim.with_name(claim.name + '.run')
        metadata.write_text('0 previous-owner\n')
        proc = self.start('metadata-follower')
        self.wait_for(lambda: (self.dir / 'logs/metadata-follower.log').exists())
        self.assertNotIn('following run', self.log('metadata-follower'))
        self.assertEqual(self.count(), 0)
        metadata.write_text(f'{owner.pid} published-owner\n')
        self.wait_for(lambda: 'following run published-owner' in self.log('metadata-follower'))
        self.assertFalse((self.ci / 'lock').exists())
        (self.dir / 'results/published-owner.exit').write_text('9\n')
        self.assert_result(proc, 'metadata-follower', 9)
        self.assertEqual(self.count(), 0)
        self.assertTrue(claim.exists())  # A follower must not release the owner's claim.

    @staticmethod
    def group_live(pid):
        try:
            os.killpg(pid, 0)
            return True
        except ProcessLookupError:
            return False

    @staticmethod
    def stop_group(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def test_sigkilled_owner_live_child_blocks_reclaim_until_child_exits(self):
        for slots in (1, 2):
            with self.subTest(slots=slots):
                self.configure(slots=slots)
                self.tree += f'-orphan-{slots}'
                release = self.home / 'release'
                release.unlink(missing_ok=True)
                before = self.count()
                run = f'killed-owner-{slots}'
                owner = self.start(run)
                self.wait_for(lambda: self.count() == before + 1)
                marker = self.ci / 'lock.child' if slots == 1 else self.dir / 'slots/1.lock.child'
                self.wait_for(marker.exists)
                child = int(marker.read_text())
                children = subprocess.run(['pgrep', '-P', str(owner.pid)],
                                          capture_output=True, text=True, check=True)
                for pid in map(int, children.stdout.split()):
                    self.addCleanup(self.stop_group, pid)
                owner.kill()
                owner.wait(timeout=5)
                self.assertTrue(self.group_live(child))
                for retry in ('first', 'again'):
                    attempt = f'{retry}-after-kill-{slots}'
                    proc = self.start(attempt)
                    self.assert_result(proc, attempt, 75)
                    self.assertIn('still has a live slot child', self.log(attempt))
                    self.assertEqual(self.count(), before + 1)
                    self.assertFalse((self.dir / 'slots/2.lock').exists())
                release.touch()
                self.wait_for(lambda: not self.group_live(child))
                recovered = f'recovered-{slots}'
                proc = self.start(recovered)
                self.assert_result(proc, recovered, 0)
                self.assertEqual(self.count(), before + 2)

    def stub(self, name, body):
        stubs = self.ci / 'node/fixture/bin'
        stubs.mkdir(parents=True, exist_ok=True)
        path = stubs / name
        path.write_text('#!/bin/bash\n' + body)
        path.chmod(0o755)
        with (self.dir / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_NODE=fixture\n')
        return path

    def test_stale_reclaimers_serialize_inspection_and_acquisition(self):
        self.configure(slots=2)
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        claim.write_text(str(os.getpid()))
        claim.with_name(claim.name + '.run').write_text(f'{os.getpid()} stale\n')
        self.stub('rm', '''for arg in "$@"; do
  if [ "$arg" = "$HOME/ci/projects/fixture/results/tree-$FIXTURE_TREE.running" ] &&
     mkdir "$HOME/remove-once" 2>/dev/null; then
    touch "$HOME/remove-paused"
    while [ ! -f "$HOME/release-remove" ]; do /bin/sleep 0.05; done
  fi
done
exec /bin/rm "$@"
''')
        self.stub('sleep', 'touch "$HOME/waiting-$PPID"\nexec /bin/sleep "$@"\n')
        self.env['FIXTURE_TREE'] = self.tree
        self.addCleanup(lambda: (self.home / 'release-remove').touch())
        a = self.start('reclaimer-a')
        self.wait_for(lambda: (self.home / 'remove-paused').exists())
        b = self.start('reclaimer-b')
        self.wait_for(lambda: self.count() or (self.home / f'waiting-{b.pid}').exists())
        try:
            self.assertEqual(self.count(), 0, 'second reclaimer entered before inspection lock released')
        finally:
            (self.home / 'release-remove').touch()
        self.wait_for(lambda: self.count() == 1)
        self.wait_for(lambda: 'following run reclaimer-a' in self.log('reclaimer-b'))
        (self.home / 'release').touch()
        self.assert_result(a, 'reclaimer-a', 0)
        self.assert_result(b, 'reclaimer-b', 0)
        self.assertEqual(self.count(), 1)

    def test_finisher_preserves_replacement_claim(self):
        owner = self.start('old-finisher')
        self.wait_for(lambda: self.count() == 1)
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        metadata = claim.with_name(claim.name + '.run')
        claim.write_text(str(os.getpid()))
        metadata.write_text(f'{os.getpid()} replacement\n')
        (self.home / 'release').touch()
        self.assert_result(owner, 'old-finisher', 0)
        self.assertEqual(metadata.read_text(), f'{os.getpid()} replacement\n')
        self.assertEqual(claim.read_text(), str(os.getpid()))

    def test_unobservable_owner_keeps_claim_and_retry_returns_busy(self):
        owner = self.live_owner()
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        metadata = claim.with_name(claim.name + '.run')
        (self.home / 'release').touch()
        for status in (0, 1):
            with self.subTest(ps_status=status):
                claim.write_text(str(owner.pid))
                metadata.write_text(f'{owner.pid} hidden-owner\n')
                self.stub('ps', f'exit {status}\n')  # Empty output or inspection error.
                run = f'hidden-retry-{status}'
                retry = self.start(run)
                self.assert_result(retry, run, 75)
                self.assertEqual(self.count(), 0)
                self.assertEqual(metadata.read_text(), f'{owner.pid} hidden-owner\n')
                self.assertIn('cannot inspect owner', self.log(run))

    def test_attached_follower_waits_when_owner_becomes_unobservable(self):
        owner = self.live_owner()
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        claim.write_text(str(owner.pid))
        claim.with_name(claim.name + '.run').write_text(f'{owner.pid} visible-owner\n')
        self.stub('ps', '''if [ -f "$HOME/hide-ps" ]; then
  touch "$HOME/hidden-inspection"
  exit 1
fi
exec /bin/ps "$@"
''')
        follower = self.start('hidden-follower')
        self.wait_for(lambda: 'following run visible-owner' in self.log('hidden-follower'))
        (self.home / 'hide-ps').touch()
        self.wait_for(lambda: (self.home / 'hidden-inspection').exists())
        time.sleep(0.2)
        self.assertIsNone(follower.poll(), 'hidden ps must not end an attached follower')
        (self.dir / 'results/visible-owner.exit').write_text('7\n')
        self.assert_result(follower, 'hidden-follower', 7)
        self.assertEqual(self.count(), 0)
        self.assertTrue(claim.exists())

    def test_cleanup_guard_wait_is_bounded_and_preserves_other_owner(self):
        self.configure(slots=2)
        self.fake_clock()
        self.stub('sleep', 'touch "$HOME/waiting-$PPID"\nexec /bin/sleep "$@"\n')
        owner = self.start('guard-blocked')
        self.wait_for(lambda: self.count() == 1)
        guard = self.ci / 'lock'
        guard.write_text(str(os.getpid()))  # Reused PID remains live indefinitely.
        (self.home / 'release').touch()
        self.wait_for(lambda: (self.home / f'waiting-{owner.pid}').exists())
        self.advance_clock(60)
        self.assertEqual(owner.wait(timeout=3), 74)
        self.assertEqual((self.dir / 'results/guard-blocked.exit').read_text().strip(), '74')
        self.assertIn('lost-run:', self.log('guard-blocked'))
        self.assertIn('cleanup guard', self.log('guard-blocked'))
        self.assertEqual(guard.read_text(), str(os.getpid()))
        self.assertTrue((self.dir / 'slots/1.lock').exists())
        self.assertTrue((self.dir / 'slots/1.run').exists())

    def test_dead_followed_owner_without_result_reports_lost_run(self):
        owner = self.live_owner()
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        claim.write_text(str(owner.pid))
        claim.with_name(claim.name + '.run').write_text(f'{owner.pid} lost-owner\n')
        follower = self.start('lost-follower')
        self.wait_for(lambda: 'following run lost-owner' in self.log('lost-follower'))
        owner.kill()
        owner.wait(timeout=5)
        self.assert_result(follower, 'lost-follower', 74)
        self.assertIn('lost-run: followed run lost-owner', self.log('lost-follower'))
        self.assertTrue(claim.exists())

    def test_initial_claim_guard_respects_shorter_total_deadline(self):
        self.fake_clock()
        self.stub('sleep', 'touch "$HOME/waiting-$PPID"\nexec /bin/sleep "$@"\n')
        with (self.dir / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_WAIT_TIMEOUT=20\n')
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        guard = claim.with_name(claim.name + '.guard')
        guard.write_text(str(os.getpid()))
        follower = self.start('short-guard-wait')
        self.wait_for(lambda: (self.home / f'waiting-{follower.pid}').exists())
        self.advance_clock(20)
        self.assertEqual(follower.wait(timeout=3), 75)
        self.assertEqual((self.dir / 'results/short-guard-wait.exit').read_text().strip(), '75')
        self.assertEqual(guard.read_text(), str(os.getpid()))
        self.assertEqual(self.count(), 0)

    def test_claim_release_guard_wait_is_bounded(self):
        self.fake_clock()
        self.stub('sleep', 'touch "$HOME/waiting-$PPID"\nexec /bin/sleep "$@"\n')
        owner = self.start('claim-guard-blocked')
        self.wait_for(lambda: self.count() == 1)
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        guard = claim.with_name(claim.name + '.guard')
        guard.write_text(str(os.getpid()))
        (self.home / 'release').touch()
        self.wait_for(lambda: (self.home / f'waiting-{owner.pid}').exists())
        self.advance_clock(60)
        self.assert_result(owner, 'claim-guard-blocked', 74)
        self.assertIn('claim release guard', self.log('claim-guard-blocked'))
        self.assertTrue(claim.exists())
        self.assertEqual(guard.read_text(), str(os.getpid()))

    def test_terminal_publication_failure_cannot_report_success(self):
        (self.home / 'release').touch()
        for mode in ('rename', 'write'):
            with self.subTest(mode=mode):
                self.tree += '-' + mode
                run = 'unpublished-' + mode
                if mode == 'rename':
                    self.stub('mv', '''case "$1" in *.exit.new) exit 1 ;; esac
exec /bin/mv "$@"
''')
                else:
                    self.stub('mv', 'exec /bin/mv "$@"\n')
                    (self.dir / 'results' / (run + '.exit.new')).mkdir()
                owner = self.start(run)
                self.assertEqual(owner.wait(timeout=10), 74)
                self.assertIn('lost-run:', self.log(run))
                self.assertFalse((self.dir / 'results' / (run + '.exit')).exists())
                self.assertFalse((self.dir / 'results' / ('tree-' + self.tree + '.pass')).exists())

    def test_stale_claim_is_replaced(self):
        dead = subprocess.Popen(['/bin/bash', '-c', 'exit 0'])
        dead.wait()
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        claim.write_text(str(dead.pid))
        claim.with_name(claim.name + '.run').write_text(f'{dead.pid} obsolete\n')
        (self.home / 'release').touch()
        proc = self.start('fresh')
        self.assert_result(proc, 'fresh', 0)
        self.assertEqual(self.count(), 1)
        self.assertNotIn('following run', self.log('fresh'))
        self.assertFalse(claim.exists())

    def test_force_and_custom_commands_bypass_pass_and_claim(self):
        self.configure(slots=2)
        owner = self.start('normal')
        self.wait_for(lambda: self.count() == 1)
        marker = self.dir / 'results' / ('tree-' + self.tree + '.pass')
        marker.write_text('cached\n')
        for run, force, command in (('forced', True, ''),
                                    ('custom', False, 'bash "$HOME/check"')):
            before = self.count()
            proc = self.start(run, force=force, command=command)
            self.wait_for(lambda: self.count() == before + 1)
            self.assertNotIn('following run', self.log(run))
            self.assertNotIn('== cached:', self.log(run))
            (self.home / 'release').touch()
            self.assert_result(proc, run, 0)
            self.assert_result(owner, 'normal', 0)
            (self.home / 'release').unlink()
        # Custom commands do not publish full-suite pass markers.
        self.assertEqual(marker.read_text().strip(), self.sha)
        # Force and custom pairs must execute twice when no normal claim exists.
        for prefix, force, command in (('force', True, ''),
                                        ('custom', False, 'bash "$HOME/check"')):
            before = self.count()
            a = self.start(prefix + '-a', force=force, command=command)
            b = self.start(prefix + '-b', force=force, command=command)
            self.wait_for(lambda: self.count() == before + 2)
            (self.home / 'release').touch()
            self.assert_result(a, prefix + '-a', 0)
            self.assert_result(b, prefix + '-b', 0)
            (self.home / 'release').unlink()


class ClientFixture(unittest.TestCase):
    """Full client flow; SSH and Git push are local stubs, with no real runner."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='remote-ci-client-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / 'source'
        self.root.mkdir()
        self.kit = self.home / 'kit'
        shutil.copytree(KIT / 'remote', self.kit / 'remote')
        (self.kit / 'bin').mkdir()
        self.client = self.kit / 'bin/remote-ci'
        shutil.copyfile(KIT / 'bin/remote-ci', self.client)
        config = self.home / 'config'
        config.write_text('REMOTE_CI_HOST=fixture\nREMOTE_CI_DIRECT_HOST=fixture-direct\n')
        (self.root / '.remote-ci.conf').write_text('REMOTE_CI_NAME=fixture\nREMOTE_CI_WAIT_TIMEOUT=100\n')
        self.dir = self.home / 'ci/projects/fixture'
        for name in ('logs', 'results', 'runs'):
            (self.dir / name).mkdir(parents=True)
        (self.dir / '.ready').touch()
        stubs = self.home / 'stubs'
        stubs.mkdir()
        scripts = {
            'git': f'#!{sys.executable}\n' + '''import os, sys
if sys.argv[1:2] == ['push']:
    open(os.environ['HOME'] + '/push-stub', 'a').write('push\\n')
    sys.exit(0)
os.execv('/usr/bin/git', ['git', *sys.argv[1:]])
''',
            'ssh': f'#!{sys.executable}\n' + '''import os, re, sys, time
from pathlib import Path
command = sys.argv[-1]
if os.environ['FIXTURE_CLIENT_MODE'] == 'blocked-transport' and 'done=' in command:
    Path(os.environ['HOME'] + '/client-waiting').touch()
    while True: time.sleep(60)
if command.startswith('nohup bash '):
    run = re.search(r'/runs/([^/]+)/bin/run.sh', command).group(1)
    home = Path(os.environ['HOME'])
    if os.environ['FIXTURE_CLIENT_MODE'] == 'killed-follower':
        (home / 'ci/projects/fixture/logs' / (run + '.log')).write_text(
            '== following run killed-owner sha fixture\\n')
    if os.environ['FIXTURE_CLIENT_MODE'] == 'completed':
        (home / 'ci/projects/fixture/logs' / (run + '.log')).write_text(
            'FAIL fixture\\n== finished rc=7\\n')
        (home / 'ci/projects/fixture/results' / (run + '.exit')).write_text('7\\n')
    (home / 'submitted').write_text(run)
    sys.exit(0)
os.execv('/bin/bash', ['bash', '-c', command])
''',
            'date': '#!/bin/bash\nif [ "$1" = +%s ]; then cat "$HOME/clock"; '
                    'else exec /bin/date "$@"; fi\n',
            'sleep': '#!/bin/bash\ntouch "$HOME/client-waiting"\nexec /bin/sleep 0.05\n',
        }
        for name, body in scripts.items():
            path = stubs / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), AGENT_LANES_CONFIG=str(config),
                        PATH=str(stubs) + os.pathsep + os.environ['PATH'],
                        FIXTURE_CLIENT_MODE='no-log')
        self.clock(0)
        def git(*args):
            subprocess.run(['/usr/bin/git', *args], cwd=self.root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git('init', '-q')
        git('add', '.remote-ci.conf')
        git('-c', 'user.name=Fixture', '-c', 'user.email=user@example.invalid',
            'commit', '-qm', 'fixture')
        self.procs = []
        self.addCleanup(self.stop_clients)

    def clock(self, seconds):
        path = self.home / 'clock.new'
        path.write_text(str(seconds) + '\n')
        path.replace(self.home / 'clock')

    def stop_clients(self):
        for proc in self.procs:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)

    def start(self, summary=False):
        proc = subprocess.Popen(['/bin/bash', str(self.client), 'check',
                                 *(['--summary'] if summary else [])], cwd=self.root,
                                env=self.env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        self.procs.append(proc)
        return proc

    def wait_for(self, condition):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.02)
        self.fail('client fixture did not reach wait')

    def fixed_run_prefix(self):
        # Model two machines with the same second, commit and local PID.
        self.client.write_text(self.client.read_text().replace('${sha:0:8}-$$', '${sha:0:8}-4242'))
        date = self.home / 'stubs/date'
        date.write_text('#!/bin/bash\ncase "$1" in\n'
                        '+%s) cat "$HOME/clock" ;;\n'
                        '+%Y%m%d-%H%M%S) echo 20000101-000000 ;;\n'
                        '*) exec /bin/date "$@" ;;\nesac\n')
        self.env['FIXTURE_CLIENT_MODE'] = 'completed'

    def test_run_ids_differ_with_same_time_commit_and_pid(self):
        self.fixed_run_prefix()
        runs = []
        for _ in range(2):
            proc = self.start(summary=True)
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 7, err)
            runs.append((self.home / 'submitted').read_text())
        self.assertEqual(len(set(runs)), 2, 'same time/commit/PID must not share a run')
        for run in runs:
            self.assertRegex(run, r'^20000101-000000-[0-9a-f]{8}-4242-[0-9a-f]{24}$')
        self.assertEqual(len(list((self.dir / 'runs').iterdir())), 2)

    def test_existing_run_directory_is_not_shared_or_overwritten(self):
        self.fixed_run_prefix()
        # Force even the random suffix to collide, to test the exclusive mkdir.
        random = self.home / 'stubs/openssl'
        random.write_text('#!/bin/bash\necho 00112233445566778899aabb\n')
        random.chmod(0o755)
        first = self.start(summary=True)
        out, err = first.communicate(timeout=5)
        self.assertEqual(first.returncode, 7, err)
        run = (self.home / 'submitted').read_text()
        conf = self.dir / 'runs' / run / 'conf'
        conf.write_text('keep existing run config\n')
        pushes = (self.home / 'push-stub').read_text()
        second = self.start(summary=True)
        out, err = second.communicate(timeout=5)
        self.assertEqual(second.returncode, 1, err)
        self.assertIn('cannot reserve run directory', out)
        self.assertEqual(conf.read_text(), 'keep existing run config\n')
        self.assertEqual((self.home / 'push-stub').read_text(), pushes)
        self.assertEqual(len(list((self.dir / 'runs').iterdir())), 1)

    def test_client_returns_published_result_in_stream_and_summary_modes(self):
        self.env['FIXTURE_CLIENT_MODE'] = 'completed'
        for summary in (False, True):
            with self.subTest(summary=summary):
                proc = self.start(summary)
                out, err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 7, err)
                self.assertIn('FAIL fixture', out)
                self.assertIn('== finished rc=7', out)
                self.assertNotIn('lost-run:', out)

    def test_client_total_deadline_bounds_missing_log_and_killed_follower(self):
        for mode in ('no-log', 'killed-follower', 'blocked-transport'):
            for summary in (False, True):
                with self.subTest(mode=mode, summary=summary):
                    self.clock(0)
                    self.env['FIXTURE_CLIENT_MODE'] = mode
                    (self.home / 'client-waiting').unlink(missing_ok=True)
                    proc = self.start(summary)
                    self.wait_for(lambda: (self.home / 'client-waiting').exists())
                    self.clock(100)
                    out, err = proc.communicate(timeout=3)
                    self.assertEqual(proc.returncode, 74, err)
                    self.assertIn('lost-run: run ', out)
                    self.assertIn('total wait exceeded 100 seconds', out)
                    run = (self.home / 'submitted').read_text()
                    self.assertFalse((self.dir / 'results' / (run + '.exit')).exists())


class KitRevision(unittest.TestCase):
    def test_uploaded_kit_matches_hash_when_working_scripts_change_during_sync(self):
        with tempfile.TemporaryDirectory(prefix='remote-ci-kit-race-') as tmp:
            home = Path(tmp)
            root = home / 'source'
            root.mkdir()
            kit = home / 'kit'
            shutil.copytree(KIT / 'remote', kit / 'remote')
            (kit / 'bin').mkdir()
            client = kit / 'bin/remote-ci'
            shutil.copyfile(KIT / 'bin/remote-ci', client)
            # Stop locally before push. Setup must never touch a real runner.
            (kit / 'remote/setup.sh').write_text('#!/bin/bash\nexit 91\n')
            original = (kit / 'remote/run.sh').read_bytes()
            config = home / 'config'
            config.write_text('REMOTE_CI_HOST=local\n')
            (root / '.remote-ci.conf').write_text('REMOTE_CI_NAME=fixture\n')
            stubs = home / 'stubs'
            stubs.mkdir()
            tar = stubs / 'tar'
            tar.write_text('#!/bin/bash\nif [ ! -f "$HOME/mutated" ]; then '
                           'printf "\\n# changed while syncing\\n" >>"$HOME/kit/remote/run.sh"; '
                           'touch "$HOME/mutated"; fi\nexec /usr/bin/tar "$@"\n')
            tar.chmod(0o755)
            git_stub = stubs / 'git'
            git_stub.write_text(f'#!{sys.executable}\n' + """import os, subprocess, sys
args = sys.argv[1:]
if args[:2] == ['hash-object', '--stdin']:
    data = sys.stdin.buffer.read()
    with open(os.environ['HOME'] + '/hashed-kit', 'wb') as log: log.write(data)
    sys.exit(subprocess.run(['/usr/bin/git', *args], input=data).returncode)
if args[:1] == ['push']: sys.exit(98)
os.execv('/usr/bin/git', ['git', *args])
""")
            git_stub.chmod(0o755)
            env = dict(os.environ, HOME=tmp, AGENT_LANES_CONFIG=str(config),
                       PATH=str(stubs) + os.pathsep + os.environ['PATH'])
            def git(*args):
                return subprocess.run(['/usr/bin/git', *map(str, args)], cwd=root,
                                      capture_output=True, text=True, check=True).stdout.strip()
            git('init', '-q')
            git('add', '.remote-ci.conf')
            git('-c', 'user.name=Fixture', '-c', 'user.email=user@example.invalid',
                'commit', '-qm', 'fixture')
            expected = ''.join(script.name + '\n' + git('hash-object', script) + '\n'
                               for script in sorted((kit / 'remote').glob('*.sh')))
            call = subprocess.run(['/bin/bash', str(client), 'check'], cwd=root,
                                  env=env, capture_output=True, text=True)
            self.assertEqual(call.returncode, 1)  # Stub setup failed before any push.
            self.assertTrue((home / 'mutated').exists())
            self.assertEqual((home / 'hashed-kit').read_text(), expected)
            self.assertNotEqual((kit / 'remote/run.sh').read_bytes(), original)
            copies = [home / 'ci/bin/run.sh', *home.glob('ci/projects/fixture/runs/*/bin/run.sh')]
            self.assertEqual(len(copies), 2)
            for copy in copies:
                self.assertEqual(copy.read_bytes(), original)

    def test_client_cache_key_changes_with_synced_runner_scripts(self):
        with tempfile.TemporaryDirectory(prefix='remote-ci-key-') as tmp:
            home = Path(tmp)
            root = home / 'source'
            root.mkdir()
            kit = home / 'kit'
            shutil.copytree(KIT / 'remote', kit / 'remote')
            (kit / 'bin').mkdir()
            client = kit / 'bin/remote-ci'
            shutil.copyfile(KIT / 'bin/remote-ci', client)
            config = home / 'config'
            config.write_text('REMOTE_CI_HOST=local\n')
            (root / '.remote-ci.conf').write_text('REMOTE_CI_NAME=fixture\n')
            stubs = home / 'stubs'
            stubs.mkdir()
            # Stop every cache miss at sync. No setup, push or suite can run.
            tar = stubs / 'tar'
            tar.write_text('#!/bin/bash\n: >"$HOME/sync-attempted"\nexit 91\n')
            tar.chmod(0o755)
            env = dict(os.environ, HOME=tmp, AGENT_LANES_CONFIG=str(config),
                       PATH=str(stubs) + os.pathsep + os.environ['PATH'])

            def git(*args, input=None):
                return subprocess.run(['git', *map(str, args)], cwd=root, env=env,
                                      input=input, capture_output=True, text=True,
                                      check=True).stdout.strip()

            git('init', '-q')
            git('add', '.remote-ci.conf')
            git('-c', 'user.name=Fixture', '-c', 'user.email=user@example.invalid',
                'commit', '-qm', 'fixture')
            prefix = git('rev-parse', 'HEAD^{tree}') + '-' + git(
                'hash-object', root / '.remote-ci.conf')[:8]
            results = home / 'ci/projects/fixture/results'
            results.mkdir(parents=True)
            (results / ('tree-' + prefix + '.pass')).touch()  # Old two-part format.

            def key():
                blobs = ''.join(script.name + '\n' + git('hash-object', script) + '\n'
                                for script in sorted((kit / 'remote').glob('*.sh')))
                return prefix + '-' + git('hash-object', '--stdin', input=blobs)[:8]

            def check(hit):
                attempt = home / 'sync-attempted'
                attempt.unlink(missing_ok=True)
                call = subprocess.run(['/bin/bash', str(client), 'check'], cwd=root,
                                      env=env, capture_output=True, text=True)
                self.assertEqual(call.returncode, 0 if hit else 1)
                self.assertEqual('already passed' in call.stdout, hit)
                self.assertEqual(attempt.exists(), not hit)

            check(False)  # A pass from the old key format is not reused.
            original = key()
            self.assertRegex(original, r'^[0-9a-f]{40}-[0-9a-f]{8}-[0-9a-f]{8}$')
            (results / ('tree-' + original + '.pass')).touch()
            check(True)
            for name in ('run.sh', 'lib.sh'):
                with (kit / 'remote' / name).open('a') as script:
                    script.write('\n# fixture revision change\n')
                updated = key()
                self.assertNotEqual(original, updated)
                check(False)
                (results / ('tree-' + updated + '.pass')).touch()
                check(True)
                original = updated


class Summary(unittest.TestCase):
    def test_follower_summary_filters_owner_log_with_the_same_pattern(self):
        # Run the production summary block locally, without sync, push or SSH.
        client = (KIT / 'bin/remote-ci').read_text()
        body = client.split('if [ "$summary" = 1 ]; then\n', 1)[1].split('\nfi\nwait_remote ', 1)[0]
        with tempfile.TemporaryDirectory(prefix='remote-ci-summary-') as tmp:
            root = Path(tmp)
            (root / 'logs').mkdir()
            (root / 'results').mkdir()
            owner = root / 'logs/owner.log'
            owner.write_text('pkg:test: FAIL src/check.test.ts > failing case\n'
                             'Tests  1 failed\nowner-only-marker\n== finished rc=7\n')
            follower = root / 'logs/follower.log'
            follower.write_text('== following run owner sha ' + 'a' * 40 + '\n'
                                'Error: followed run rc=7\n== finished rc=7\n')
            (root / 'results/follower.exit').write_text('7\n')
            script = ('p=$SUMMARY_DIR; run=follower\n'
                      'await_file() { [ -f "$1" ]; }; export -f await_file\n'
                      'wait_remote() { /bin/bash -c "$1"; }\n') + body
            env = dict(os.environ, SUMMARY_DIR=tmp)
            env.pop('REMOTE_CI_SUMMARY_PATTERN', None)
            for pattern in ('', 'owner-only-marker'):
                with self.subTest(pattern=pattern):
                    env['REMOTE_CI_SUMMARY_PATTERN'] = pattern
                    result = subprocess.run(['/bin/bash', '-c', script], env=env,
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 7)
                    self.assertIn('== following run owner', result.stdout)
                    self.assertEqual(result.stdout.count('== finished rc=7'), 1)
                    self.assertEqual('FAIL src/check.test.ts' in result.stdout, not bool(pattern))
                    self.assertEqual('owner-only-marker' in result.stdout, bool(pattern))
            # A pruned owner log must not hide the follower's result or event.
            owner.unlink()
            result = subprocess.run(['/bin/bash', '-c', script], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 7)
            self.assertIn('== following run owner', result.stdout)
            self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
