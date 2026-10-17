"""Phase grants: resource math plus real lease-bound commands on a remote host."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent/'remote'))
import resources
import scheduler
import scheduler_memory as memory
import scheduler_profiles
import test_scheduler_runner as runner_fixture

LIMITS = dict(MAX_JOBS=8, MAX_WORKERS=12, CPU_RESERVE=1, MEM_RESERVE_MB=1024,
              WORKER_MB=1536, MAX_JOB_MB=8192, INITIAL_MEMORY_MB=3072)
METRICS = dict(cores=16, load=0, available_mb=10240, total_mb=16384, pressure=1, swap_mb=0)
PROCS = {10: dict(start='birth', group=10, rss=0)}


class PhaseMemory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = dict(sequence=0, jobs={})

    def request(self, token, **kwargs):
        row = dict(project=token, lane='', sha='a'*40, tree=token, mode='run',
                   command_digest='b'*64, slots=1, workers=1, memory_mb=6144,
                   leases=[], generation=1, phase_memory_version=1)
        row.update(kwargs)
        return scheduler_profiles.authorize(row, self.root, LIMITS)

    def submit(self, token):
        scheduler.submit(self.data, token, 10, 'birth', 100, 1000, self.request(token))
        return self.data['jobs'][token]

    def tick(self, fast=False):
        limits = dict(LIMITS, FAST_LANE=int(fast), FAST_WORKERS=1, FAST_MEMORY_MB=1024, FAST_SECONDS=60)
        scheduler.schedule(self.data, METRICS, limits, PROCS, 100)

    def test_initial_includes_database_and_distrusts_caller_ceiling(self):
        r = self.request('db', leases=['postgres:18'], memory_ceiling_mb=1, initial_memory_mb=1)
        self.assertEqual((r['memory_mb'], r['memory_ceiling_mb']), (3072, 6144))
        old = self.request('legacy', phase_memory_version=0)
        self.assertEqual(old['memory_mb'], 6144)
        self.assertNotIn('memory_ceiling_mb', old)
        off = scheduler_profiles.authorize(self.request('off'), self.root,
                                           dict(LIMITS, INITIAL_MEMORY_MB=0))
        self.assertNotIn('memory_ceiling_mb', off)
        self.assertNotIn('initial_memory_mb', off)

    def test_setup_admissions_preserve_shared_growth_in_both_planners(self):
        for fast in (False, True):
            with self.subTest(fast=fast):
                self.data = dict(sequence=0, jobs={})
                a, b, c = [self.submit(t) for t in ('a', 'b', 'c')]
                self.tick(fast)
                self.assertEqual([a['state'], b['state'], c['state']], ['running','running','queued'])
                self.assertEqual(memory.admission_memory([a,b]), 9216)
                self.assertIn(c['blocker']['reason'], ('memory-growth-reserve', 'fast-reserve'))
                a['memory_wait'] = dict(at=101, target_mb=6144)
                self.assertTrue(memory.grow(self.data, 'a', METRICS, LIMITS, 101))
                b['memory_wait'] = dict(at=102, target_mb=6144)
                self.assertFalse(memory.grow(self.data, 'b', METRICS, LIMITS, 102))
                self.assertEqual(a['memory_mb']+b['memory_mb'],9216)
                scheduler.complete(self.data, 'a', 0, 103)
                self.assertTrue(memory.grow(self.data, 'b', METRICS, LIMITS, 104))

    def test_growth_waiter_precedes_new_admission_and_pressure_holds_growth(self):
        a = self.submit('a'); self.tick()
        a['memory_wait'] = dict(at=101, target_mb=6144)
        b = self.submit('b'); self.tick()
        self.assertEqual(b['blocker']['reason'], 'memory-growth')
        self.assertFalse(memory.grow(self.data, 'a', dict(METRICS,pressure=4), LIMITS, 102))
        self.assertEqual(a['memory_mb'], 3072)
        self.assertFalse(memory.grow(self.data, 'a', METRICS, LIMITS, 103))
        self.assertTrue(memory.grow(self.data, 'a', METRICS, LIMITS, 133))

    def test_memory_growth_uses_cumulative_swap_window(self):
        a = self.submit('a'); self.tick()
        a['memory_wait'] = dict(at=101, target_mb=6144)
        low = dict(METRICS, available_mb=4000)
        for now, swap in ((101, 16), (106, 32), (111, 48)):
            self.assertFalse(memory.grow(self.data, 'a', dict(low, swap_mb=swap), LIMITS, now))
        self.assertFalse(memory.grow(self.data, 'a', dict(METRICS, swap_mb=64), LIMITS, 116))
        self.assertEqual(self.data['throttle_reason'], 'swap-recovery')
        self.assertTrue(memory.grow(self.data, 'a', dict(METRICS, swap_mb=64), LIMITS, 147))

    def test_oldest_growth_gets_full_increment_not_partial_or_overtaken(self):
        a,b = self.submit('a'),self.submit('b');self.tick()
        a['memory_wait']=dict(at=101,target_mb=6144)
        b['memory_wait']=dict(at=102,target_mb=6144)
        self.assertFalse(memory.grow(self.data, 'b', METRICS, LIMITS, 102))
        self.assertEqual(b['memory_mb'],3072)
        self.assertTrue(memory.grow(self.data, 'a', METRICS, LIMITS, 102))
        self.assertEqual(a['granted']['memory_mb'],6144)

    def test_impossible_eventual_ceiling_rejected_before_setup(self):
        a=self.submit('a')
        scheduler.schedule(self.data,dict(METRICS,total_mb=4096),LIMITS,PROCS,100)
        self.assertEqual(a['state'],'rejected')
        self.assertNotIn('workers',a)

    def test_shrink_only_after_reap_and_matching_generation(self):
        a=self.submit('a');self.tick()
        a['memory_wait']=dict(at=101,target_mb=6144)
        memory.grow(self.data,'a',METRICS,LIMITS,101)
        queue=resources.Queue(self.root/'queue')
        with queue.transaction() as data:data.update(copy.deepcopy(self.data))
        def row():
            with queue.transaction() as data:return copy.deepcopy(data['jobs']['a'])
        memory.release_phase(queue,'a',a['lease_generation']+1,'check')
        self.assertEqual(row()['memory_mb'],6144)
        with queue.transaction() as data:data['jobs']['a']['groups']={'alive':{}}
        memory.release_phase(queue,'a',a['lease_generation'],'check')
        self.assertEqual(row()['memory_mb'],6144)
        with queue.transaction() as data:data['jobs']['a']['groups']={}
        with patch.object(memory,'event'):
            memory.release_phase(queue,'a',a['lease_generation'],'check')
        self.assertEqual(row()['memory_mb'],3072)
        self.assertEqual(row()['peak_grant_mb'],6144)

    def test_growth_timeout_clears_wait_without_launch_and_cleanup_never_waits(self):
        a=self.submit('a');self.tick(); a.update(owner_birth='birth',deadline=time.time()+100)
        queue=resources.Queue(self.root/'queue')
        with queue.transaction() as data:data.update(copy.deepcopy(self.data))
        with patch('scheduler_process.snapshot',return_value={10:dict(birth='birth')}), \
                patch.object(resources,'scheduler_sample',return_value=dict(METRICS,available_mb=1024)), \
                patch.object(resources,'policy',return_value=LIMITS), \
                patch.object(memory,'GROWTH_WAIT_SECONDS',.05), patch.object(memory.time,'sleep'):
            ready,_=memory.prepare(queue,'a','check',True,30,[False])
        self.assertFalse(ready)
        with queue.transaction() as data:
            self.assertNotIn('memory_wait',data['jobs']['a'])
            self.assertFalse(data['jobs']['a'].get('groups'))
        self.assertTrue(memory.prepare(queue,'a','cleanup',True,30,[False])[0])


class PhaseMemoryRunner(unittest.TestCase):
    setUp=runner_fixture.SchedulerRunner.setUp
    git=runner_fixture.SchedulerRunner.git
    configure=runner_fixture.SchedulerRunner.configure
    add_project=runner_fixture.SchedulerRunner.add_project
    stop_runs=runner_fixture.SchedulerRunner.stop_runs
    start=runner_fixture.SchedulerRunner.start
    wait_for=runner_fixture.SchedulerRunner.wait_for
    log=runner_fixture.SchedulerRunner.log
    count=runner_fixture.SchedulerRunner.count
    assert_result=runner_fixture.SchedulerRunner.assert_result
    rows=runner_fixture.SchedulerRunner.rows

    def test_real_command_gets_full_phase_grant_then_returns_extra(self):
        with (self.home/'.config/agent-lanes/config').open('a') as f:
            f.write('AGENT_CI_INITIAL_MEMORY_MB=3072\n')
        with (self.dir/'conf').open('a') as f:f.write('REMOTE_CI_MEMORY_MB=6144\n')
        proc=self.start('phase-memory')
        self.wait_for(lambda:self.count()==1 or proc.poll() is not None)
        self.assertEqual(self.count(),1,self.log('phase-memory'))
        row=next(r for r in self.rows().values() if r.get('state')=='running')
        self.assertEqual(row['requested']['memory_mb'],3072)
        self.assertEqual(row['memory_mb'],6144)
        self.assertTrue(row['process'])
        (self.home/'release').touch()
        self.assert_result(proc,'phase-memory',0)
        self.assertIn('memory grant 3072 -> 6144',self.log('phase-memory'))
        self.assertIn('memory release 6144 -> 3072',self.log('phase-memory'))
        events=[json.loads(s) for s in (self.ci/'telemetry/events.jsonl').read_text().splitlines()]
        self.assertTrue(any(e['event']=='memory-granted' for e in events))
        self.assertTrue(any(e['event']=='memory-released' for e in events))
        self.assertFalse(any(r.get('workers') for r in self.rows().values()))


import test_scheduler_pause_lifecycle as pause_fixture


class PhaseMemoryPause(unittest.TestCase):
    setUp=pause_fixture.PauseLifecycle.setUp
    git=pause_fixture.PauseLifecycle.git
    stop=pause_fixture.PauseLifecycle.stop
    rows=pause_fixture.PauseLifecycle.rows
    evidence=pause_fixture.PauseLifecycle.evidence
    wait=pause_fixture.PauseLifecycle.wait
    launch=pause_fixture.PauseLifecycle.launch

    def control(self, *args):
        if args[0]=='resume':
            # A policy change during upgrade must not rewrite accepted jobs.
            with (self.home/'.config/agent-lanes/config').open('a') as f:
                f.write('AGENT_CI_INITIAL_MEMORY_MB=2048\n')
        return pause_fixture.PauseLifecycle.control(self,*args)

    def test_parked_growth_contract_survives_install_and_policy_change(self):
        with (self.home/'.config/agent-lanes/config').open('a') as f:
            f.write('AGENT_CI_INITIAL_MEMORY_MB=3072\n')
        pause_fixture.PauseLifecycle.test_park_install_resume_original_pooled_job_once(self)
        for row in self.rows().values():
            if row.get('run_id') in ('saved','earlier-name'):
                self.assertEqual(row['initial_memory_mb'],2048)
                self.assertEqual(row['requested']['memory_mb'],2048)
                self.assertEqual(row['memory_ceiling_mb'],2048)
                self.assertNotIn('peak_grant_mb', row)


if __name__=='__main__':unittest.main()
