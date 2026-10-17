"""Durable maintenance pause/resume invariants."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(KIT/'remote'))
import resources
import scheduler
import scheduler_pause as pause

LIMITS = dict(MAX_JOBS=2, MAX_WORKERS=4, CPU_RESERVE=1, MEM_RESERVE_MB=1024,
              WORKER_MB=1024, MAX_JOB_MB=8192, FAST_LANE=0)
METRICS = dict(cores=8, load=0, available_mb=16000, total_mb=24000,
               pressure=1, swap_mb=0)


def request(run='run'):
    return dict(project='fixture', lane='', sha='a'*40, tree='b'*64, mode='run',
                command_digest='c'*64, generation=1, slots=1, workers=1,
                memory_mb=2048, leases=[], run_id=run, park_version=1,
                park_envelope='d'*64)


class PauseState(unittest.TestCase):
    def row(self):
        data = dict(sequence=0, jobs={}, paused=dict(version=1))
        scheduler.submit(data, 'token', 10, 'start', 100, 200, request())
        data['jobs']['token']['owner_birth'] = 'birth'
        return data

    def test_pause_wins_same_lock_before_grant_and_scheduler_preserves_row(self):
        data = self.row()
        self.assertTrue(pause.park(data, 'token', 10, 'birth', 'd'*64, 101))
        scheduler.schedule(data, METRICS, LIMITS, {}, 102)
        row = data['jobs']['token']
        self.assertEqual(row['state'], 'parked')
        self.assertNotIn('owner', row)
        self.assertNotIn('result', row)
        self.assertEqual((row['sequence'], row['submitted_at'], row['deadline']), (1, 100, 200))

    def test_fast_lane_cannot_grant_while_paused(self):
        data = self.row()
        fast = dict(LIMITS, FAST_LANE=1, FAST_WORKERS=1, FAST_MEMORY_MB=4096,
                    FAST_SECONDS=60)
        scheduler.schedule(data, METRICS, fast,
                           {10: dict(start='start', birth='birth', group=10, rss=0)}, 101)
        self.assertEqual(data['jobs']['token']['state'], 'queued')
        self.assertFalse(data['jobs']['token'].get('workers'))

    def test_grant_wins_and_cannot_be_parked(self):
        data = self.row()
        data.pop('paused')
        scheduler.schedule(data, METRICS, LIMITS,
                           {10: dict(start='start', birth='birth', group=10, rss=0)}, 100)
        data['paused'] = dict(version=1)
        self.assertEqual(data['jobs']['token']['state'], 'running')
        self.assertFalse(pause.park(data, 'token', 10, 'birth', 'd'*64, 101))

    def test_unadvertised_request_never_becomes_parkable(self):
        data = dict(sequence=0, jobs={}, paused=dict(version=1))
        old = request()
        old.pop('park_version')
        old.pop('park_envelope')
        scheduler.submit(data, 'old', 10, 'start', 100, 200, old)
        data['jobs']['old']['owner_birth'] = 'birth'
        self.assertFalse(pause.park(data, 'old', 10, 'birth', 'd'*64, 101))

    def test_starting_without_persisted_launcher_identity_becomes_stale(self):
        row = dict(state='starting', resume_starting_at=10, resume_nonce='nonce')
        self.assertTrue(pause.stale_starting(row, {}, 41))
        row.update(resume_owner=20, resume_owner_birth='birth')
        self.assertFalse(pause.stale_starting(row, {20: dict(birth='birth')}, 41))
        self.assertTrue(pause.stale_starting(row, {20: dict(birth='other')}, 41))

    def test_adoption_is_exact_once_and_refreshes_generation(self):
        data = self.row()
        pause.park(data, 'token', 10, 'birth', 'd'*64, 101)
        old = data['jobs']['token']['lease_generation']
        data['jobs']['token'].update(state='starting', resume_nonce='nonce')
        data['jobs']['token'].update(resume_owner=20, resume_owner_birth='newbirth')
        self.assertTrue(pause.adopt(data, 'token', 20, 'new', 'newbirth', 'd'*64, 'nonce', 110))
        row = data['jobs']['token']
        self.assertEqual((row['owner'], row['lease_generation']), (20, old+1))
        self.assertFalse(pause.adopt(data, 'token', 21, 'other', 'otherbirth', 'd'*64, 'nonce', 111))

    def test_expired_adoption_publishes_75_without_execution(self):
        data = self.row()
        pause.park(data, 'token', 10, 'birth', 'd'*64, 101)
        data['jobs']['token'].update(state='starting', resume_nonce='nonce')
        data['jobs']['token'].update(resume_owner=20, resume_owner_birth='newbirth')
        self.assertEqual(pause.adopt(data, 'token', 20, 'new', 'newbirth', 'd'*64, 'nonce', 200), 'expired')
        self.assertEqual((data['jobs']['token']['state'], data['jobs']['token']['result']),
                         ('expired', 75))


class DurableRecords(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ci = Path(self.temp.name)/'ci'
        self.queue = resources.Queue(Path(self.temp.name)/'queue')

    def tearDown(self):
        self.temp.cleanup()

    def parked(self, *, corrupt=False):
        path = self.ci/'projects/fixture/runs/run/resume.json'
        path.parent.mkdir(parents=True)
        (path.parent/'bin').mkdir()
        (path.parent/'bin/run.sh').write_text('#!/bin/sh\n')
        (path.parent/'conf').write_text('REMOTE_CI_NAME=fixture\n')
        argv = ['fixture', 'a'*40, 'b'*64, 'run', '', '0', 'runner', '', '100']
        deadline = int(time.time()) + 100
        record = dict(version=1, argv=argv, token='token', deadline=deadline,
                      config_sha256=pause.digest(path.parent/'conf'),
                      kit_sha256=pause.kit_digest(path.parent))
        path.write_text(json.dumps(record) if not corrupt else '{')
        path.chmod(0o600)
        with self.queue.transaction() as data:
            data['jobs']['token'] = dict(version=2, protocol=2, project='fixture',
                run_id='run', sha='a'*40, tree='b'*64, state='parked', park_version=1,
                park_envelope=pause.digest(path), parked_owner=10,
                parked_owner_birth='old', sequence=1, lease_generation=1,
                submitted_at=100, deadline=deadline, requested=dict(
                    slots=1, workers=1, memory_mb=2048, leases=[]))
        return path

    def test_audit_waits_for_old_owner_exit_and_rejects_corruption(self):
        path = self.parked()
        live = {10: dict(birth='old')}
        self.assertEqual(pause.audit(self.queue, self.ci, live)['blockers'], 1)
        self.assertEqual(pause.audit(self.queue, self.ci, {})['parked'], 1)
        path.write_text('{')
        self.assertEqual(pause.audit(self.queue, self.ci, {})['blockers'], 1)

    def test_terminal_finalizer_is_idempotent_after_scheduler_completion(self):
        self.parked()
        with self.queue.transaction() as data:
            pause.terminalize_parked(data, 'token', 74, time.time())
        self.assertEqual(pause.expire(self.queue, self.ci, 'fixture', 'run', time.time()), 74)
        self.assertEqual(pause.expire(self.queue, self.ci, 'fixture', 'run', time.time()), 74)
        self.assertEqual((self.ci/'projects/fixture/results/run.exit').read_text(), '74\n')

    def test_observer_never_publishes_a_normally_finishing_run(self):
        self.parked()
        with self.queue.transaction() as data:
            data['jobs']['token'].update(state='running', owner=20, owner_birth='live')
            scheduler.complete(data, 'token', 0, time.time())
        pause.audit(self.queue, self.ci, {20: dict(birth='live')})
        self.assertFalse((self.ci/'projects/fixture/results/run.exit').exists())

    def test_crashed_controller_before_launcher_publication_can_resume(self):
        import scheduler_resume
        self.parked()
        with self.queue.transaction() as data:
            data['jobs']['token'].update(state='starting', resume_nonce='nonce',
                                        resume_starting_at=time.time()-31)
        self.assertTrue(scheduler_resume.recover(self.queue, 'token'))
        with self.queue.transaction() as data:
            self.assertEqual(data['jobs']['token']['state'], 'parked')
            self.assertNotIn('resume_nonce', data['jobs']['token'])

    def test_pre_admission_hang_is_reaped_without_grant_or_result(self):
        import scheduler_resume
        import scheduler_process
        import subprocess
        self.parked()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 start_new_session=True)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        birth = scheduler_process.snapshot()[child.pid]['birth']
        with self.queue.transaction() as data:
            data['jobs']['token'].update(state='starting', resume_nonce='nonce',
                resume_starting_at=time.time()-31, resume_owner=child.pid,
                resume_owner_birth=birth)
        # Popen.wait reaps the root zombie; the real detached owner is reaped
        # by the system. A second recovery must retain the same original run.
        scheduler_resume.recover(self.queue, 'token')
        child.wait(timeout=15)
        scheduler_resume.recover(self.queue, 'token')
        with self.queue.transaction() as data:
            row = data['jobs']['token']
            self.assertEqual(row['state'], 'parked')
            self.assertFalse(row.get('workers'))
            self.assertNotIn('result', row)
        self.assertFalse((self.ci/'projects/fixture/results/run.exit').exists())

    def test_audit_expires_disconnected_park_without_blocking_install(self):
        self.parked()
        with self.queue.transaction() as data:
            data['jobs']['token']['deadline'] = time.time()-1
        self.assertEqual(pause.audit(self.queue, self.ci, {})['blockers'], 0)
        self.assertEqual((self.ci/'projects/fixture/results/run.exit').read_text(), '75\n')

    def test_deadline_controller_terminalizes_and_publishes_original_result(self):
        self.parked()
        with self.queue.transaction() as data:
            data['jobs']['token']['deadline'] = 100
        self.assertEqual(pause.expire(self.queue, self.ci, 'fixture', 'run', 100), 75)
        with self.queue.transaction() as data:
            self.assertEqual((data['jobs']['token']['state'], data['jobs']['token']['result']),
                             ('expired', 75))
        self.assertEqual((self.ci/'projects/fixture/results/run.exit').read_text(), '75\n')

    def test_pause_refuses_any_incompatible_waiter_but_keeps_intake_frozen(self):
        with self.queue.transaction() as data:
            data['jobs']['old'] = dict(version=2, state='queued')
        self.assertEqual(pause.request_pause(self.queue, self.ci, 'upgrade', {}), 1)
        self.assertTrue((self.ci/'freeze').exists())
        with self.queue.transaction() as data:
            self.assertNotIn('paused', data)

    def test_pause_refuses_old_running_and_recovery_authorities(self):
        for state in ('running', 'quarantined'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                ci = Path(temp)/'ci'
                queue = resources.Queue(Path(temp)/'queue')
                with queue.transaction() as data:
                    data['jobs']['old'] = dict(version=2, state=state,
                        workers=1 if state == 'running' else 0,
                        quarantined_leases=['cleanup:fixture'] if state == 'quarantined' else [])
                self.assertEqual(pause.request_pause(queue, ci, 'upgrade', {}), 1)
                with queue.transaction() as data:
                    self.assertNotIn('paused', data)

    def test_resume_preserves_a_manual_freeze(self):
        self.ci.mkdir(parents=True)
        (self.ci/'freeze').write_text('manual\n')
        self.assertEqual(pause.request_pause(self.queue, self.ci, 'upgrade', {}), 0)
        self.assertEqual(pause.resume(self.queue, self.ci, {}), 0)
        self.assertEqual((self.ci/'freeze').read_text(), 'manual\n')

    def test_installer_uses_safe_park_audit_not_active_count_bypass(self):
        source = (KIT/'remote/install-kit.sh').read_text()
        self.assertIn('scheduler_pause.py" audit', source)
        self.assertNotIn('resources.py" active-count', source)

    def test_gated_launcher_cannot_execute_before_publication(self):
        marker = Path(self.temp.name)/'launched'
        command = [sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).touch()', str(marker)]
        child = pause.gated_launcher(command)
        time.sleep(.1)
        self.assertFalse(marker.exists())
        child.stdin.close()
        child.wait(timeout=15)
        self.assertFalse(marker.exists(), 'closed publication gate executed work')
        child = pause.gated_launcher(command)
        child.stdin.write(b'1')
        child.stdin.close()
        child.wait(timeout=15)
        self.assertTrue(marker.exists())


if __name__ == '__main__':
    unittest.main()
