"""Local runner fixtures. Real shlock/Git/process groups; stub suite, no database or SSH."""
# Allow 60 s for fixture scheduling at host load 40-140; product deadlines stay separate.
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from fixture_tools import isolate_corepack

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
        isolate_corepack(self.bin, self.home)
        # These fixtures test slot/claim semantics, independently of real host load.
        sampler = self.bin / 'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "sample = lambda: dict(cores=10, load=0, available_mb=20000, pressure=1)\nif __name__ == '__main__':"))
        settings = self.home / '.config/agent-lanes/config'
        settings.parent.mkdir(parents=True)
        # Two concurrent fixture runs of two workers each (MAX_WORKERS bounds the host's grants).
        settings.write_text('AGENT_CI_MAX_JOBS=2\nAGENT_CI_MAX_WORKERS=4\n')
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
        self.tree = hashlib.sha256(b'fixture execution identity').hexdigest()
        self.git('init', '--bare', '-q', self.dir / 'repo.git')
        (self.home / 'check').write_text('''#!/bin/bash
printf 'run\\n' >>"$HOME/checks"
printf 'start %s\\n' "$PWD" >>"$HOME/order"
touch "$HOME/started-$$"
while [ ! -f "$HOME/release" ]; do sleep 0.05; done
printf 'end %s\\n' "$PWD" >>"$HOME/order"
[ "$FIXTURE_RC" = 0 ] || echo 'FAIL src/check.test.ts > fixture'
exit "$FIXTURE_RC"
''')
        self.configure()

    def git(self, *args):
        return subprocess.run(['git', *map(str, args)], env=self.env, capture_output=True,
                              text=True, check=True).stdout.strip()

    def configure(self, rc=0, slots=1, exclusive=False, project='fixture'):
        # Command timeout above the 60 s fixture waits: a loaded shared runner must not kill a held check.
        (self.ci / 'projects' / project / 'conf').write_text(
            'REMOTE_CI_CHECK=\'bash "$HOME/check"\'\nREMOTE_CI_TIMEOUT=90\nREMOTE_CI_WAIT_TIMEOUT=100\n'
            f'REMOTE_CI_SLOTS={slots}\nexport FIXTURE_RC={rc}\n'
            + ('REMOTE_CI_EXCLUSIVE=1\n' if exclusive else ''))

    def add_project(self, project, slots=1, exclusive=False):
        path = self.ci / 'projects' / project
        for name in ('logs', 'results', 'slots'):
            (path / name).mkdir(parents=True)
        self.git('init', '--bare', '-q', path / 'repo.git')
        self.configure(slots=slots, exclusive=exclusive, project=project)
        return path

    def stop_runs(self):
        (self.home / 'release').touch()
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=60)

    def start(self, run, force=False, command='', project='fixture'):
        self.git('-C', self.ci / 'projects' / project / 'repo.git', 'fetch', '-q', self.source,
                 f'HEAD:refs/heads/run/{run}')
        encoded = base64.b64encode(command.encode()).decode()
        proc = subprocess.Popen(['/bin/bash', str(self.bin / 'run.sh'), project,
                                 self.sha, self.tree, run, encoded, str(int(force))],
                                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def wait_for(self, condition):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        logs = ''.join(f'\n--- {path.relative_to(self.ci)}\n{path.read_text()}'
                       for path in sorted(self.ci.glob('projects/*/logs/*.log')))
        self.fail('fixture event did not arrive within 60 seconds' + logs)

    def log(self, run, project='fixture'):
        path = self.ci / 'projects' / project / 'logs' / (run + '.log')
        return path.read_text() if path.exists() else ''

    def count(self):
        path = self.home / 'checks'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def assert_result(self, proc, run, rc, project='fixture'):
        self.assertEqual(proc.wait(timeout=60), rc, self.log(run, project))
        results = self.ci / 'projects' / project / 'results'
        self.assertEqual((results / (run + '.exit')).read_text().strip(), str(rc))

    def order(self):
        path = self.home / 'order'
        return path.read_text().splitlines() if path.exists() else []

    def cleanup_failure(self, rc, slots, fault, exclusive=False):
        self.configure(rc, slots, exclusive)
        with (self.dir / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\nREMOTE_CI_DB_CLEAN=fixture_%\n')
        (self.dir / 'env.local').write_text(
            'pg_ctl() { return 0; }\n'
            'database_url() { echo fixture; }\n'
            'psql() { cat >/dev/null; [ -f "$HOME/checks" ] || return 0; '
            + ('return 1; }\n' if fault == 'query' else 'echo fixture_test; }\n') +
            'dropdb() { [ ! -f "$HOME/checks" ]; }\n'
            'createdb() { return 0; }\n'
            'stop_idle_postgres() { touch "$HOME/stopped"; }\n'
            # Exercise finish with a deterministic suite result, without macOS time/nice.
            'supervised() { shift 5; "$@"; }\n')
        run = 'cleanup'
        (self.home / 'release').touch()
        self.assert_result(self.start(run), run, 76)
        self.assertEqual((self.dir / 'results/cleanup.check').read_text().strip(), str(rc))
        self.assertIn(f'== check-result rc={rc}', self.log(run))
        pending = self.dir / 'results/cleanup.cleanup'
        self.assertTrue(pending.exists())
        self.assertIn('role=', pending.read_text())
        self.assertFalse((self.home / 'stopped').exists())
        self.assertFalse((self.dir / 'results' / ('tree-' + self.tree + '.pass')).exists())
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        metadata = claim.with_name(claim.name + '.run').read_text()
        self.assertIn(' cleanup', metadata)
        if not exclusive:
            self.assertEqual((self.dir / 'slots/1.run').read_text().strip(), run)
        before = self.count()
        for force in (False, True):
            retry = 'retry-' + str(force)
            self.assert_result(self.start(retry, force=force), retry, 76)
            self.assertEqual(self.count(), before, 'pending cleanup must prevent resource reuse')
            self.assertEqual(claim.with_name(claim.name + '.run').read_text(), metadata)
        # An operator retries cleanup, then clears its record. Runs can use the slot again.
        pending.unlink()
        (self.dir / 'env.local').write_text(
            'supervised() { shift 5; "$@"; }\n')
        self.configure(0, slots, exclusive)
        self.assert_result(self.start('recovered', force=True), 'recovered', 0)
        self.assertEqual(self.count(), before + 1)

    def test_resource_grant_caps_project_worker_exports(self):
        with (self.dir / 'conf').open('a') as conf:
            conf.write('export VITEST_MAX_WORKERS=99\n')
        (self.home / 'check').write_text('printf "%s %s" "$AGENT_CI_WORKERS" "$VITEST_MAX_WORKERS" >"$HOME/grant"\n')
        self.assert_result(self.start('grant'), 'grant', 0)
        self.assertEqual((self.home / 'grant').read_text(), '2 2')
        self.assertIn('== resources workers=2 memory_mb=4096', self.log('grant'))

    def test_resource_overrun_is_infrastructure_not_candidate_failure(self):
        (self.home / '.config/agent-lanes/config').write_text('AGENT_CI_WORKER_MB=1\n')
        self.assert_result(self.start('resource-stop'), 'resource-stop', 75)
        self.assertIn('busy: memory budget exceeded', self.log('resource-stop'))
        self.assertFalse((self.dir / 'results' / ('tree-' + self.tree + '.pass')).exists())

    def test_cleanup_green_drop(self):
        """Requirement 1: failed cleanup preserves green checks and exclusive ownership."""
        self.cleanup_failure(0, 1, 'drop', exclusive=True)

    def test_cleanup_red_drop(self):
        """Requirement 1: cleanup takes precedence over red checks without losing their rc."""
        self.cleanup_failure(7, 2, 'drop')

    def test_cleanup_green_query(self):
        """Requirement 1: a failed cleanup query retains the slot as a failed drop does."""
        self.cleanup_failure(0, 2, 'query')

    def test_check_wrapper_failure_keeps_green_check_result(self):
        """Brief goal: timing-wrapper infrastructure failure must not become a red check."""
        (self.home / 'failed-timer').write_text('#!/bin/bash\n"$@"; exit 1\n')
        (self.dir / 'env.local').write_text('platform_time=(bash "$HOME/failed-timer")\n')
        (self.home / 'release').touch()
        self.assert_result(self.start('wrapper-failed'), 'wrapper-failed', 74)
        self.assertEqual((self.dir / 'results/wrapper-failed.check').read_text().strip(), '0')
        self.assertEqual(self.count(), 1)
        self.assertFalse((self.dir / 'results' / ('tree-' + self.tree + '.pass')).exists())

    def test_checkout_failure_is_infrastructure(self):
        """Requirement 2: checkout failure is not a candidate check verdict."""
        with (self.bin / 'lib.sh').open('a') as lib:
            lib.write('\ngit() { [ "$1" != fetch ] || return 1; command git "$@"; }\n')
        self.assert_result(self.start('checkout-failed'), 'checkout-failed', 74)
        self.assertEqual(self.count(), 0)

    def test_database_start_failure_is_infrastructure(self):
        """Requirement 2: database startup failure must not request a coding fixer."""
        with (self.dir / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\n')
        (self.dir / 'env.local').write_text('pg_ctl() { return 1; }\n')
        self.assert_result(self.start('database-failed'), 'database-failed', 74)
        self.assertEqual(self.count(), 0)

    def review_candidate_env(self):
        (self.dir / 'env.local').write_text(
            'pg_ctl() { return 0; }; createdb() { return 0; }; dropdb() { return 0; }\n'
            'psql() { cat >/dev/null; }; database_url() { echo fixture; }\n'
            'stop_idle_postgres() { :; }\n'
            'supervised() { [ "$1" != /usr/bin/time ] || shift 5; "$@"; }\n')

    def test_review_candidate_check_codes_are_normalized(self):
        """Review 6: candidate codes cannot collide with runner infrastructure codes."""
        self.review_candidate_env()
        (self.home / 'release').touch()
        for rc in (2, 3, 7, 74, 75, 76):
            with self.subTest(rc=rc):
                self.configure(rc)
                run = f'candidate-{rc}'
                self.assert_result(self.start(run, force=True), run, 1)
                self.assertEqual((self.dir / 'results' / (run + '.check')).read_text().strip(), str(rc))

    def test_review_prepare_code_seven_is_a_verdict(self):
        """Review 6: candidate prepare failures keep their raw code but publish 1."""
        self.review_candidate_env()
        with (self.dir / 'conf').open('a') as conf:
            conf.write("REMOTE_CI_PREPARE='exit 7'\n")
        self.assert_result(self.start('prepare-red'), 'prepare-red', 1)
        self.assertEqual((self.dir / 'results/prepare-red.check').read_text().strip(), '7')
        self.assertEqual(self.count(), 0)

    def test_review_candidate_migration_is_a_verdict(self):
        """Review 4: a candidate migration is not database infrastructure."""
        self.review_candidate_env()
        with (self.dir / 'conf').open('a') as conf:
            conf.write("REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\nREMOTE_CI_TEMPLATE_INPUTS=source.txt\nREMOTE_CI_DB_PREPARE='exit 1'\n")
        self.assert_result(self.start('migration-red'), 'migration-red', 1)
        self.assertEqual((self.dir / 'results/migration-red.check').read_text().strip(), '1')
        self.assertEqual(self.count(), 0)

    def saved_cleanup(self, stalled=False):
        saved = self.dir / 'runs/pending'
        saved.mkdir(parents=True)
        (saved / 'conf').write_text('REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\nREMOTE_CI_DB_CLEAN=saved_%\nREMOTE_CI_SLOTS=2\n')
        (saved / 'env.local').write_text(
            'pg_ctl() { return 0; }\n'
            'psql() { cat >/dev/null; echo "query $*" >>"$HOME/cleanup.calls"; echo saved_test; }\n'
            'dropdb() { echo "drop $*" >>"$HOME/cleanup.calls"; ' +
            ('while [ ! -f "$HOME/cleanup-release" ]; do /bin/sleep 0.05; done; ' if stalled else '') + 'return 0; }\n')
        pending = self.dir / 'results/pending.cleanup'
        pending.write_text('run=pending\nslot=1\ndatabase=saved_ci_s1\nrole=saved_role\ncheck_rc=0\n')
        (self.dir / 'results/pending.exit').write_text('76\n')
        (self.dir / 'slots/1.lock').write_text('99999999\n')
        (self.dir / 'slots/1.run').write_text('pending\n')
        self.git('-C', self.dir / 'repo.git', 'fetch', '-q', self.source, 'HEAD:refs/heads/run/pending')
        return pending

    def test_review_pending_cleanup_retries_saved_ownership(self):
        """Review 8: retry cleanup once using its saved config before resource reuse."""
        pending = self.saved_cleanup()
        self.review_candidate_env()
        (self.home / 'release').touch()
        self.assert_result(self.start('after-cleanup', force=True), 'after-cleanup', 0)
        self.assertFalse(pending.exists())
        calls = (self.home / 'cleanup.calls').read_text()
        self.assertIn('pattern=saved_%', calls)
        self.assertIn('role=saved_role', calls)
        self.assertIn('drop --if-exists --force saved_ci_s1', calls)
        self.assertEqual(calls.count('query '), 1)
        self.assertEqual(self.count(), 1)

    def test_review_pending_cleanup_retry_is_bounded(self):
        """Review 8: a stalled cleanup retry cannot hold the caller indefinitely."""
        pending = self.saved_cleanup(stalled=True)
        with (self.dir / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_WAIT_TIMEOUT=2\n')
        started = time.monotonic()
        self.assert_result(self.start('bounded-cleanup', force=True), 'bounded-cleanup', 76)
        self.assertLess(time.monotonic() - started, 60)
        self.assertTrue(pending.exists())
        self.assertTrue((self.home / 'cleanup.calls').exists(), 'must attempt cleanup before refusing')
        self.assertIn('drop ', (self.home / 'cleanup.calls').read_text())
        self.assertEqual((self.dir / 'slots/1.run').read_text().strip(), 'pending')
        self.assertEqual(self.count(), 0)

    def test_review_prune_retains_pending_cleanup_inputs(self):
        """Review 2: daily prune must retain recovery inputs and the pending run ref."""
        self.saved_cleanup()
        result = subprocess.run(['/bin/bash', str(self.bin / 'prune.sh')], env=self.env,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.dir / 'runs/pending/conf').exists())
        self.assertEqual(self.git('-C', self.dir / 'repo.git', 'rev-parse', 'refs/heads/run/pending'), self.sha)

    def prune_templates(self, recent, old_count):
        """Run prune over RECENT templates used within 3 days (newest first) and OLD_COUNT
        10-day-old ones; return the names and what prune dropped."""
        pg = self.ci / 'pg/16'
        pg.mkdir(parents=True)
        (pg / 'PG_VERSION').write_text('16\n')
        prefix = 'mci_' + hashlib.sha256(b'fixture').hexdigest()[:12] + '_'
        names = [prefix + f'{i:032x}' for i in range(recent + old_count)]
        templates = self.dir / 'templates'
        templates.mkdir()
        old = time.time() - 10 * 86400
        for i, name in enumerate(names):
            marker = templates / (name + '.ready')
            marker.touch()
            t = time.time() - i * 3600 if i < recent else old - i * 60
            os.utime(marker, (t, t))
        # prune.sh reads no env.local and load_project resets PATH; stub Postgres in the conf.
        with (self.dir / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\n'
                       'pg_ctl() { return 0; }\n'
                       'psql() { printf "%s\\n" ' + ' '.join(names) + '; }\n'
                       'dropdb() { echo "$*" >>"$HOME/dropped"; }\n')
        result = subprocess.run(['/bin/bash', str(self.bin / 'prune.sh')], env=self.env,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0)
        log = self.home / 'dropped'
        dropped = sorted(l.split()[-1] for l in log.read_text().split('\n') if l) if log.exists() else []
        for name in names:
            self.assertEqual((templates / (name + '.ready')).exists(), name not in dropped)
        return names, dropped

    def test_prune_keeps_every_template_used_in_three_days(self):
        """More than 5 recent templates all stay; only the 10-day-old ones go."""
        names, dropped = self.prune_templates(recent=7, old_count=3)
        self.assertEqual(dropped, names[7:])
        self.assertIn('template dropped', (self.ci / 'logs/prune.log').read_text())

    def test_prune_keeps_the_five_newest_old_templates(self):
        """With nothing used recently, the 5 newest stay as the cache."""
        names, dropped = self.prune_templates(recent=0, old_count=8)
        self.assertEqual(dropped, names[5:])

    def test_pass_appears_while_queued(self):
        (self.ci / 'lock').write_text(str(os.getpid()))
        proc = self.start('queued')
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        self.wait_for(lambda: claim.with_name(claim.name + '.run').exists())
        marker = self.dir / 'results' / ('tree-' + self.tree + '.pass')
        marker.write_text(self.sha + '\n')
        (self.ci / 'lock').unlink()
        self.assert_result(proc, 'queued', 0)
        self.assertIn('== cached: identical tree passed while queued', self.log('queued'))
        self.assertEqual(self.count(), 0)
        self.assertEqual(marker.read_text(), self.sha + '\n')
        self.assertFalse(claim.exists())

    def queue_jobs(self):
        path = self.home / '.local/state/agent-ci/queue.json'
        return json.loads(path.read_text())['jobs'] if path.exists() else {}

    def test_run_blocked_on_lock_holds_no_admission(self):
        # One host admission: a run waiting for the exclusive lock must leave it to another job.
        (self.home / '.config/agent-lanes/config').write_text('AGENT_CI_MAX_JOBS=1\n')
        (self.ci / 'lock').write_text(str(os.getpid()))
        proc = self.start('blocked')
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running.run')
        self.wait_for(claim.exists)
        time.sleep(2)  # the old order granted resources within milliseconds of the claim
        self.assertEqual(self.queue_jobs(), {})
        other = subprocess.run([sys.executable, str(self.bin / 'resources.py'), 'acquire', '--token', 'other',
                                '--owner', str(os.getpid()), '--deadline', str(int(time.time()) + 20),
                                '--workers', '1'], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(other.returncode, 0, other.stderr)
        subprocess.run([sys.executable, str(self.bin / 'resources.py'), 'release', '--token', 'other'],
                       env=self.env, check=True, timeout=60)
        (self.ci / 'lock').unlink()
        (self.home / 'release').touch()
        self.assert_result(proc, 'blocked', 0)
        self.assertIn('== resources workers=', self.log('blocked'))
        self.assertEqual(self.queue_jobs(), {})

    def test_slot_deadline_releases_nothing_it_never_got(self):
        with (self.dir / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_WAIT_TIMEOUT=3\n')
        (self.ci / 'lock').write_text(str(os.getpid()))
        self.assert_result(self.start('late'), 'late', 75)
        log = self.log('late')
        self.assertIn('busy: slot wait reached total deadline', log)
        self.assertNotIn('== resources', log)
        self.assertNotIn('lost-run', log)
        self.assertEqual(self.queue_jobs(), {})
        self.assertEqual((self.ci / 'lock').read_text(), str(os.getpid()))

    def test_cached_tree_never_waits_for_lock_or_resources(self):
        (self.ci / 'lock').write_text(str(os.getpid()))
        (self.dir / 'results' / ('tree-' + self.tree + '.pass')).write_text(self.sha + '\n')
        proc = self.start('cached')
        self.assertEqual(proc.wait(timeout=30), 0)
        log = self.log('cached')
        self.assertIn('== cached: identical tree passed while queued', log)
        self.assertNotIn('== resources', log)
        self.assertEqual(self.count(), 0)
    def test_malformed_pass_while_queued_releases_real_check(self):
        """Review 7: malformed marker is a miss; release the held check after rejection."""
        (self.ci / 'lock').write_text(str(os.getpid()))
        proc = self.start('malformed-queued')
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running.run')
        self.wait_for(claim.exists)
        marker = self.dir / 'results' / ('tree-' + self.tree + '.pass')
        marker.write_text('previous-success\n')
        (self.ci / 'lock').rename(self.home / 'released-lock')
        self.wait_for(lambda: self.count() == 1)
        (self.home / 'release').touch()
        self.assert_result(proc, 'malformed-queued', 0)
        self.assertNotIn('== cached:', self.log('malformed-queued'))
        self.assertEqual(marker.read_text(), self.sha + '\n')

    def test_identical_runs_follow_success_and_failure_without_a_slot(self):
        for rc, slots, exclusive in ((0, 1, True), (0, 1, False), (7, 2, False)):
            with self.subTest(rc=rc, slots=slots, exclusive=exclusive):
                self.configure(rc, slots, exclusive)
                # Use a different tree key for each case, so a prior pass cannot hide a run.
                case = f'{rc}-{slots}-{int(exclusive)}'
                self.tree = hashlib.sha256((self.tree + '-' + case).encode()).hexdigest()
                release = self.home / 'release'
                release.unlink(missing_ok=True)
                before = self.count()
                owner_id = f'owner-{self.sha[:8]}-{case}'
                follower_id = f'follower-{self.sha[:8]}-{case}'
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
                self.assert_result(owner, owner_id, 1 if rc else 0)
                self.assert_result(follower, follower_id, 1 if rc else 0)
                self.assertEqual((self.dir / 'results' / (owner_id + '.check')).read_text().strip(), str(rc))
                self.assertEqual(self.count(), before + 1)
                claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
                self.assertFalse(claim.exists())
                self.assertFalse(claim.with_name(claim.name + '.run').exists())
                marker = self.dir / 'results' / ('tree-' + self.tree + '.pass')
                self.assertEqual(marker.exists(), rc == 0)
                if rc:
                    self.assertIn('Error: followed run rc=1 (log ', self.log(follower_id))
                    # Force the follower to be latest even on a coarse filesystem clock.
                    os.utime(self.dir / 'logs' / (follower_id + '.log'),
                             ns=(time.time_ns() + 1000000,) * 2)
                    result = failure.latest(self.dir / 'logs', self.sha)
                    self.assertEqual(result['files'], [{'file': 'src/check.test.ts', 'package': ''}])
                    self.assertTrue(result['path'].endswith(owner_id + '.log'))
                    self.assertIn('FAIL src/check.test.ts', '\n'.join(
                        failure.latest(self.dir / 'logs', self.sha, tail=True)['summary']))

    def live_owner(self, run):
        # Real owners carry their run ID on the command line (run.sh PROJECT SHA TREE RUN ...).
        path = self.home / 'owner/run.sh'
        path.parent.mkdir(exist_ok=True)
        path.write_text('#!/bin/bash\nwhile :; do sleep 1; done\n')
        proc = subprocess.Popen(['/bin/bash', str(path), 'fixture', 'sha', 'tree', run, '', '0'], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def fake_clock(self):
        # Shell and Python admission deadlines must share the Unix epoch.
        self.clock_origin = int(time.time())
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
        new.write_text(str(self.clock_origin + seconds) + '\n')
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
        owner = self.live_owner('stalled-owner')
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
        owner = self.live_owner('total-owner')
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
        owner = self.live_owner('published-owner')
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
        # Darwin can return EPERM for unreaped zombies. Observe live members,
        # as resources.py does, without treating an inaccessible live group as gone.
        rows = subprocess.check_output(['ps', '-axo', 'pgid=,stat='], text=True)
        for line in rows.splitlines():
            fields = line.split()
            if fields and int(fields[0]) == pid and not fields[1].startswith('Z'):
                return True
        return False

    @staticmethod
    def stop_group(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            if Runner.group_live(pid):
                raise

    def test_sigkilled_owner_live_child_blocks_reclaim_until_child_exits(self):
        for slots, exclusive in ((1, True), (1, False), (2, False)):
            with self.subTest(slots=slots, exclusive=exclusive):
                self.configure(slots=slots, exclusive=exclusive)
                case = f'{slots}-{int(exclusive)}'
                self.tree = hashlib.sha256((self.tree + '-orphan-' + str(case)).encode()).hexdigest()
                release = self.home / 'release'
                release.unlink(missing_ok=True)
                before = self.count()
                run = f'killed-owner-{case}'
                owner = self.start(run)
                self.wait_for(lambda: self.count() == before + 1)
                marker = self.ci / 'lock.child' if exclusive else self.dir / 'slots/1.lock.child'
                self.wait_for(marker.exists)
                child = int(marker.read_text())
                children = subprocess.run(['pgrep', '-P', str(owner.pid)],
                                          capture_output=True, text=True, check=True)
                for pid in map(int, children.stdout.split()):
                    self.addCleanup(self.stop_group, pid)
                owner.kill()
                owner.wait(timeout=60)
                self.assertTrue(self.group_live(child))
                for retry in ('first', 'again'):
                    attempt = f'{retry}-after-kill-{case}'
                    proc = self.start(attempt)
                    self.assert_result(proc, attempt, 75)
                    self.assertIn('still has a live slot child', self.log(attempt))
                    self.assertEqual(self.count(), before + 1)
                    self.assertFalse((self.dir / 'slots/2.lock').exists())
                release.touch()
                self.wait_for(lambda: not self.group_live(child))
                recovered = f'recovered-{case}'
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
        owner = self.live_owner('hidden-owner')
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
        owner = self.live_owner('visible-owner')
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
        self.stub('sleep', '[ "$1" != 1 ] || touch "$HOME/waiting-$PPID"\nexec /bin/sleep "$@"\n')
        owner = self.start('guard-blocked')
        self.wait_for(lambda: self.count() == 1)
        guard = self.ci / 'lock'
        guard.write_text(str(os.getpid()))  # Reused PID remains live indefinitely.
        (self.home / 'release').touch()
        self.wait_for(lambda: (self.home / f'waiting-{owner.pid}').exists())
        self.advance_clock(60)
        self.assertEqual(owner.wait(timeout=60), 74)
        self.assertEqual((self.dir / 'results/guard-blocked.exit').read_text().strip(), '74')
        self.assertIn('lost-run:', self.log('guard-blocked'))
        self.assertIn('cleanup guard', self.log('guard-blocked'))
        self.assertEqual(guard.read_text(), str(os.getpid()))
        self.assertTrue((self.dir / 'slots/1.lock').exists())
        self.assertTrue((self.dir / 'slots/1.run').exists())

    def test_dead_followed_owner_without_result_reports_lost_run(self):
        owner = self.live_owner('lost-owner')
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        claim.write_text(str(owner.pid))
        claim.with_name(claim.name + '.run').write_text(f'{owner.pid} lost-owner\n')
        follower = self.start('lost-follower')
        self.wait_for(lambda: 'following run lost-owner' in self.log('lost-follower'))
        owner.kill()
        owner.wait(timeout=60)
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
        self.assertEqual(follower.wait(timeout=60), 75)
        self.assertEqual((self.dir / 'results/short-guard-wait.exit').read_text().strip(), '75')
        self.assertEqual(guard.read_text(), str(os.getpid()))
        self.assertEqual(self.count(), 0)

    def test_claim_release_guard_wait_is_bounded(self):
        self.fake_clock()
        self.stub('sleep', '[ "$1" != 1 ] || touch "$HOME/waiting-$PPID"\nexec /bin/sleep "$@"\n')
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
                self.tree = hashlib.sha256((self.tree + '-' + mode).encode()).hexdigest()
                run = 'unpublished-' + mode
                if mode == 'rename':
                    self.stub('mv', '''case "$1" in *.exit.new) exit 1 ;; esac
exec /bin/mv "$@"
''')
                else:
                    self.stub('mv', 'exec /bin/mv "$@"\n')
                    (self.dir / 'results' / (run + '.exit.new')).mkdir()
                owner = self.start(run)
                self.assertEqual(owner.wait(timeout=60), 74)
                self.assertIn('lost-run:', self.log(run))
                self.assertFalse((self.dir / 'results' / (run + '.exit')).exists())
                self.assertFalse((self.dir / 'results' / ('tree-' + self.tree + '.pass')).exists())

    def test_setup_lock_wait_respects_the_total_deadline(self):
        (self.ci / 'lock').write_text(str(os.getpid()))
        setup = subprocess.run(['/bin/bash', str(self.bin / 'setup.sh'), 'fixture', '1'], env=self.env,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(setup.returncode, 74, setup.stdout + setup.stderr)
        self.assertIn('lost-run:', setup.stdout)
        self.assertEqual((self.ci / 'lock').read_text(), str(os.getpid()))

    def test_setup_slot_wait_respects_the_total_deadline(self):
        (self.dir / 'slots/1.lock').write_text(str(os.getpid()))
        setup = subprocess.run(['/bin/bash', str(self.bin / 'setup.sh'), 'fixture', '1'], env=self.env,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(setup.returncode, 74, setup.stdout + setup.stderr)
        self.assertFalse((self.ci / 'lock').exists())

    def test_log_is_final_when_the_exit_is_published(self):
        self.configure(rc=7)
        (self.home / 'release').touch()
        self.stub('mv', '''case "$1" in *.exit.new)
  r=$(basename "$1" .exit.new); cp "$(dirname "$(dirname "$1")")/logs/$r.log" "$HOME/log-at-exit" ;;
esac
exec /bin/mv "$@"
''')
        self.assert_result(self.start('red'), 'red', 1)
        self.assertEqual((self.dir / 'results/red.check').read_text().strip(), '7')
        seen = (self.home / 'log-at-exit').read_text()
        self.assertIn('== finished rc=1', seen)
        self.assertTrue(failure.parse(seen)['failed'])

    def test_reused_pid_of_another_runner_is_not_the_claim_owner(self):
        other = self.live_owner('other-run')
        claim = self.dir / 'results' / ('tree-' + self.tree + '.running')
        claim.write_text(str(other.pid))
        claim.with_name(claim.name + '.run').write_text(f'{other.pid} old-run\n')
        (self.dir / 'results/old-run.exit').write_text('74\n')
        (self.home / 'release').touch()
        proc = self.start('retry')
        self.assert_result(proc, 'retry', 0)
        self.assertEqual(self.count(), 1)
        self.assertNotIn('following run old-run', self.log('retry'))

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


    def test_single_slot_projects_share_the_runner(self):
        """REMOTE_CI_SLOTS=1 without REMOTE_CI_EXCLUSIVE holds a project slot, not the whole runner."""
        other = self.add_project('other')
        a = self.start('shared-a')
        b = self.start('shared-b', project='other')
        self.wait_for(lambda: self.count() == 2)  # Both start before either finishes.
        logs = self.log('shared-a') + self.log('shared-b', 'other')
        self.assertTrue((self.dir / 'slots/1.lock').exists(), logs)
        self.assertTrue((other / 'slots/1.lock').exists(), logs)
        self.assertFalse((self.ci / 'lock.child').exists())
        (self.home / 'release').touch()
        self.assert_result(a, 'shared-a', 0)
        self.assert_result(b, 'shared-b', 0, project='other')

    def test_exclusive_project_drains_and_runs_alone(self):
        """REMOTE_CI_EXCLUSIVE=1 waits for slot runs, holds back later ones, and overrides SLOTS."""
        other = self.add_project('other', slots=2)
        self.configure(slots=2, exclusive=True)
        self.stub('sleep', 'touch "$HOME/waiting-$PPID"\nexec /bin/sleep "$@"\n')
        with (other / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_NODE=fixture\n')
        first = self.start('slot-first', project='other')
        self.wait_for(lambda: self.count() == 1)
        exclusive = self.start('exclusive')
        drain = self.ci / 'drain'
        self.wait_for(lambda: drain.exists() and drain.read_text().strip() == str(exclusive.pid))
        self.wait_for(lambda: (self.home / f'waiting-{exclusive.pid}').exists())
        # Slot 2 is free, but a run submitted after the drain owner must wait behind it.
        later = self.start('slot-later', force=True, project='other')
        self.wait_for(lambda: (self.home / f'waiting-{later.pid}').exists())
        self.assertEqual(self.count(), 1)
        self.assertFalse((other / 'slots/2.lock').exists())
        (self.home / 'release').touch()
        self.assert_result(first, 'slot-first', 0, project='other')
        self.assert_result(exclusive, 'exclusive', 0)
        self.assert_result(later, 'slot-later', 0, project='other')
        self.assertIn('slot 1/exclusive', self.log('exclusive'))
        tails = [kind + ' ' + '/'.join(path.split('/')[-2:])
                 for kind, path in (line.split(' ', 1) for line in self.order())]
        self.assertEqual(tails, ['start other/work-slot1', 'end other/work-slot1',
                                 'start fixture/work', 'end fixture/work',
                                 'start other/work-slot1', 'end other/work-slot1'])

    def test_single_slot_keeps_base_database_name(self):
        """One non-exclusive slot keeps REMOTE_CI_DB (no _s1) with its own slot role."""
        self.review_candidate_env()
        with (self.dir / 'env.local').open('a') as env:
            env.write('createdb() { echo "createdb $*" >>"$HOME/db.calls"; }\n'
                      'dropdb() { echo "dropdb $*" >>"$HOME/db.calls"; }\n')
        with (self.dir / 'conf').open('a') as conf:
            conf.write('REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\n')
        (self.home / 'release').touch()
        self.assert_result(self.start('base-db'), 'base-db', 0)
        calls = (self.home / 'db.calls').read_text().splitlines()
        created = [c for c in calls if c.startswith('createdb ')]
        self.assertEqual(len(created), 1, calls)
        self.assertRegex(created[0], r'^createdb -O mci_[0-9a-f]{12}_s1 -T template0 fixture_ci$')
        # Slot runs drop their database at prepare and cleanup; exclusive runs keep it.
        self.assertEqual(calls.count('dropdb --if-exists --force fixture_ci'), 2, calls)

    def test_prune_skips_while_a_slot_run_is_live(self):
        """Prune must not touch a project while any slot run (now the default) is live."""
        (self.dir / 'slots/1.lock').write_text(str(os.getpid()))
        submission = self.dir / 'runs/finished'
        submission.mkdir(parents=True)
        (self.dir / 'results/finished.exit').write_text('0\n')
        result = subprocess.run(['/bin/bash', str(self.bin / 'prune.sh')], env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('slot run in progress; skipped', (self.ci / 'logs/prune.log').read_text())
        self.assertTrue(submission.exists())
        self.assertFalse((self.ci / 'lock').exists())

class GroupLiveness(unittest.TestCase):
    def test_zombie_group_permission_error_does_not_hide_live_errors(self):
        """Reproduced killed-owner fixture defect: zombies exited; live EPERM stays an error.

        Process states and signal results are stubs. The existing killed-owner
        test exercises real groups and keeps every reclaim assertion.
        """
        from unittest.mock import patch
        snapshot = ['5432 Z\n5433 S\n']
        with patch.object(subprocess, 'check_output', side_effect=lambda *a, **k: snapshot[0]), \
                patch.object(os, 'killpg', side_effect=PermissionError('fixture EPERM')):
            try:
                live = Runner.group_live(5432)
            except PermissionError:
                self.fail('a zombie-only group must count as exited')
            self.assertFalse(live)
            snapshot[0] = '5432 S\n'
            self.assertTrue(Runner.group_live(5432))
            snapshot[0] = '5432 Z\n'
            try:
                Runner.stop_group(5432)
            except PermissionError:
                self.fail('cleanup must tolerate EPERM only after the group exits')
            snapshot[0] = '5432 S\n'
            with self.assertRaises(PermissionError):
                Runner.stop_group(5432)


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
        isolate_corepack(self.kit / 'remote', self.home)
        # The client also calls its bounded-transport and routing helpers.
        shutil.copytree(KIT / 'bin', self.kit / 'bin')
        self.client = self.kit / 'bin/remote-ci'
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
            'ssh': f'#!{sys.executable}\n' + '''import os, re, subprocess, sys, time
from pathlib import Path
command = sys.argv[-1]
if os.environ['FIXTURE_CLIENT_MODE'] == 'blocked-transport' and 'done=' in command:
    Path(os.environ['HOME'] + '/client-waiting').touch()
    while True: time.sleep(60)
if 'nohup bash ' in command:
    # The launch gate (freeze, host kit) runs for real; the detached run is the stub below.
    gate = command.split('nohup bash ', 1)[0].strip().rstrip(';')
    if gate:
        status = subprocess.run(['/bin/bash', '-c', gate]).returncode
        if status:
            sys.exit(status)
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
            proc.communicate(timeout=60)

    def start(self, summary=False):
        proc = subprocess.Popen(['/bin/bash', str(self.client), 'check',
                                 *(['--summary'] if summary else [])], cwd=self.root,
                                env=self.env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        self.procs.append(proc)
        return proc

    def wait_for(self, condition):
        end = time.monotonic() + 60
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

    def test_review_cached_pass_cannot_bypass_pending_cleanup(self):
        """Review 3: cached checks must reach recovery when cleanup is pending."""
        git = lambda *args: subprocess.check_output(['/usr/bin/git', *map(str, args)],
                                                     cwd=self.root, text=True).strip()
        blobs = ''.join(script.name + '\n' + git('hash-object', script) + '\n'
                        for script in sorted(p for p in (self.kit / 'remote').iterdir() if p.suffix in ('.sh', '.py')))
        rev = subprocess.check_output(['/usr/bin/git', 'hash-object', '--stdin'],
                                      input=blobs, text=True).strip()[:8]
        key = git('rev-parse', 'HEAD^{tree}') + '-' + git('hash-object', self.root / '.remote-ci.conf')[:8] + '-' + rev
        (self.dir / 'results' / ('tree-' + key + '.pass')).touch()
        (self.dir / 'results/pending.cleanup').write_text('run=pending\n')
        self.env['FIXTURE_CLIENT_MODE'] = 'completed'
        proc = self.start(summary=True)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 7, out + err)  # Published result from the fixture submission.
        self.assertTrue((self.home / 'submitted').exists())
        self.assertNotIn('already passed', out)

    def test_run_ids_differ_with_same_time_commit_and_pid(self):
        self.fixed_run_prefix()
        runs = []
        for _ in range(2):
            proc = self.start(summary=True)
            out, err = proc.communicate(timeout=60)
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
        out, err = first.communicate(timeout=60)
        self.assertEqual(first.returncode, 7, err)
        run = (self.home / 'submitted').read_text()
        conf = self.dir / 'runs' / run / 'conf'
        conf.write_text('keep existing run config\n')
        pushes = (self.home / 'push-stub').read_text()
        second = self.start(summary=True)
        out, err = second.communicate(timeout=60)
        self.assertEqual(second.returncode, 74, err)  # Requirement 2: submission is infrastructure.
        self.assertIn('cannot reserve run directory', out)
        self.assertEqual(conf.read_text(), 'keep existing run config\n')
        self.assertEqual((self.home / 'push-stub').read_text(), pushes)
        self.assertEqual(len(list((self.dir / 'runs').iterdir())), 1)

    def test_first_setup_runs_the_runs_own_scripts_within_the_deadline(self):
        (self.dir / '.ready').unlink()
        (self.kit / 'remote/setup.sh').write_text('#!/bin/bash\nprintf "%s %s\\n" "$0" "$2" >"$HOME/setup-call"\n')
        self.env['FIXTURE_CLIENT_MODE'] = 'completed'
        proc = self.start(summary=True)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 7, out + err)
        run = (self.home / 'submitted').read_text()
        script, budget = (self.home / 'setup-call').read_text().split()
        self.assertEqual(script, str(self.dir / 'runs' / run / 'bin/setup.sh'))
        self.assertEqual(budget, '100')

    def test_client_returns_published_result_in_stream_and_summary_modes(self):
        self.env['FIXTURE_CLIENT_MODE'] = 'completed'
        for summary in (False, True):
            with self.subTest(summary=summary):
                proc = self.start(summary)
                out, err = proc.communicate(timeout=60)
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
                    out, err = proc.communicate(timeout=60)
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
            isolate_corepack(kit / 'remote', home)
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
                               for script in sorted(p for p in (kit / 'remote').iterdir() if p.suffix in ('.sh', '.py')))
            call = subprocess.run(['/bin/bash', str(client), 'check'], cwd=root,
                                  env=env, capture_output=True, text=True)
            self.assertEqual(call.returncode, 74)  # Requirement 2: setup failed before any check.
            self.assertTrue((home / 'mutated').exists())
            self.assertEqual((home / 'hashed-kit').read_text(), expected)
            self.assertNotEqual((kit / 'remote/run.sh').read_bytes(), original)
            copies = [home / 'ci/bin/run.sh', *home.glob('ci/projects/fixture/runs/*/bin/run.sh')]
            self.assertEqual(len(copies), 2)
            for copy in copies:
                self.assertEqual(copy.read_bytes(), original)

    def test_client_cache_key_changes_with_synced_runner_scripts(self):
        # Review 7: share the full identity-flow fixture, including valid SHA markers.
        from test_cache import ReviewClient
        fixture = ReviewClient('test_client_cache_identity_and_terminal_hit')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.test_client_cache_identity_and_terminal_hit()


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
                                            capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 7)
                    self.assertIn('== following run owner', result.stdout)
                    self.assertEqual(result.stdout.count('== finished rc=7'), 1)
                    self.assertEqual('FAIL src/check.test.ts' in result.stdout, not bool(pattern))
                    self.assertEqual('owner-only-marker' in result.stdout, bool(pattern))
            # A pruned owner log must not hide the follower's result or event.
            owner.unlink()
            result = subprocess.run(['/bin/bash', '-c', script], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 7)
            self.assertIn('== following run owner', result.stdout)
            self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
