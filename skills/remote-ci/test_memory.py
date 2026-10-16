"""Per-project memory brief: bounded fixtures, no memory load on the host."""
import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_runner as fixtures
KIT = Path(os.environ.get('CIQUEUE_MEMORY_KIT', Path(__file__).resolve().parent))
fixtures.KIT = KIT
sys.path.insert(0, str(KIT / 'remote'))
import resources as r
import scheduler_process as supervisor
import process_tree

LIMITS = dict(MAX_JOBS=4, MAX_WORKERS=8, CPU_RESERVE=2,
              MEM_RESERVE_MB=1024, WORKER_MB=2048, MAX_JOB_MB=8192)
METRICS = dict(cores=10, load=0, available_mb=16000, pressure=1)


class Memory(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for key in ('AGENT_CI_KILL_FACTOR', 'AGENT_CI_KILL_PCT'):
            os.environ.pop(key, None)

    def test_project_floor_grants_4096_and_waits_with_3072_headroom(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp), \
                patch.object(r, 'sample', return_value=METRICS):
            queue = r.Queue(temp)
            self.assertEqual(r.acquire(queue, 'job', os.getpid(), time.time()+10,
                                       LIMITS, 1, memory_mb=4096), (1, 4096))
        self.assertEqual(r.capacity([], dict(METRICS, available_mb=4096), LIMITS,
                                    1, memory_mb=4096), 0)
        self.assertEqual(r.capacity([], dict(METRICS, available_mb=5120), LIMITS,
                                    1, memory_mb=4096), 1)

    def test_project_floor_is_capped_without_raising_worker_count(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp), \
                patch.object(r, 'sample', return_value=METRICS):
            self.assertEqual(r.acquire(r.Queue(temp), 'job', os.getpid(), time.time()+10,
                                       LIMITS, 1, memory_mb=999999), (1, 8192))

    def watch_sample(self, used, pressure=1, memory=100):
        samples = iter([{42: dict(group=42, parent=1, rss=used*1024, start='now', birth='100')}, {}])
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp), \
                patch.object(process_tree, 'birth', return_value='100'), \
                patch.object(r, 'processes', side_effect=lambda: next(samples, {})), \
                patch.object(r, 'sample', return_value=dict(METRICS, pressure=pressure)), \
                patch.object(r.time, 'sleep'), patch.object(r, 'terminate') as stop, \
                contextlib.redirect_stdout(io.StringIO()) as log:
            result = r.watch(42, 60, memory, str(Path(temp)/'stop'))
            return result, stop.call_count, log.getvalue()

    def test_120_percent_survives_and_logs_peak(self):
        code, stopped, log = self.watch_sample(120)
        self.assertEqual((code, stopped), (0, 0))
        self.assertIn('== memory peak 120 MB of 100 granted', log)

    def test_200_percent_stops_and_logs_peak(self):
        code, stopped, log = self.watch_sample(200)
        self.assertEqual((code, stopped), (75, 1))
        self.assertIn('== memory peak 200 MB of 100 granted', log)

    def test_critical_pressure_stops_below_budget(self):
        self.assertEqual(self.watch_sample(20, pressure=4)[:2], (75, 1))

    def test_stop_during_policy_read_keeps_success_and_peak(self):
        # Reproduced by the fast V1 project on snapshot ad596228: group TERM
        # killed the config reader and turned an already successful check into 74.
        def interrupted_policy(_mode):
            handler = signal.getsignal(signal.SIGTERM)
            if callable(handler):
                handler(signal.SIGTERM, None)
            raise subprocess.CalledProcessError(15, ['fixture-config-reader'])
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(r, 'policy', side_effect=interrupted_policy), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertEqual(r.watch(42, 60, 100, str(Path(temp)/'stop')), 0)
            self.assertFalse((Path(temp)/'stop').exists())
            self.assertIn('== memory peak 0 MB of 100 granted', log.getvalue())

    def test_host_percent_and_invalid_project_setting(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp,
                AGENT_CI_KILL_PCT='200', AGENT_CI_MAX_JOB_MB='6144'):
            limits = r.policy('runner')
            self.assertEqual(limits['KILL_PCT'], 200)
            self.assertEqual(r.project_memory(10000, limits), 6144)
            self.assertEqual(r.project_memory(0, limits), 0)
            self.assertFalse(r.budget_exceeded(200, 100, limits['KILL_PCT']))
            self.assertTrue(r.budget_exceeded(201, 100, limits['KILL_PCT']))
            with self.assertRaises(ValueError):
                r.project_memory(-1, limits)

    def test_stop_during_sampling_keeps_success_but_active_errors_fail(self):
        def interrupted_sample():
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            raise subprocess.CalledProcessError(15, ['fixture-process-sampler'])
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp), \
                patch.object(r, 'processes', side_effect=interrupted_sample), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertEqual(r.watch(42, 60, 100, str(Path(temp)/'stop')), 0)
            self.assertIn('== memory peak 0 MB of 100 granted', log.getvalue())
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp), \
                patch.object(r, 'processes', side_effect=ValueError('fixture sample unavailable')), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                r.watch(42, 60, 100, str(Path(temp)/'stop'))

    def test_kill_percent_is_only_host_threshold(self):
        # Brief: one host setting. A leftover factor must not override 300%.
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp,
                AGENT_CI_KILL_PCT='300', AGENT_CI_KILL_FACTOR='1.5',
                AGENT_CI_WORKER_MB='2048'):
            self.assertEqual(r.policy('runner')['KILL_PCT'], 300)
            self.assertEqual(r.policy('runner')['WORKER_MB'], 2048)
            self.assertEqual(self.watch_sample(200)[:2], (0, 0))
            self.assertEqual(self.watch_sample(300)[:2], (0, 0))
            self.assertEqual(self.watch_sample(301)[:2], (75, 1))
            self.assertNotIn('KILL_FACTOR', r.policy('runner'))
            with patch.dict(os.environ, AGENT_CI_KILL_PCT='75'):
                self.assertEqual(r.policy('runner')['KILL_PCT'], 75)
            with patch.dict(os.environ, AGENT_CI_KILL_PCT='1.5'):
                with self.assertRaises(ValueError):
                    r.policy('runner')

    def test_policy_reader_fallback_still_stops_an_overrun(self):
        samples = iter([{42: dict(group=42, parent=1, rss=200*1024, start='now', birth='100')}])
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(r, 'policy', side_effect=OSError('fixture reader unavailable')), \
                patch.object(process_tree, 'birth', return_value='100'), \
                patch.object(r, 'processes', side_effect=lambda: next(samples, {})), \
                patch.object(r, 'terminate'), patch.object(r.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(r.watch(42, 60, 100, str(Path(temp)/'stop')), 75)
            self.assertEqual((Path(temp)/'stop').read_text(), '75')

    def supervisor_sample(self, used, memory=100):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(sig, handler) for sig, handler in previous.items()])
        original = supervisor.snapshot
        def sample():
            procs = original()
            with queue.transaction() as data:
                group = data['jobs']['job'].get('process', {}).get('group')
            for pid, row in procs.items():
                row['rss'] = used*1024 if pid == group else 0
            return procs
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp),                 patch.object(supervisor, 'snapshot', side_effect=sample),                 patch.object(r, 'sample', return_value=METRICS):
            queue = r.Queue(temp)
            with queue.transaction() as data:
                data['jobs']['job'] = dict(version=2, state='running', owner=os.getpid(),
                    owner_birth=supervisor.birth(os.getpid()), lease_generation=1,
                    workers=1, memory_mb=memory)
            result = supervisor.run(queue, 'job', 'check', 10, str(Path(temp)/'stop'),
                                    [sys.executable, '-c', 'import time; time.sleep(2.3)'])
            with queue.transaction() as data:
                peak = data['jobs']['job'].get('peak_rss_mb')
                self.assertFalse(data['jobs']['job']['groups'])
            return result, peak

    def test_v2_120_percent_survives(self):
        result, peak = self.supervisor_sample(120)
        self.assertEqual(result, 0)
        self.assertEqual(peak, 120)

    def test_v2_200_percent_stops(self):
        result, peak = self.supervisor_sample(200)
        self.assertEqual(result, 75)
        self.assertEqual(peak, 200)

    def test_stop_line_caps_both_supervisors_at_host_maximum(self):
        # Review 3: an 8192 MB grant at 300% never permits 24 GB.
        with patch.dict(os.environ, AGENT_CI_KILL_PCT='300', AGENT_CI_MAX_JOB_MB='8192'):
            self.assertEqual(self.watch_sample(8192, memory=8192)[:2], (0, 0))
            self.assertEqual(self.watch_sample(8193, memory=8192)[:2], (75, 1))
            result, peak = self.supervisor_sample(8193, memory=8192)
            self.assertEqual((result, peak), (75, 8193))

    def test_tree_credit_includes_another_session(self):
        procs = {10: dict(group=10, parent=1, rss=100*1024, start='root', birth='100'),
                 20: dict(group=20, parent=10, rss=900*1024, start='child', birth='101'),
                 30: dict(group=30, parent=1, rss=7000*1024, start='unrelated')}
        self.assertEqual(r.unused_reservation(dict(group=10, memory_mb=2048), procs), 1048)

    def test_watch_counts_child_in_another_session(self):
        samples = iter([{10: dict(group=10, parent=1, rss=10*1024, start='root', birth='100'),
                         20: dict(group=20, parent=10, rss=190*1024, start='child', birth='101')}])
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp), \
                patch.object(process_tree, 'birth', return_value='100'), \
                patch.object(r, 'processes', side_effect=lambda: next(samples, {})), \
                patch.object(r, 'sample', return_value=METRICS), \
                patch.object(r.time, 'sleep'), patch.object(r, 'terminate'):
            self.assertEqual(r.watch(10, 60, 100, str(Path(temp)/'stop')), 75)

    def test_v2_retains_reparented_child_but_not_reused_pid(self):
        procs = {10: dict(group=10, parent=1, rss=0, start='root', birth='100'),
                 20: dict(group=20, parent=10, rss=0, start='child', birth='101')}
        record = dict(group=10, generation=1, members={'10': '100'})
        supervisor.refresh(record, procs)
        procs.pop(10)
        procs[20]['parent'] = 1
        self.assertEqual(set(supervisor.members(record, procs)), {20})
        procs[20]['birth'] = '102'
        self.assertFalse(supervisor.members(record, procs))

    def test_tree_counts_real_setsid_child_in_both_versions(self):
        root = subprocess.Popen([sys.executable, '-u', '-c',
            'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",'
            '"import time; blob=bytearray(12*1024*1024); time.sleep(60)"], start_new_session=True); '
            'print(p.pid,flush=True); time.sleep(60)'], stdout=subprocess.PIPE, text=True,
            start_new_session=True)
        child = int(root.stdout.readline())
        try:
            deadline = time.monotonic()+60
            while time.monotonic() < deadline:
                procs = supervisor.snapshot()
                if procs.get(child, {}).get('rss', 0) >= 8192:
                    break
                time.sleep(0.05)
            self.assertNotEqual(procs[child]['group'], root.pid)
            group_rss = sum(p['rss'] for p in procs.values() if p['group'] == root.pid)//1024
            used = 16384-r.unused_reservation(dict(group=root.pid, memory_mb=16384), procs)
            self.assertGreaterEqual(used-group_rss, 8)
            record = dict(group=root.pid, generation=1,
                          members={str(root.pid): procs[root.pid]['birth']})
            self.assertIn(child, supervisor.members(record, procs))
            self.assertTrue(supervisor.reap(record))
            self.assertNotIn(child, supervisor.snapshot())
        finally:
            for pid in (child, root.pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            root.wait(timeout=60)
            root.stdout.close()


class MemoryRunner(unittest.TestCase):
    git = fixtures.Runner.git
    configure = fixtures.Runner.configure
    stop_runs = fixtures.Runner.stop_runs
    start = fixtures.Runner.start
    wait_for = fixtures.Runner.wait_for
    log = fixtures.Runner.log
    count = fixtures.Runner.count
    assert_result = fixtures.Runner.assert_result
    setUp = fixtures.Runner.setUp

    def project_run(self, version, need, expected):
        with (self.dir/'conf').open('a') as stream:
            stream.write('REMOTE_CI_WORKERS=1\nREMOTE_CI_MEMORY_MB=%s\n' % need)
        (self.home/'.config/agent-lanes/config').write_text(
            'AGENT_CI_SCHEDULER=%s\nAGENT_CI_MAX_WORKERS=4\nAGENT_CI_WORKER_MB=2048\n' % version)
        sampler = self.bin/'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "scheduler_sample = lambda: dict(cores=10, load=0, available_mb=20000, total_mb=24000, pressure=1, swap_mb=0)\nif __name__ == '__main__':"))
        run = self.start('memory')
        self.wait_for(lambda: self.count() == 1)
        (self.home/'release').touch()
        self.assert_result(run, 'memory', 0)
        self.assertIn('resources workers=1 memory_mb=%s' % expected, self.log('memory'))
        self.assertRegex(self.log('memory'), r'== memory peak \d+ MB of %s granted' % expected)

    def test_v1_project_config_reaches_grant(self):
        self.project_run('v1', 4096, 4096)

    def test_v2_project_config_reaches_grant(self):
        self.project_run('v2', 6144, 6144)

    def test_v2_project_config_respects_host_cap(self):
        self.project_run('v2', 999999, 8192)


if __name__ == '__main__':
    unittest.main()
