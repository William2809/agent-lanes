"""Fast-lane integration requirements against the real v2 scheduler."""
import os
from pathlib import Path
import sys
import unittest


sys.dont_write_bytecode = True
sys.path.insert(0, os.environ.get(
    'CIQUEUE_FAST_KIT', str(Path(__file__).resolve().parent / 'remote')))
import scheduler as s


LIMITS = dict(MAX_JOBS=4, MAX_WORKERS=4, CPU_RESERVE=2,
              MEM_RESERVE_MB=2048, WORKER_MB=2048,
              FAST_LANE=1, FAST_WORKERS=1, FAST_MEMORY_MB=2048,
              FAST_SECONDS=60)
METRICS = dict(cores=8, load=0, available_mb=12288, total_mb=16384,
               pressure=1, swap_mb=0)
PROCS = {10: dict(start='birth', group=10, rss=0)}


class FastScheduler(unittest.TestCase):
    def setUp(self):
        self.data = dict(sequence=0, jobs={})

    def submit(self, token, now=100, fast=False, **kwargs):
        request = dict(project=token, lane='', sha='a' * 40, tree=token,
                       mode='run', command_digest='b' * 64, slots=1,
                       workers=1, memory_mb=2048, leases=[], generation=1)
        if fast:
            request.update(profile_known=True, predicted_s=30,
                           estimate_scope='job', profile='c' * 64)
        request.update(kwargs)
        s.submit(self.data, token, 10, 'birth', now, now + 1000, request)
        return self.data['jobs'][token]

    def tick(self, now=100, metrics=None, limits=None):
        s.schedule(self.data, metrics or METRICS, limits or LIMITS, PROCS, now)

    def complete(self, token, now):
        s.complete(self.data, token, 0, now)

    def test_three_normal_jobs_preserve_full_fast_capacity_and_checkout(self):
        for token in ('normal-a', 'normal-b', 'normal-c'):
            self.submit(token, project='shared', slots=4)
        self.tick()
        self.assertTrue(all(self.data['jobs'][token]['state'] == 'running'
                            for token in ('normal-a', 'normal-b', 'normal-c')))
        fast = self.submit('fast', project='shared', slots=4, fast=True)
        self.tick(101)
        self.assertEqual((fast['state'], fast['slot'], fast['workers'], fast['memory_mb']),
                         ('running', 4, 1, 2048))
        self.assertEqual(fast['lane_class'], 'fast')
        self.assertEqual(fast['job_deadline'], 161)

    def test_unknown_estimate_stays_normal_and_cannot_take_reserve(self):
        for token in ('a', 'b', 'c'):
            self.submit(token)
        unknown = self.submit('unknown', profile_known=False, predicted_s=10,
                              estimate_scope='job')
        self.tick()
        self.assertEqual(unknown['state'], 'queued')
        self.assertNotEqual(unknown.get('lane_class'), 'fast')

    def test_worker_memory_and_job_caps_hold_across_projects(self):
        limits = dict(LIMITS, MAX_JOBS=3, MAX_WORKERS=3)
        metrics = dict(METRICS, available_mb=8192, total_mb=10240)
        for token in ('a', 'b', 'c', 'd'):
            self.submit(token, fast=token == 'd')
        self.tick(metrics=metrics, limits=limits)
        running = [row for row in self.data['jobs'].values() if row['state'] == 'running']
        self.assertLessEqual(len(running), 3)
        self.assertLessEqual(sum(row['workers'] for row in running), 3)
        self.assertLessEqual(sum(row['memory_mb'] for row in running), 6144)

    def test_active_lease_conflict_blocks_fast_job(self):
        active = self.submit('active', leases=['ports:tcp:8000-8010'])
        self.tick()
        fast = self.submit('fast', fast=True, leases=['ports:tcp:8005'])
        self.tick(101)
        self.assertEqual(active['state'], 'running')
        self.assertEqual(fast['state'], 'queued')
        self.assertEqual(fast['blocker']['holder'], 'active')

    def test_quarantine_blocks_only_its_resources(self):
        dirty = self.submit('dirty', project='p', leases=['cleanup:p'],
                            cleanup_required=True)
        self.tick()
        s.complete(self.data, 'dirty', 76, 101, quarantine=True)
        blocked = self.submit('blocked', project='p', leases=['cleanup:p'], fast=True)
        unrelated = self.submit('unrelated', project='q', fast=True)
        self.tick(102)
        self.assertEqual(dirty['state'], 'quarantined')
        self.assertEqual(blocked['state'], 'queued')
        self.assertEqual(unrelated['state'], 'running')

    def test_aged_wide_job_drains_and_then_runs(self):
        wide = self.submit('wide', now=100, workers=4, memory_mb=8192)
        fast = self.submit('fast', now=401, fast=True)
        self.tick(401)
        self.assertEqual(wide['state'], 'running')
        self.assertTrue(wide['drain'])
        self.assertEqual(fast['state'], 'queued')

    def test_aged_long_job_survives_continuous_short_work(self):
        holder = self.submit('holder')
        self.tick(100)
        long = self.submit('long', now=101, workers=3, memory_mb=6144)
        for index, now in enumerate((102, 202, 302, 402)):
            short = self.submit('short-%d' % index, now=now, fast=True)
            self.tick(now)
            if short['state'] == 'running':
                self.complete('short-%d' % index, now + 1)
        self.complete('holder', 403)
        self.tick(404)
        self.assertEqual(long['state'], 'running')

    def test_fast_job_never_takes_aged_wide_heads_lease(self):
        holder = self.submit('holder')
        self.tick(100)
        head = self.submit('head', now=101, workers=4, memory_mb=8192,
                           leases=['ports:tcp:9000'])
        fast = self.submit('fast', now=402, fast=True, leases=['ports:tcp:9000'])
        self.tick(402)
        self.assertEqual(holder['state'], 'running')
        self.assertEqual(head['state'], 'queued')
        self.assertEqual(fast['state'], 'queued')

    def test_pressure_and_swap_growth_stop_all_new_admission(self):
        pressure = self.submit('pressure', fast=True)
        self.tick(metrics=dict(METRICS, pressure=2))
        self.assertEqual(pressure['state'], 'queued')
        self.data['sample'] = dict(at=100, swap_mb=0)
        swap = self.submit('swap', now=101, fast=True)
        self.tick(101, metrics=dict(METRICS, swap_mb=1))
        self.assertEqual(swap['state'], 'queued')

    def test_one_cpu_headroom_cannot_be_reused_by_simultaneous_grants(self):
        self.submit('a', fast=True)
        self.submit('b', fast=True)
        self.tick(metrics=dict(METRICS, cores=6, load=3))
        running = [row for row in self.data['jobs'].values() if row['state'] == 'running']
        self.assertEqual(len(running), 1)

    def test_single_job_host_disables_reserve_but_still_progresses(self):
        only = self.submit('only', fast=True)
        self.tick(limits=dict(LIMITS, MAX_JOBS=1, MAX_WORKERS=1),
                  metrics=dict(METRICS, cores=3, available_mb=4096, total_mb=6144))
        self.assertEqual(only['state'], 'running')

    def test_impossible_requests_reject_without_blocking_valid_work(self):
        workers = self.submit('workers', workers=5)
        memory = self.submit('memory', memory_mb=14337)
        valid = self.submit('valid', fast=True)
        self.tick()
        self.assertEqual((workers['state'], workers['result']), ('rejected', 75))
        self.assertEqual((memory['state'], memory['result']), ('rejected', 75))
        self.assertEqual(valid['state'], 'running')

    def test_lowered_host_job_ceiling_rejects_already_queued_request(self):
        for flag in (0, 1):
            self.data = dict(sequence=0, jobs={})
            queued = self.submit('large', memory_mb=4096)
            self.tick(limits=dict(LIMITS, FAST_LANE=flag, MAX_JOB_MB=2048))
            self.assertEqual((queued['state'], queued['result']), ('rejected', 75))
            self.assertNotIn('workers', queued)

    def test_rejected_lease_proposal_does_not_consume_followers_capacity(self):
        a = self.submit('a', fast=True, leases=['ports:tcp:9000'])
        b = self.submit('b', fast=True, leases=['ports:tcp:9000'])
        c = self.submit('c', fast=True)
        self.tick(limits=dict(LIMITS, MAX_JOBS=2, MAX_WORKERS=2))
        self.assertEqual([a['state'], b['state'], c['state']], ['running', 'queued', 'running'])
        self.assertEqual(b['blocker']['holder'], 'a')
        self.assertEqual(b['blocker']['reason'], 'lease:ports:tcp:9000')
        self.assertNotIn('granted', b)

    def test_rejected_growth_proposal_does_not_hold_phantom_memory(self):
        large = self.submit('large', memory_mb=1024, initial_memory_mb=1024,
                            memory_ceiling_mb=8192, phase_memory_version=1)
        small = self.submit('small', memory_mb=3072)
        self.tick(metrics=dict(METRICS, available_mb=7168))
        self.assertEqual(large['state'], 'queued')
        self.assertEqual(large['blocker']['reason'], 'memory-growth-reserve')
        self.assertEqual(small['state'], 'running')

    def test_idle_fast_capacity_is_loaned_without_preempting_running_jobs(self):
        limits = dict(LIMITS, FAST_BORROW=1)
        normal = [self.submit(t, project='shared', slots=4) for t in ('a','b','c','d')]
        self.tick(limits=limits)
        self.assertTrue(all(row['state'] == 'running' for row in normal))
        quick = self.submit('quick', now=101, project='shared', slots=4, fast=True)
        self.tick(101, limits=limits)
        self.assertEqual(quick['state'], 'queued')
        self.assertTrue(all(row['state'] == 'running' for row in normal))
        self.complete('a', 102)
        self.tick(103, limits=limits)
        self.assertEqual((quick['state'],quick['lane_class']), ('running','fast'))
        self.assertEqual(quick['job_deadline'],163)

    def test_borrow_preserves_eligible_fast_preference(self):
        normal = self.submit('normal')
        quick = self.submit('quick', fast=True)
        self.tick(limits=dict(LIMITS, MAX_JOBS=2, MAX_WORKERS=2, FAST_BORROW=1))
        self.assertEqual(quick['lane_class'], 'fast')
        self.assertEqual(normal['state'], 'running')

    def test_blocked_fast_job_does_not_idle_unrelated_capacity(self):
        limits = dict(LIMITS, MAX_JOBS=2, MAX_WORKERS=2, FAST_BORROW=1)
        holder = self.submit('holder', leases=['ports:tcp:9000'])
        self.tick(limits=limits)
        quick = self.submit('quick', fast=True, leases=['ports:tcp:9000'])
        normal = self.submit('normal')
        self.tick(101, limits=limits)
        self.assertEqual((holder['state'], quick['state'], normal['state']),
                         ('running','queued','running'))
        self.assertEqual(quick['blocker']['holder'], 'holder')

    def test_borrow_never_overrides_pressure_or_physical_caps(self):
        a = self.submit('a')
        self.tick(limits=dict(LIMITS, FAST_BORROW=1), metrics=dict(METRICS, pressure=2))
        self.assertEqual(a['state'], 'queued')
        self.assertEqual(a['blocker']['reason'], 'pressure')
        self.tick(131, limits=dict(LIMITS, FAST_BORROW=1, MAX_JOBS=1))
        b = self.submit('b', now=132)
        self.tick(132, limits=dict(LIMITS, FAST_BORROW=1, MAX_JOBS=1))
        self.assertEqual((a['state'],b['state']), ('running','queued'))

    def test_project_service_history_changes_normal_selection(self):
        limits = dict(LIMITS, MAX_JOBS=1, MAX_WORKERS=1)
        self.data['served'] = {'heavy': 1000, 'light': 0}
        heavy = self.submit('heavy-job', project='heavy')
        light = self.submit('light-job', project='light')
        self.tick(limits=limits)
        self.assertEqual(light['state'], 'running')
        self.assertEqual(heavy['state'], 'queued')


if __name__ == '__main__':
    unittest.main()
