"""ciqueue2rv regressions. Real runner fixtures; host metrics are synthetic."""
import json
import tempfile
import time
from unittest.mock import patch
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_runner as fixtures
import test_hostkit as host_fixtures
KIT = Path(os.environ.get('CIQUEUE_REVIEW_KIT', Path(__file__).resolve().parent))
fixtures.KIT = KIT
host_fixtures.KIT = KIT
sys.path.insert(0, str(KIT/'remote'))
import resources as r
import scheduler_maintenance as maintenance
import scheduler_process as supervisor


class GrantLifecycle(unittest.TestCase):
    git = fixtures.Runner.git
    configure = fixtures.Runner.configure
    stop_runs = fixtures.Runner.stop_runs
    start = fixtures.Runner.start
    wait_for = fixtures.Runner.wait_for
    log = fixtures.Runner.log
    count = fixtures.Runner.count
    assert_result = fixtures.Runner.assert_result
    setUp = fixtures.Runner.setUp

    def test_admission_timeout_publishes_75_without_a_peak_grant(self):
        # Review 1: the one-second product deadline must still publish a receipt.
        with (self.dir/'conf').open('a') as stream:
            stream.write('REMOTE_CI_WAIT_TIMEOUT=1\n')
        sampler = self.bin/'resources.py'
        sampler.write_text(sampler.read_text().replace('available_mb=20000, pressure=1)',
                                                      'available_mb=20000, pressure=4)'))
        self.assert_result(self.start('timeout'), 'timeout', 75)
        self.assertNotIn('== memory peak', self.log('timeout'))
        self.assertEqual(self.count(), 0)

    def test_coordinator_failure_publishes_74_without_a_peak_grant(self):
        # Review 1: an acquire error has no grant to include in the peak line.
        sampler = self.bin/'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "if __name__ == '__main__' and sys.argv[1] == 'acquire': sys.exit(74)\nif __name__ == '__main__':"))
        self.assert_result(self.start('lost'), 'lost', 74)
        self.assertNotIn('== memory peak', self.log('lost'))
        self.assertEqual(self.count(), 0)

    def test_killed_before_recovery_file_does_not_quarantine_untouched_grant(self):
        # Review 4: stop after the transaction grants, before shell recovery input.
        (self.home/'.config/agent-lanes/config').write_text('AGENT_CI_SCHEDULER=v2\n')
        with (self.dir/'conf').open('a') as stream:
            stream.write('REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\n')
        sampler = self.bin/'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "scheduler_sample=lambda: dict(cores=10, load=0, available_mb=20000, total_mb=24000, pressure=1, swap_mb=0)\nif __name__ == '__main__':"))
        hook = self.bin/'scheduler.sh'
        hook.write_text(hook.read_text().replace('  locked=1\n', '  kill -STOP "$$"\n  locked=1\n', 1))
        ledger = self.home/'.local/state/agent-ci/queue.json'
        def granted():
            return ledger.exists() and any(r.get('state') == 'running'
                for r in json.loads(ledger.read_text())['jobs'].values())
        run = self.start('untouched')
        self.wait_for(granted)
        run.kill()
        run.wait(timeout=60)
        self.assertFalse((self.dir/'results/untouched.recovery').exists())
        result = subprocess.run([sys.executable, '-c',
            'import resources,scheduler_process; scheduler_process.reconcile(resources.Queue())'],
            env=dict(self.env, PYTHONPATH=str(self.bin)), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = next(iter(json.loads(ledger.read_text())['jobs'].values()))
        self.assertEqual(row['state'], 'finished')
        self.assertEqual(row['result'], 74)
        self.assertFalse(row.get('workers'))
        self.assertEqual(self.count(), 0)


    def test_v2_receipts_keep_grant_and_cached_outcome(self):
        # Rebase requirement: keep the landed receipt hooks on both admission paths.
        (self.home/'.config/agent-lanes/config').write_text(
            'AGENT_CI_SCHEDULER=v2\nAGENT_CI_WORKER_MB=2048\n')
        with (self.dir/'conf').open('a') as stream:
            stream.write('REMOTE_CI_WORKERS=1\nREMOTE_CI_MEMORY_MB=4096\n')
        sampler = self.bin/'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "scheduler_sample=lambda: dict(cores=10, load=0, available_mb=20000, total_mb=24000, pressure=1, swap_mb=0)\nif __name__ == '__main__':"))
        (self.home/'release').touch()
        for name in ('first', 'cached'):
            with self.subTest(run=name):
                self.assert_result(self.start(name), name, 0)
                path = self.ci/'receipts/fixture'/(name+'.json')
                self.wait_for(lambda: path.exists() and json.loads(path.read_text())['exit_code'] is not None)
                row = json.loads(path.read_text())
                if name == 'first':
                    self.assertEqual(row['granted'], dict(workers=1, memory_mb=4096))
                else:
                    self.assertEqual(row['cache']['tree']['outcome'], 'hit')
                    self.assertNotIn('check', row['phases'])
                self.assertEqual(self.count(), 1)


def quarantine():
    return dict(version=2, protocol=2, owner=0, start='', state='quarantined',
                sequence=1, lease_generation=1, project='fixture', run_id='old',
                phase='cleanup', result=76, cleanup_required=True, slot=1,
                requested=dict(slots=1, workers=1, memory_mb=4096, leases=['cleanup:fixture']))


class Migration(unittest.TestCase):
    def test_both_authority_switches_refuse_unresolved_leases(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp,
                                                              AGENT_CI_SCHEDULER='v1'):
            queue = r.Queue()
            with queue.transaction() as data:
                data['authority'] = 'v1'
                data['jobs']['legacy'] = dict(owner=os.getpid(), start='fixture', sequence=1)
            with patch.dict(os.environ, AGENT_CI_SCHEDULER='v2'):
                with self.assertRaises(ValueError):
                    r.scheduler_version()
                with queue.transaction() as data:
                    data['jobs'].clear()
                    data.pop('authority', None)
                pending = Path(temp)/'ci/projects/fixture/results/legacy.cleanup'
                pending.parent.mkdir(parents=True)
                pending.write_text('slot=1\n')
                (Path(temp)/'ci/freeze').touch()
                with patch.object(sys, 'argv', ['resources.py', 'recover-pending']), \
                        patch.object(maintenance, 'start_recovery'):
                    self.assertEqual(r.main(), 0)
                with self.assertRaises(ValueError):
                    r.scheduler_version()
                pending.rename(pending.with_suffix('.recovered'))
                with queue.transaction() as data:
                    data['jobs'].clear()
                self.assertEqual(r.scheduler_version(), 'v2')
            with queue.transaction() as data:
                data['jobs']['old'] = quarantine()
            # The prior authority marker is necessary even after a config rollback.
            (queue.root/'authority').write_text('v2')
            with self.assertRaises(ValueError):
                r.scheduler_version()
            with queue.transaction() as data:
                data['jobs']['old']['state'] = 'finished'
            self.assertEqual(r.scheduler_version(), 'v1')

    def test_grant_checks_authority_again_inside_transaction(self):
        limits = dict(MAX_JOBS=2, MAX_WORKERS=4, CPU_RESERVE=1,
                      MEM_RESERVE_MB=1024, WORKER_MB=2048, MAX_JOB_MB=8192)
        metrics = dict(cores=8, load=0, available_mb=32000, total_mb=32000,
                       pressure=1, swap_mb=0)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp,
                                                              AGENT_CI_SCHEDULER='v1'):
            queue = r.Queue()
            with queue.transaction() as data:
                data['authority'] = 'v2'
            # Model the switch after the outer flag read, before lock acquisition.
            with patch.object(r, 'scheduler_version', return_value='v1'), \
                    patch.object(r, 'sample', return_value=metrics):
                with self.assertRaises(ValueError):
                    r.acquire(queue, 'legacy', os.getpid(), time.time()+60, limits, 1)
            with queue.transaction() as data:
                data['jobs'].clear()
                data['authority'] = 'v1'
            request = dict(project='fixture', lane='', sha='', tree='new', mode='run',
                           command_digest='a'*64, generation=1, slots=1, workers=1,
                           memory_mb=4096, leases=[])
            with patch.object(r, 'scheduler_version', return_value='v2'), \
                    patch.object(r, 'policy', return_value=limits), \
                    patch.object(r, 'scheduler_sample', return_value=metrics):
                result = r.acquire_v2(queue, 'new', os.getpid(), time.time()+60,
                                      limits, request, 'runner')
                self.assertEqual(result.get('result'), 74)
            with queue.transaction() as data:
                self.assertFalse(any(row.get('workers') for row in data['jobs'].values()))

    def test_installer_counts_dead_and_quarantined_leases(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp):
            queue = r.Queue()
            with queue.transaction() as data:
                data['jobs']['dirty'] = quarantine()
                data['jobs']['dead'] = dict(quarantine(), state='running', owner=99999999)
            call = subprocess.run([sys.executable, str(KIT/'remote/resources.py'), 'active-count'],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(call.returncode, 0, call.stderr)
            self.assertEqual(call.stdout.strip(), '2')

    def test_frozen_drain_resolves_dead_queued_and_following_owners(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp,
                                                              AGENT_CI_SCHEDULER='v1'):
            (Path(temp)/'ci').mkdir()
            (Path(temp)/'ci/freeze').touch()
            queue = r.Queue()
            with queue.transaction() as data:
                for state in ('queued', 'following'):
                    data['jobs'][state] = dict(quarantine(), state=state,
                                              owner=99999999, owner_birth='gone')
            call = subprocess.run([sys.executable, str(KIT/'remote/resources.py'), 'recover-pending'],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(call.returncode, 0, call.stderr)
            with queue.transaction() as data:
                self.assertEqual([row['state'] for row in data['jobs'].values()], ['finished', 'finished'])
                self.assertTrue(all(row['result'] == 74 for row in data['jobs'].values()))
            self.assertEqual((queue.root/'authority').read_text(), 'v2')
            self.assertEqual(r.scheduler_version(), 'v1')
            with queue.transaction() as data:
                self.assertEqual(data['authority'], 'v1')

    def test_frozen_recovery_can_acquire_after_config_rollback(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp,
                                                              AGENT_CI_SCHEDULER='v1'):
            (Path(temp)/'ci').mkdir()
            (Path(temp)/'ci/freeze').touch()
            queue = r.Queue()
            with queue.transaction() as data:
                data['jobs']['old'] = quarantine()
            request = dict(project='fixture', lane='', sha='', tree='recovery', mode='run',
                           command_digest='a'*64, generation=1, slots=1, workers=1,
                           memory_mb=4096, leases=['cleanup:fixture'], recovery_of='old')
            limits = dict(MAX_JOBS=2, MAX_WORKERS=4, CPU_RESERVE=1,
                          MEM_RESERVE_MB=1024, WORKER_MB=2048, MAX_JOB_MB=8192)
            metrics = dict(cores=8, load=0, available_mb=32000, total_mb=32000,
                           pressure=1, swap_mb=0)
            with patch.object(r, 'policy', return_value=limits), \
                    patch.object(r, 'scheduler_sample', return_value=metrics):
                result = r.acquire_v2(queue, 'recovery', os.getpid(), time.time()+60,
                                      limits, request, 'runner')
            self.assertEqual(result.get('workers'), 1, result)
            queue.release('recovery', 0)

    def test_frozen_maintenance_recovery_resolves_saved_lease(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp,
                                                              AGENT_CI_SCHEDULER='v1'):
            base = Path(temp)/'ci/projects/fixture/results'
            base.mkdir(parents=True)
            pending = base/'old.recovery'
            pending.write_text('fixture')
            (Path(temp)/'ci/freeze').touch()
            queue = r.Queue()
            with queue.transaction() as data:
                data['jobs']['old'] = quarantine()
            with patch.object(sys, 'argv', ['scheduler_maintenance.py', 'recover', 'old']), \
                    patch.object(r, 'acquire_v2', return_value=dict(workers=1)) as acquire, \
                    patch.object(supervisor, 'run', return_value=0):
                self.assertEqual(maintenance.main(), 0)
                self.assertEqual(acquire.call_args[0][5]['recovery_of'], 'old')
            with queue.transaction() as data:
                self.assertEqual(data['jobs']['old']['state'], 'finished')
            self.assertTrue(pending.with_suffix('.recovered').exists())


class MemoryBackfill(unittest.TestCase):
    def test_small_job_backfills_memory_head_then_head_reserves(self):
        # Review 3 permits an age-bounded drain, without duration estimates.
        limits = dict(MAX_JOBS=2, MAX_WORKERS=4, CPU_RESERVE=1,
                      MEM_RESERVE_MB=1024, WORKER_MB=2048, MAX_JOB_MB=8192)
        metrics = dict(cores=8, load=0, available_mb=5120, pressure=1)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp,
                                                              AGENT_CI_SCHEDULER='v1'):
            queue = r.Queue()
            procs = r.processes()
            clock = [110]
            with queue.transaction() as data:
                data['sequence'] = 1
                data['jobs']['head'] = dict(owner=os.getpid(), start=procs[os.getpid()]['start'],
                    sequence=1, submitted_at=100, deadline=200, requested_workers=1,
                    requested_memory_mb=8192)
            with patch.object(r, 'sample', return_value=metrics), \
                    patch.object(r.time, 'time', side_effect=lambda: clock[0]), \
                    patch.object(r.time, 'sleep', side_effect=lambda _: clock.__setitem__(0, clock[0]+1)):
                self.assertEqual(r.acquire(queue, 'small', os.getpid(), 111, limits, 1), (1, 2048))
                queue.release('small')
                clock[0] = 131
                self.assertIsNone(r.acquire(queue, 'late', os.getpid(), 132, limits, 1))
                with patch.object(r, 'sample', return_value=dict(metrics, available_mb=9216)):
                    self.assertEqual(r.acquire(queue, 'head', os.getpid(), 200, limits, 1,
                                               memory_mb=8192), (1, 8192))


class SubmissionGating(unittest.TestCase):
    setUp = host_fixtures.HostKit.setUp
    git = host_fixtures.HostKit.git
    remote_ci = host_fixtures.HostKit.remote_ci
    host_kit = host_fixtures.HostKit.host_kit

    def runnable_host(self):
        kit = self.host_kit()
        (kit/'run.sh').write_text('#!/bin/bash\ndir="$HOME/ci/projects/$1"\n'
            ': >"$dir/logs/$4.log"\necho 0 >"$dir/results/$4.exit"\n')
        stub = self.home/'stubs/git'
        stub.write_text(stub.read_text().replace('sys.exit(98)', 'sys.exit(0)'))

    def test_default_v1_skips_metadata_helper_upload_and_ledger(self):
        self.runnable_host()
        (self.kit/'remote/scheduler_submit.py').write_text('raise SystemExit(93)\n')
        call = self.remote_ci('check', '--summary')
        self.assertEqual(call.returncode, 0, call.stdout + call.stderr)
        self.assertTrue((self.home/'push-args').exists())
        self.assertEqual(list(self.project.glob('runs/*/submission.json*')), [])
        self.assertFalse((self.home/'.local/state/agent-ci').exists())

    def test_v2_uploads_versioned_submission_only_when_enabled(self):
        self.runnable_host()
        config = self.home/'.config/agent-lanes/config'
        config.parent.mkdir(parents=True)
        config.write_text('AGENT_CI_SCHEDULER=v2\n')
        call = self.remote_ci('check', '--summary')
        self.assertEqual(call.returncode, 0, call.stdout + call.stderr)
        submissions = list(self.project.glob('runs/*/submission.json'))
        self.assertEqual(len(submissions), 1)
        row = json.loads(submissions[0].read_text())
        self.assertEqual(row['schema'], 'agent-ci.submission/v1')
        self.assertEqual(row['protocol'], 2)
        self.assertNotIn('command', row)


if __name__ == '__main__':
    unittest.main()
