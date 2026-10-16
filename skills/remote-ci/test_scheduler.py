"""Requirements from the ciqueue brief. Synthetic hosts; no application suites."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, os.environ.get('CIQUEUE_BASELINE_REMOTE', str(Path(__file__).parent / 'remote')))
import resources as r
import scheduler as s

LIMITS = dict(MAX_JOBS=2, MAX_WORKERS=4, CPU_RESERVE=2,
              MEM_RESERVE_MB=2048, WORKER_MB=2048)
METRICS = dict(cores=10, load=0, available_mb=16000, total_mb=24000, pressure=1, swap_mb=0)
PROCS = {10: dict(start='birth', group=10, rss=0)}


class Scheduler(unittest.TestCase):
    def setUp(self):
        self.data = dict(sequence=0, jobs={})

    def submit(self, token, now=100, **kwargs):
        req = dict(project=token, lane='lane', sha='a'*40, tree=token,
                   mode='run', command_digest='b'*64, slots=1, workers=1,
                   memory_mb=2048, leases=[], generation=1)
        req.update(kwargs)
        s.submit(self.data, token, 10, 'birth', now, now+1000, req)
        return self.data['jobs'][token]

    def tick(self, now=100, metrics=None, procs=None):
        s.schedule(self.data, METRICS if metrics is None else metrics, LIMITS,
                   PROCS if procs is None else procs, now)

    def test_kernel_birth_survives_display_start_time_jitter(self):
        holder = self.submit('holder', project='shared')
        queued = self.submit('queued', project='shared')
        for row in (holder, queued):
            row['owner_birth'] = '12345'
        procs = {10: dict(start='shifted display time', birth='12345', group=10, rss=0)}
        self.tick(procs=procs)
        self.assertEqual(holder['state'], 'running')
        self.assertEqual(queued['state'], 'queued')
        self.assertTrue(r.live(queued, procs))
        # PID reuse, missing birth and a missing process still fail closed.
        for other in ({10: dict(start='birth', birth='12346')},
                      {10: dict(start='birth')}, {}):
            self.assertFalse(r.live(queued, other))
        self.tick(now=101, procs={10: dict(start='birth', birth='12346')})
        self.assertEqual(queued['state'], 'lost')
        self.assertEqual(queued['result'], 74)

    def test_live_cancelled_owner_keeps_lease_for_cleanup(self):
        row = self.submit('cancelled')
        self.tick()
        row['cancel'] = True
        self.tick(now=101)
        self.assertEqual(row['state'], 'running')
        self.assertEqual(row['workers'], 1)
        # Finishing cleanup cannot turn the cancelled check green.
        s.complete(self.data, 'cancelled', 0, 102)
        self.assertEqual(row['state'], 'finished')
        self.assertEqual(row['result'], 74)
        self.assertNotIn('workers', row)

    def test_atomic_grant_and_project_slots(self):
        self.submit('a', project='p', slots=1, leases=['template:p'])
        self.submit('b', project='p', slots=1)
        self.tick()
        a, b = (self.data['jobs'][t] for t in ('a', 'b'))
        self.assertEqual((a['state'], a['slot'], a['workers'], a['memory_mb']),
                         ('running', 1, 1, 2048))
        self.assertEqual(b['state'], 'queued')
        self.assertEqual(b['blocker']['holder'], 'a')
        self.assertNotIn('slot', b)
        self.assertNotIn('workers', b)

    def test_concurrent_transactions_assign_unique_host_sequence(self):
        """Follow-up 3: simultaneous submitters cannot share a sequence or slot."""
        script = '''
import json, sys
import resources, scheduler
q = resources.Queue(sys.argv[1])
i = int(sys.argv[2])
req = dict(project='p'+str(i%3), lane='', sha='', tree=str(i), mode='run',
           command_digest='a'*64, generation=1, slots=1, workers=1, memory_mb=2048, leases=[])
with q.transaction() as data:
    scheduler.submit(data, str(i), 10, 'birth', 100, 1000, req)
    scheduler.schedule(data, json.loads(sys.argv[3]), json.loads(sys.argv[4]),
                       {10: dict(start='birth', group=10, rss=0)}, 100)
'''
        with tempfile.TemporaryDirectory() as root:
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent/'remote'))
            children = [subprocess.Popen([sys.executable, '-B', '-c', script, root, str(i),
                         json.dumps(METRICS), json.dumps(LIMITS)], env=env) for i in range(10)]
            for child in children:
                self.assertEqual(child.wait(timeout=15), 0)
            with r.Queue(root).transaction() as data:
                rows = list(data['jobs'].values())
            self.assertEqual(sorted(row['sequence'] for row in rows), list(range(1, 11)))
            running = [row for row in rows if row['state'] == 'running']
            self.assertEqual(len(running), 2)
            self.assertEqual(len({row['project'] for row in running}), 2)
            for row in rows:
                if row['state'] == 'queued':
                    self.assertFalse({'slot', 'workers', 'memory_mb'} & row.keys())

    def test_aggregate_workers_memory_and_leases(self):
        self.submit('a', workers=3, leases=['ports:tcp:8000-8010'])
        self.tick()
        self.submit('b', workers=2)
        self.submit('c', leases=['ports:tcp:8009-8020'])
        self.tick()
        self.assertEqual(self.data['jobs']['b']['blocker']['reason'], 'workers')
        self.assertEqual(self.data['jobs']['c']['blocker']['reason'], 'lease:ports:tcp:8009-8020')
        self.assertNotIn('workers', self.data['jobs']['b'])
        self.assertNotIn('workers', self.data['jobs']['c'])

    def test_safe_backfill_reserves_head_capacity(self):
        self.submit('a', project='p', leases=['template:p'])
        self.tick()
        self.submit('head', project='p', leases=['template:p'])
        self.submit('backfill')
        self.tick()
        self.assertEqual(self.data['jobs']['backfill']['state'], 'running')
        s.complete(self.data, 'a', 0, 101)
        self.tick(101)
        self.assertEqual(self.data['jobs']['head']['state'], 'running')

    def test_unsafe_backfill_and_bounded_drain(self):
        self.submit('a', project='p', workers=2)
        self.tick()
        self.submit('head', project='p', workers=3)
        self.submit('big', workers=2)
        self.tick()
        self.assertEqual(self.data['jobs']['big']['state'], 'queued')
        self.submit('small', now=401)
        self.tick(401)
        self.assertEqual(self.data['jobs']['small']['state'], 'queued')
        s.complete(self.data, 'a', 0, 402)
        self.tick(402)
        self.assertEqual(self.data['jobs']['head']['state'], 'running')

    def test_backfill_reserves_unsampled_cpu_commitments(self):
        """Brief 2: backfill cannot use CPU that the blocked head needs."""
        self.submit('active', project='p')
        self.tick()
        head = self.submit('head', project='p', workers=2)
        later = self.submit('later')
        # Eight usable cores minus five sampled load, one active worker and one
        # new worker leaves only one core. The two-worker head needs two.
        self.tick(101, metrics=dict(METRICS, load=5))
        self.assertEqual(head['state'], 'queued')
        self.assertEqual(later['state'], 'queued')

    def test_exclusive_head_drains_and_slots_one_is_not_exclusive(self):
        self.submit('a')
        self.tick()
        self.submit('exclusive', leases=['exclusive-host'])
        self.submit('later')
        self.tick()
        self.assertEqual(self.data['jobs']['later']['state'], 'queued')
        s.complete(self.data, 'a', 0, 101)
        self.tick(101)
        self.assertEqual(self.data['jobs']['exclusive']['state'], 'running')
        self.assertEqual(self.data['jobs']['later']['state'], 'queued')

    def test_identical_requests_follow_one_result(self):
        self.submit('a', project='p', tree='same')
        self.tick()
        self.submit('b', project='p', tree='same', generation=2)
        self.assertEqual(self.data['jobs']['b']['follow'], 'a')
        self.tick()
        self.assertNotIn('workers', self.data['jobs']['b'])
        s.complete(self.data, 'a', 1, 102)
        self.tick(102)
        self.assertEqual(self.data['jobs']['b']['result'], 1)

    def test_supersede_queued_only_and_preserve_gate(self):
        self.submit('active', project='p', tree='old', generation=1)
        self.tick()
        self.submit('gate', project='p', tree='gate', generation=2, landing=True)
        self.submit('queued', project='p', tree='middle', generation=3)
        self.submit('new', project='p', tree='new', generation=4)
        self.assertEqual(self.data['jobs']['queued']['result'], 77)
        self.assertEqual(self.data['jobs']['gate']['state'], 'queued')
        self.assertEqual(self.data['jobs']['active']['state'], 'running')
        self.submit('late', project='p', generation=2)
        self.assertEqual(self.data['jobs']['late']['result'], 77)

    def test_stale_owner_children_and_cleanup_quarantine(self):
        self.submit('dead')
        self.submit('child', cleanup_required=True)
        self.tick()
        self.data['jobs']['child']['process'] = dict(group=20, generation=2,
                                                    members={'21': 'child'})
        self.tick(101, procs={21: dict(start='child', group=20, rss=1024)})
        self.assertEqual(self.data['jobs']['dead']['result'], 74)
        self.assertEqual(self.data['jobs']['child']['state'], 'running')
        # The host reaper confirms the group gone before it clears the record.
        self.data['jobs']['child'].pop('process')
        self.tick(102, procs={})
        self.assertEqual(self.data['jobs']['child']['state'], 'quarantined')
        # Follow-up: a reaped cleanup failure quarantines resources, not capacity.
        self.assertNotIn('workers', self.data['jobs']['child'])
        self.assertEqual(self.data['jobs']['child']['slot'], 1)

    def test_deadline_expires_without_grant(self):
        self.submit('a')
        self.tick(1100)
        self.assertEqual(self.data['jobs']['a']['result'], 75)
        self.assertNotIn('workers', self.data['jobs']['a'])

    def test_pressure_swap_growth_and_recovery(self):
        self.submit('a')
        self.tick(metrics=dict(METRICS, pressure=2))
        self.assertEqual(self.data['jobs']['a']['state'], 'queued')
        self.tick(101, metrics=dict(METRICS, swap_mb=10))
        self.tick(110, metrics=dict(METRICS, swap_mb=10))
        self.assertEqual(self.data['jobs']['a']['state'], 'queued')
        self.tick(132, metrics=dict(METRICS, swap_mb=10))
        self.assertEqual(self.data['jobs']['a']['state'], 'running')

    def test_no_raw_rss_credit_and_memory_budget(self):
        self.submit('a', memory_mb=6000)
        self.tick()
        self.data['jobs']['a']['group'] = 10
        self.submit('b', memory_mb=6000)
        self.tick(metrics=dict(METRICS, available_mb=10000),
                  procs={10: dict(start='birth', group=10, rss=6000*1024)})
        self.assertEqual(self.data['jobs']['b']['blocker']['reason'], 'memory')
        self.assertEqual(self.data['jobs']['b']['state'], 'queued')

    def test_metadata_is_allowlisted(self):
        row = self.submit('a', command='SECRET SENTINEL', password='SECRET SENTINEL')
        self.tick()
        self.assertNotIn('SECRET SENTINEL', json.dumps(self.data))
        for key in ('project', 'lane', 'sha', 'tree', 'mode', 'command_digest',
                    'requested', 'submitted_at', 'generation', 'admitted_at'):
            self.assertIn(key, row)

    def test_changed_slot_contract_cannot_expand_active_serial_job(self):
        self.submit('a', project='p', slots=1)
        self.tick()
        self.submit('b', project='p', slots=2)
        self.tick()
        self.assertEqual(self.data['jobs']['b']['state'], 'queued')
        self.assertEqual(self.data['jobs']['b']['blocker']['reason'], 'project-slot')

    def test_targeted_lint_cannot_supersede_required_test(self):
        """Review P1: task identity separates required targeted commands."""
        test = self.submit('test', project='p', command_digest='a'*64)
        lint = self.submit('lint', project='p', generation=2, command_digest='c'*64)
        self.assertEqual(test['state'], 'queued')
        self.assertEqual(lint['state'], 'queued')

    def test_landing_follower_protects_queued_execution(self):
        """Brief 4: a gate that follows a queued duplicate survives supersession."""
        old = self.submit('old', project='p', tree='same')
        gate = self.submit('gate', project='p', tree='same', landing=True)
        self.submit('new', project='p', tree='new', generation=2)
        self.assertEqual(old['state'], 'queued')
        self.assertEqual(gate['state'], 'following')

    def test_impossible_requests_do_not_drain_host(self):
        """Review P2: permanently impossible limits reject before FIFO drain."""
        worker = self.submit('worker', workers=5)
        memory = self.submit('memory', memory_mb=23000)
        short = self.submit('short')
        self.tick(500)
        self.assertEqual((worker['state'], worker['result']), ('rejected', 75))
        self.assertEqual((memory['state'], memory['result']), ('rejected', 75))
        self.assertEqual(short['state'], 'running')

    def test_cleanup_quarantine_is_scoped_and_recovery_inherits_lease(self):
        """Review P1: dirty persistent state blocks only affected resources."""
        self.submit('dirty', project='p', leases=['exclusive-host', 'cleanup:p'], cleanup_required=True)
        self.tick()
        s.complete(self.data, 'dirty', 76, 101, quarantine=True)
        blocked = self.submit('blocked', project='p', now=102)
        short = self.submit('short', now=500)
        self.tick(500)
        self.assertEqual(blocked['state'], 'queued')
        self.assertEqual(short['state'], 'running')
        recovery = self.submit('recovery', project='p', recovery_of='dirty', leases=['cleanup:p'])
        self.tick(501)
        self.assertEqual(recovery['state'], 'running')
        self.assertEqual(recovery['slot'], 1)
        s.complete(self.data, 'recovery', 0, 502)
        s.complete(self.data, 'dirty', 76, 502)
        self.tick(502)
        self.assertEqual(blocked['state'], 'running')

    def test_process_birth_and_lease_generation_are_required(self):
        """Follow-up 5/6: a reused numeric PGID is not cancellation authority."""
        import scheduler_process as process
        record = dict(group=10, generation=7, members={'10': '100'})
        self.assertEqual(process.members(record, {10: dict(PROCS[10], birth='101')}), {})
        matching = {10: dict(start='original', birth='100', group=10, rss=0)}
        self.assertEqual(set(process.members(record, matching)), {10})
        record['members']['10'] = '101'
        self.assertEqual(set(process.members(record, {10: dict(PROCS[10], birth='101')})), {10})

    def test_ten_mixed_submissions_preserve_required_checks(self):
        """Release check: ten mixed jobs; disconnect and failure are injected."""
        long = self.submit('db', project='db', leases=['template:db'], cleanup_required=True)
        self.tick()
        duplicate = self.submit('duplicate', project='db', tree='db', leases=['template:db'])
        impossible = self.submit('impossible', workers=99)
        required = self.submit('required', project='db', task='tests', generation=2, landing=True)
        self.submit('lint', project='db', task='lint', generation=3)
        old = self.submit('old', project='edit', task='unit', generation=1)
        newer = self.submit('newer', project='edit', task='unit', generation=2)
        shorts = [self.submit('short'+str(i)) for i in range(3)]
        self.assertEqual(len(self.data['jobs']), 10)
        self.tick(101)
        self.assertEqual(impossible['result'], 75)
        self.assertEqual(old['result'], 77)
        self.assertEqual(required['state'], 'queued')
        self.assertEqual(duplicate['state'], 'following')
        # A DB phase ran and was reaped. Disconnect after its side effects.
        long['touched'] = True
        long['owner'] = 99
        self.tick(102)
        self.assertEqual(long['state'], 'quarantined')
        self.assertEqual(duplicate['result'], 74)
        for now in range(103, 110):
            for token, row in list(self.data['jobs'].items()):
                if row.get('state') == 'running':
                    s.complete(self.data, token, 0, now)
            self.tick(now)
        self.assertTrue(all(row['state'] == 'finished' for row in shorts))
        self.assertEqual(newer['state'], 'finished')
        self.assertEqual(required['state'], 'queued')

    def test_legacy_guard_blocks_without_partial_grant(self):
        self.submit('a')
        self.tick(metrics=dict(METRICS, legacy_blocker=dict(reason='legacy-guard', holder='old')))
        self.assertEqual(self.data['jobs']['a']['blocker']['holder'], 'old')
        self.assertNotIn('workers', self.data['jobs']['a'])
        self.tick(101)
        self.assertEqual(self.data['jobs']['a']['state'], 'running')

    def test_memory_model_ignores_killed_and_reads_legacy_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'receipts').mkdir()
            (root/'projects/p/logs').mkdir(parents=True)
            (root/'receipts/good.json').write_text(json.dumps(dict(
                project='p', mode='run', workers=1, peak_rss_mb=3000, exit_code=0)))
            (root/'receipts/killed.json').write_text(json.dumps(dict(
                project='p', mode='run', workers=1, peak_rss_mb=10, exit_code=75)))
            model = s.prediction(root, 'p', 'run', 1, 4096)
            self.assertEqual(model['predicted_mb'], 4006)  # 3000*1.25 + 256 MB
            self.assertEqual(model['samples'], 1)
            (root/'projects/p/logs/old.log').write_text(
                '== resources workers=1 memory_mb=4096\n'
                '== scheduler mode=full\n3145728000  maximum resident set size\n'
                '== finished rc=0 today\n')
            model = s.prediction(root, 'p', 'full', 1, 4096)
            self.assertEqual(model['predicted_mb'], 4006)
            self.assertEqual(model['source'], 'logs')
            self.assertEqual(s.prediction(root, 'new', 'full', 1, 4096)['predicted_mb'], 4096)

    def test_flag_off_policy_and_release_after_flip(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp):
            self.assertEqual(r.scheduler_version(), 'v1')
            q = r.Queue(Path(temp)/'q')
            with q.transaction() as d:
                d['jobs']['v1'] = dict(owner=10, start='birth', workers=1, memory_mb=2048)
            with patch.dict(os.environ, AGENT_CI_SCHEDULER='v2'):
                q.release('v1')
            with q.transaction() as d:
                self.assertEqual(d['jobs'], {})

    def test_flag_switch_rejects_new_v1_admission(self):
        """Migration: old queued runners cannot grant under a second authority."""
        import time
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp, AGENT_CI_SCHEDULER='v2'):
            q = r.Queue(Path(temp)/'q')
            with self.assertRaises(ValueError):
                r.acquire(q, 'old', os.getpid(), time.time()+10, LIMITS, 1, 'runner')
            with q.transaction() as data:
                self.assertEqual(data['jobs'], {})

    def test_completed_lease_rejects_cancellation(self):
        """Follow-up 6: a completed phase is no longer a cancellation target."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp):
            q = r.Queue()
            with q.transaction() as data:
                data['jobs']['done'] = dict(version=2, protocol=2, lease_generation=1,
                    phase='check', state='finished', result=0, owner=0, start='gone')
            with patch.object(sys, 'argv', ['resources.py', 'cancel', '--token', 'done',
                                           '--generation', '1', '--phase', 'check']):
                code = r.main()
            with q.transaction() as data:
                self.assertFalse(data['jobs']['done'].get('cancel'))
            self.assertEqual(code, 74)

    def test_cancelled_grant_cannot_publish_pass(self):
        """Follow-up 6: cancellation before the first phase cannot become pass."""
        from unittest.mock import patch
        self.submit('job')
        self.tick()
        self.data['jobs']['job']['cancel'] = True
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp):
            q = r.Queue()
            with q.transaction() as data:
                data.update(self.data)
            with patch.object(sys, 'argv', ['resources.py', 'release', '--token', 'job', '--result', '0']):
                self.assertEqual(r.main(), 74)
            with q.transaction() as data:
                self.assertEqual(data['jobs']['job']['result'], 74)


if __name__ == '__main__':
    unittest.main()
