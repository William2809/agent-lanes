"""V2 runner requirements with real processes and fake suites on the runner."""
import json
import base64
import os
from pathlib import Path
import subprocess
import signal
import sys
import time
import unittest

import test_runner as fixtures
if os.environ.get('CIQUEUE_BASELINE_KIT'):
    fixtures.KIT = Path(os.environ['CIQUEUE_BASELINE_KIT'])


@unittest.skipUnless(sys.platform == 'darwin', 'runner uses macOS time -l')
class SchedulerRunner(unittest.TestCase):
    git = fixtures.Runner.git
    configure = fixtures.Runner.configure
    add_project = fixtures.Runner.add_project
    stop_runs = fixtures.Runner.stop_runs
    start = fixtures.Runner.start
    wait_for = fixtures.Runner.wait_for
    log = fixtures.Runner.log
    count = fixtures.Runner.count
    assert_result = fixtures.Runner.assert_result

    def setUp(self):
        fixtures.Runner.setUp(self)
        settings = self.home / '.config/agent-lanes/config'
        settings.write_text('AGENT_CI_MAX_JOBS=2\nAGENT_CI_MAX_WORKERS=4\nAGENT_CI_SCHEDULER=v2\n')
        sampler = self.bin/'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "scheduler_sample = lambda: dict(cores=10, load=0, available_mb=20000, total_mb=24000, pressure=1, swap_mb=0)\nif __name__ == '__main__':"))

    def rows(self):
        path = self.home / '.local/state/agent-ci/queue.json'
        return json.loads(path.read_text())['jobs'] if path.exists() else {}

    def test_existing_project_lease_client_accepts_only_live_v2_group(self):
        # Use the unchanged shipped v1 reader, also vendored by existing projects.
        helper = Path(__file__).parent.parent/'resource-safe-checks/scripts/resource-lease.cjs'
        (self.home/'legacy-lease.cjs').write_bytes(helper.read_bytes())
        (self.home/'probe.cjs').write_text(
            "require('./legacy-lease.cjs').requireAllocation();console.log('legacy-lease-ok');")
        active = self.start('legacy-client',
                            command='node "$HOME/probe.cjs" && bash "$HOME/check"')
        self.wait_for(lambda: self.count() == 1 or active.poll() is not None)
        self.assertEqual(self.count(), 1, self.log('legacy-client'))
        token, row = next((t,r) for t,r in self.rows().items() if r.get('state') == 'running')
        self.assertEqual(row['group'], row['process']['group'])
        outside = subprocess.run(['node', '-e',
            "console.log(require(process.argv[1]).allocation() !== null)",
            str(self.home/'legacy-lease.cjs')], env=dict(self.env, AGENT_CI_TOKEN=token),
            capture_output=True, text=True, timeout=10, start_new_session=True)
        self.assertEqual(outside.returncode, 0, outside.stderr)
        self.assertEqual(outside.stdout.strip(), 'false')
        (self.home/'release').touch()
        self.assert_result(active, 'legacy-client', 0)
        self.assertIn('legacy-lease-ok', self.log('legacy-client'))
        self.assertNotIn('group', self.rows()[token])
        self.assertFalse(self.rows()[token].get('groups'))

    def test_targeted_duplicates_share_failure_without_second_execution(self):
        self.configure(rc=1)
        first = self.start('first', command='bash "$HOME/check"')
        self.wait_for(lambda: self.count() == 1)
        second = self.start('second', command='bash "$HOME/check"')
        self.wait_for(lambda: any(r.get('state') == 'following' for r in self.rows().values()))
        (self.home / 'release').touch()
        self.assert_result(first, 'first', 1)
        self.assert_result(second, 'second', 1)
        self.assertEqual(self.count(), 1)

    def test_two_slots_run_and_cleanup_without_global_lock(self):
        self.configure(slots=2)
        first = self.start('first', command='bash "$HOME/check"; true')
        second = self.start('second', command='bash "$HOME/check"; :')
        self.wait_for(lambda: self.count() == 2)
        running = [r for r in self.rows().values() if r.get('state') == 'running']
        self.assertEqual({r['slot'] for r in running}, {1, 2})
        self.assertEqual(sum(r['workers'] for r in running), 2)
        self.assertFalse((self.ci/'lock').exists())
        (self.home / 'release').touch()
        self.assert_result(first, 'first', 0)
        self.assert_result(second, 'second', 0)
        self.assertIn('model=observe', self.log('first'))

    def test_version_is_pinned_through_finish(self):
        first = self.start('first')
        self.wait_for(lambda: self.count() == 1)
        self.assertEqual(next(iter(self.rows().values())).get('version'), 2)
        (self.home / '.config/agent-lanes/config').write_text('AGENT_CI_SCHEDULER=v1\n')
        (self.home / 'release').touch()
        self.assert_result(first, 'first', 0)
        self.assertFalse(any(r.get('workers') for r in self.rows().values()))

    def test_database_and_template_need_no_later_lock(self):
        self.configure(slots=2)
        with (self.dir / 'conf').open('a') as stream:
            stream.write("REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\nREMOTE_CI_DB='fixture_s{slot}_test'\nREMOTE_CI_TEMPLATE_INPUTS=source.txt\nREMOTE_CI_DB_PREPARE='true'\n")
        (self.dir / 'env.local').write_text(
            'pg_ctl() { return 0; }\n'
            'database_url() { echo fixture; }\n'
            'psql() { cat >/dev/null; case "$*" in *pg_database_size*) echo 4096;; esac; return 0; }\n'
            'dropdb() { return 0; }\ncreatedb() { printf "%s\\n" "$@" >>"$HOME/created"; }\n'
            'acquire_guard() { echo BAD_LATE_LOCK; return 1; }\n')
        (self.home/'release').touch()
        self.assert_result(self.start('database'), 'database', 0)
        self.assertNotIn('BAD_LATE_LOCK', self.log('database'))
        self.assertIn('fixture_s1_test', (self.home/'created').read_text().splitlines())

    def test_queued_generation_exits_superseded_without_executing(self):
        active = self.start('active', command='bash "$HOME/check"')
        self.wait_for(lambda: self.count() == 1)

        def submit(run, generation):
            self.git('-C', self.dir/'repo.git', 'fetch', '-q', self.source,
                     'HEAD:refs/heads/run/' + run)
            command = base64.b64encode(('bash "$HOME/check"; : ' + run).encode()).decode()
            submission = self.dir/'runs'/run
            submission.mkdir(parents=True)
            (submission/'submission.json').write_text(json.dumps(dict(
                schema='agent-ci.submission/v1', protocol=2, task='required-test',
                lane='lane', generation=generation, landing=False)))
            proc = subprocess.Popen(['/bin/bash', str(self.bin/'run.sh'), 'fixture', self.sha,
                                     self.tree, run, command, '0', 'runner'],
                                    env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.procs.append(proc)
            return proc

        old = submit('old', 1)
        self.wait_for(lambda: any(r.get('state') == 'queued' for r in self.rows().values()))
        new = submit('new', 2)
        self.assert_result(old, 'old', 77)
        self.assertEqual(self.count(), 1)
        self.assertIn('superseded', self.log('old'))
        (self.home/'release').touch()
        self.assert_result(active, 'active', 0)
        self.assert_result(new, 'new', 0)
        self.assertEqual(self.count(), 2)

    def test_cached_tree_returns_before_busy_slot(self):
        # ciadmit defect: an existing pass must not wait behind an unrelated run.
        active = self.start('active', command='bash "$HOME/check"')
        self.wait_for(lambda: self.count() == 1)
        (self.dir/'results'/('tree-' + self.tree + '.pass')).write_text(self.sha + '\n')
        cached = self.start('cached')
        self.assertEqual(cached.wait(timeout=60), 0, self.log('cached'))
        self.assertEqual(self.count(), 1)
        (self.home/'release').touch()
        self.assert_result(active, 'active', 0)

    def test_two_single_slot_projects_share_host(self):
        self.add_project('other')
        first = self.start('first')
        second = self.start('second', project='other')
        self.wait_for(lambda: self.count() == 2)
        rows = [r for r in self.rows().values() if r.get('state') == 'running']
        self.assertEqual({r['project'] for r in rows}, {'fixture', 'other'})
        self.assertEqual([r['slot'] for r in rows], [1, 1])
        (self.home/'release').touch()
        self.assert_result(first, 'first', 0)
        self.assert_result(second, 'second', 0, project='other')

    def test_cancellation_checks_generation_then_reaps_phase(self):
        """Follow-up 6: stale cancellation cannot stop a newer lease."""
        active = self.start('active')
        self.wait_for(lambda: self.count() == 1)
        token, row = next(iter(self.rows().items()))
        command = [sys.executable, str(self.bin/'resources.py'), 'cancel', '--token', token,
                   '--phase', 'check', '--generation']
        stale = subprocess.run(command + [str(row['lease_generation'] + 1)], env=self.env,
                               capture_output=True, timeout=60)
        self.assertEqual(stale.returncode, 74)
        self.assertIsNone(active.poll())
        result = subprocess.run(command + [str(row['lease_generation'])], env=self.env,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assert_result(active, 'active', 74)
        self.assertNotIn('process', self.rows()[token])
        self.assertNotIn('workers', self.rows()[token])

    def test_fast_parent_cannot_leave_live_descendant_after_release(self):
        """Follow-up 6: retain a birth-bound group leader until descendants die."""
        proc = self.start('descendant', command='sleep 30 & echo $! >"$HOME/descendant"; exit 0')
        self.assert_result(proc, 'descendant', 0)
        pid = (self.home/'descendant').read_text().strip()
        status = subprocess.run(['ps', '-p', pid, '-o', 'stat='], capture_output=True, text=True)
        self.assertTrue(not status.stdout.strip() or status.stdout.strip().startswith('Z'))
        self.assertFalse(any(r.get('groups') or r.get('workers') for r in self.rows().values()))

    def test_cleanup_failure_allows_other_project_and_recovers(self):
        """Review P1: failed cleanup persists and recovery is bounded/scoped."""
        self.add_project('other')
        with (self.dir/'conf').open('a') as stream:
            stream.write('REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\n')
        (self.dir/'env.local').write_text(
            'pg_ctl() { return 0; }\ndatabase_url() { echo fixture; }\n'
            'psql() { cat >/dev/null; }\ncreatedb() { return 0; }\n'
            'dropdb() { [ ! -f "$HOME/checks" ] || [ -f "$HOME/repaired" ]; }\n')
        saved = self.dir/'runs/dirty'
        saved.mkdir(parents=True)
        (saved/'conf').write_text((self.dir/'conf').read_text())
        (saved/'env.local').write_text((self.dir/'env.local').read_text())
        (self.home/'release').touch()
        self.assert_result(self.start('dirty'), 'dirty', 76)
        pending = self.dir/'results/dirty.cleanup'
        self.assertTrue(pending.exists())
        self.assert_result(self.start('short', project='other'), 'short', 0, project='other')
        self.assertTrue(pending.exists())
        token = next(t for t, r in self.rows().items() if r.get('run_id') == 'dirty')
        self.assertEqual(self.rows()[token]['state'], 'quarantined')
        # Finish the failed automatic attempt before removing the injected fault.
        self.wait_for(lambda: any(r.get('recovery_of') == token and r['state'] == 'finished' and
                                 r['result'] == 76 for r in self.rows().values()))
        (self.home/'repaired').touch()
        recovered = subprocess.run([sys.executable, str(self.bin/'scheduler_maintenance.py'),
                                    'recover', token], env=self.env, capture_output=True, timeout=70)
        self.assertEqual(recovered.returncode, 0, recovered.stderr.decode())
        self.assertFalse(pending.exists())
        self.assertTrue((self.dir/'results/dirty.recovered').exists())
        self.assertEqual(self.rows()[token]['state'], 'finished')

    def test_database_commands_have_phase_deadline(self):
        """Follow-up 4: a blocked DB command cannot hold an admitted slot forever."""
        with (self.dir/'conf').open('a') as stream:
            stream.write('REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\nREMOTE_CI_TIMEOUT=1\n')
        (self.dir/'env.local').write_text('pg_ctl() { sleep 30; }\ndropdb() { return 0; }\n')
        started = time.monotonic()
        proc = self.start('database-timeout')
        self.assert_result(proc, 'database-timeout', 74)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(self.count(), 0)
        self.assertFalse(any(r.get('process') for r in self.rows().values()))

    def test_maintenance_entry_requires_live_lease(self):
        """Follow-up 2/4: internal maintenance flags cannot bypass admission."""
        for script, args in (('prune.sh', ['--admitted-v2']),
                             ('run.sh', ['--cleanup-v2', str(self.dir/'results/fake.cleanup')])):
            with self.subTest(script=script):
                result = subprocess.run(['/bin/bash', str(self.bin/script), *args], env=self.env,
                                        capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 74)

    def test_prune_waits_for_host_lease_and_runs_alone(self):
        """Follow-up 4/7: maintenance uses host exclusion through the ledger."""
        self.add_project('other')
        first = self.start('first')
        self.wait_for(lambda: self.count() == 1)
        prune = subprocess.Popen(['/bin/bash', str(self.bin/'prune.sh')], env=self.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(prune)
        self.wait_for(lambda: any(r.get('maintenance') == 'prune' and r['state'] == 'queued'
                                 for r in self.rows().values()))
        later = self.start('later', project='other')
        self.wait_for(lambda: any(r.get('run_id') == 'later' for r in self.rows().values()))
        self.assertEqual(self.count(), 1)
        (self.home/'release').touch()
        self.assert_result(first, 'first', 0)
        self.assertEqual(prune.wait(timeout=60), 0)
        self.assert_result(later, 'later', 0, project='other')
        rows = list(self.rows().values())
        before = next(r for r in rows if r.get('run_id') == 'first')
        maintenance = next(r for r in rows if r.get('maintenance') == 'prune')
        after = next(r for r in rows if r.get('run_id') == 'later')
        self.assertGreaterEqual(maintenance['admitted_at'], before['finished_at'])
        self.assertGreaterEqual(after['admitted_at'], maintenance['finished_at'])
        self.assertIn('exclusive-host', maintenance['granted']['leases'])

    def test_prune_preserves_quarantined_recovery_config(self):
        """Review P1: quarantine keeps its recovery input even without .cleanup."""
        saved = self.dir/'runs/orphan'
        saved.mkdir(parents=True)
        (saved/'conf').write_text((self.dir/'conf').read_text())
        (self.dir/'results/orphan.exit').write_text('74')
        (self.dir/'results/orphan.recovery').write_text('run=orphan\nslot=1\n')
        ledger = self.home/'.local/state/agent-ci/queue.json'
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(dict(sequence=1, jobs=dict(orphan=dict(
            version=2, protocol=2, project='fixture', run_id='orphan', sequence=1,
            lease_generation=1, state='quarantined', owner=0, start='', result=76,
            phase='cleanup', cleanup_required=True, recover_after=time.time()+3600,
            submitted_at=time.time(), slot=1, quarantined_leases=['cleanup:fixture'],
            requested=dict(slots=1, workers=1, memory_mb=4096, leases=['cleanup:fixture']))))))
        result = subprocess.run(['/bin/bash', str(self.bin/'prune.sh')], env=self.env,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue((saved/'conf').exists(), 'prune removed the quarantined recovery config')
        self.assertTrue((self.dir/'results/orphan.recovery').exists())
        self.assertEqual(self.rows()['orphan']['state'], 'quarantined')

    def test_freeze_refuses_new_admission_and_maintenance(self):
        """Host-kit migration: freeze must cover Python maintenance submitters."""
        (self.ci/'freeze').touch()
        self.assert_result(self.start('frozen'), 'frozen', 75)
        prune = subprocess.run(['/bin/bash', str(self.bin/'prune.sh')], env=self.env,
                               capture_output=True, timeout=60)
        self.assertEqual(prune.returncode, 75)
        self.assertEqual(self.count(), 0)
        self.assertFalse(any(r.get('workers') for r in self.rows().values()))

    def test_invalid_host_version_publishes_infrastructure_result(self):
        """Migration: an invalid host flag must not select a competing path."""
        (self.home/'.config/agent-lanes/config').write_text('AGENT_CI_SCHEDULER=unknown\n')
        self.assert_result(self.start('invalid'), 'invalid', 74)
        self.assertNotIn('cache prune', self.log('invalid'))
        self.assertNotIn('cache unavailable', self.log('invalid'))
        prune = subprocess.run(['/bin/bash', str(self.bin/'prune.sh')], env=self.env,
                               capture_output=True, timeout=60)
        self.assertEqual(prune.returncode, 74)
        self.assertEqual(self.count(), 0)

    def test_installer_counts_orphan_group_before_recovery(self):
        """Follow-up 1/6: installation cannot miss a phase whose owner died."""
        first = self.start('lost')
        self.wait_for(lambda: self.count() == 1)
        row = next(iter(self.rows().values()))
        group = row['process']['group']
        monitor = int(subprocess.check_output(['ps', '-p', str(group), '-o', 'ppid=']))
        identity = subprocess.check_output(['ps', '-p', str(monitor), '-o', 'command='], text=True)
        self.assertIn(str(self.bin/'scheduler_process.py'), identity)
        os.kill(monitor, signal.SIGSTOP)
        try:
            first.kill()
            first.wait(timeout=60)
            count = subprocess.check_output([sys.executable, str(self.bin/'resources.py'),
                                             'active-count'], env=self.env, text=True, timeout=60)
            self.assertEqual(count.strip(), '1')
            install = subprocess.check_output(['/bin/bash', str(self.bin/'install-kit.sh'),
                                               'count'], env=self.env, text=True, timeout=60)
            self.assertGreaterEqual(int(install), 1)
        finally:
            os.kill(monitor, signal.SIGCONT)
        (self.home/'release').touch()
        self.assert_result(self.start('after-loss'), 'after-loss', 0)
        lost = next(r for r in self.rows().values() if r.get('run_id') == 'lost')
        self.assertEqual(lost['result'], 74)
        self.assertFalse(lost.get('groups'))


@unittest.skipUnless(sys.platform == 'darwin', 'runner uses macOS time -l')
class SchedulerV1Cache(unittest.TestCase):
    git = fixtures.Runner.git
    configure = fixtures.Runner.configure
    stop_runs = fixtures.Runner.stop_runs
    start = fixtures.Runner.start
    wait_for = fixtures.Runner.wait_for
    log = fixtures.Runner.log
    count = fixtures.Runner.count
    assert_result = fixtures.Runner.assert_result

    def setUp(self):
        fixtures.Runner.setUp(self)
        with (self.home/'.config/agent-lanes/config').open('a') as stream:
            stream.write('AGENT_CI_SCHEDULER=v1\n')

    test_cached_tree_returns_before_busy_slot = SchedulerRunner.test_cached_tree_returns_before_busy_slot


class SchedulerClient(unittest.TestCase):
    setUp = fixtures.ClientFixture.setUp
    clock = fixtures.ClientFixture.clock
    stop_clients = fixtures.ClientFixture.stop_clients
    start = fixtures.ClientFixture.start

    def test_client_reports_superseded_in_both_output_modes(self):
        stub = self.home/'stubs/ssh'
        stub.write_text(stub.read_text().replace('rc=7', 'rc=77').replace("write_text('7\\n')", "write_text('77\\n')"))
        self.env['FIXTURE_CLIENT_MODE'] = 'completed'
        for summary in (False, True):
            with self.subTest(summary=summary):
                proc = self.start(summary)
                out, err = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 77, out + err)
                self.assertIn('remote-ci: superseded', out)


if __name__ == '__main__':
    unittest.main()
