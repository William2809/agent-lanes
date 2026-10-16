"""Small admission/supervision fixtures; run on the test device, not as a load test."""
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.dont_write_bytecode = True
KIT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('resources', KIT / 'remote/resources.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
METRICS = dict(cores=10, load=1, available_mb=12000, pressure=1)
LIMITS = dict(MAX_JOBS=1, MAX_WORKERS=2, CPU_RESERVE=2, MEM_RESERVE_MB=2048, WORKER_MB=2048)


class Resources(unittest.TestCase):
    def test_v2_status_reports_active_cooldown_and_expiry(self):
        limits = dict(LIMITS, MAX_JOB_MB=8192, CLEANUP_QUARANTINE_S=600)
        metrics = dict(METRICS, load=0, available_mb=32000)
        modules = {
            'scheduler': types.SimpleNamespace(observe=lambda data, procs, now: None),
            'scheduler_process': types.SimpleNamespace(reconcile=lambda queue: None,
                                                       snapshot=lambda: {}),
            'scheduler_maintenance': types.SimpleNamespace(start_recovery=lambda queue: None),
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp):
            queue = r.Queue()
            with queue.transaction() as data:
                data['frozen_until'] = 110
                data['throttle_reason'] = 'pressure'

            def status(now):
                stdout = io.StringIO()
                with patch.dict(sys.modules, modules), \
                        patch.object(r, 'scheduler_version', return_value='v2'), \
                        patch.object(r, 'policy', return_value=limits), \
                        patch.object(r, 'processes', return_value={}), \
                        patch.object(r, 'sample', return_value=metrics), \
                        patch.object(r.time, 'time', return_value=now), \
                        patch.object(sys, 'argv', ['resources.py', 'status', '--json']), \
                        redirect_stdout(stdout):
                    self.assertEqual(r.main(), 0)
                return json.loads(stdout.getvalue())

            cooling = status(104.25)
            self.assertFalse(cooling['can_admit'])
            self.assertTrue(cooling['frozen'])
            self.assertEqual(cooling['cooldown_seconds'], 6)
            self.assertEqual(cooling['throttle_reason'], 'pressure')
            for field in ('active', 'waiting', 'policy', 'load', 'cores', 'pressure'):
                self.assertIn(field, cooling)

            recovered = status(111)
            self.assertTrue(recovered['can_admit'])
            self.assertFalse(recovered['frozen'])
            self.assertEqual(recovered['cooldown_seconds'], 0)

    def test_headroom_and_grants(self):
        self.assertEqual(r.capacity([], METRICS, LIMITS, 50), 2)
        self.assertEqual(r.capacity([], dict(METRICS, available_mb=4100), LIMITS, 50), 1)
        for metrics in (dict(METRICS, load=8), dict(METRICS, pressure=2),
                        dict(METRICS, pressure=4), dict(METRICS, available_mb=3000)):
            self.assertEqual(r.capacity([], metrics, LIMITS, 50), 0)
        active = [dict(workers=1, memory_mb=2048)]
        self.assertEqual(r.capacity(active, METRICS, LIMITS, 2), 0)
        self.assertEqual(r.capacity(active, dict(METRICS, available_mb=6144),
                                    dict(LIMITS, MAX_JOBS=2), 2), 1)

    def test_running_job_holds_back_only_its_unused_reservation(self):
        # Live 2026-10-15: one 4096 MB job running, 7600 MB available, reserve 2048.
        limits = dict(LIMITS, MAX_JOBS=2, MAX_WORKERS=4)
        metrics = dict(METRICS, available_mb=7600)
        row = dict(workers=2, memory_mb=4096, group=500)
        busy = {500: dict(group=500, parent=1, birth='100', rss=3500 * 1024),
                501: dict(group=500, parent=500, birth='101', rss=400 * 1024),
                9: dict(group=9, rss=9000 * 1024)}
        self.assertEqual(r.capacity([row], metrics, limits, 2, busy), 2)
        # Not started yet (no group, or no live member): the whole grant stays reserved.
        idle = {9: dict(group=9, rss=9000 * 1024)}
        self.assertEqual(r.capacity([dict(workers=2, memory_mb=4096)], metrics, limits, 2, busy), 0)
        self.assertEqual(r.capacity([row], metrics, limits, 2, idle), 0)
        self.assertEqual(r.capacity([row], metrics, limits, 2), 0)
        for pressure in (2, 4):
            self.assertEqual(r.capacity([row], dict(metrics, pressure=pressure), limits, 2, busy), 0)

    def test_max_workers_bounds_all_running_grants(self):
        limits = dict(LIMITS, MAX_JOBS=3, MAX_WORKERS=3)
        roomy = dict(METRICS, cores=16, load=0, available_mb=64000)
        self.assertEqual(r.capacity([], roomy, limits, 50), 3)
        self.assertEqual(r.capacity([dict(workers=2, memory_mb=0)], roomy, limits, 2), 1)
        self.assertEqual(r.capacity([dict(workers=2, memory_mb=0), dict(workers=1, memory_mb=0)],
                                    roomy, limits, 1), 0)

    def test_local_mode_and_bad_host_policy(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp):
            with patch.dict(os.environ, AGENT_CI_MAX_WORKERS='10'):
                self.assertEqual(r.policy('local')['MAX_WORKERS'], 1)
            with patch.dict(os.environ, AGENT_CI_MAX_JOBS='0'):
                with self.assertRaises(ValueError):
                    r.policy('runner')

    def test_reused_pid_and_surviving_child(self):
        row = dict(owner=10, start='old', group=20)
        self.assertFalse(r.live(row, {10: dict(start='new', group=10)}))
        self.assertTrue(r.live(row, {21: dict(start='child', group=20)}))

    def test_wait_expiry_cleans_request_without_starting(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = r.Queue(temp)
            clock = [1]
            with patch.object(r, 'sample', return_value=dict(METRICS, pressure=4)), \
                    patch.object(r.time, 'sleep', side_effect=lambda _: clock.__setitem__(0, 3)), \
                    patch.object(r.time, 'time', side_effect=lambda: clock[0]):
                self.assertIsNone(r.acquire(queue, 'blocked', os.getpid(), 2, LIMITS, 2))
            with queue.transaction() as data:
                self.assertEqual(data['jobs'], {})

    def test_fifo_does_not_allow_smaller_new_job_to_jump_head(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = r.Queue(temp)
            procs = r.processes()
            with queue.transaction() as data:
                data['jobs']['first'] = dict(owner=os.getpid(), start=procs[os.getpid()]['start'], sequence=1)
                data['sequence'] = 1
            clock = [1]
            with patch.object(r, 'sample', return_value=METRICS), \
                    patch.object(r.time, 'sleep', side_effect=lambda _: clock.__setitem__(0, 3)), \
                    patch.object(r.time, 'time', side_effect=lambda: clock[0]):
                self.assertIsNone(r.acquire(queue, 'second', os.getpid(), 2, LIMITS, 1))
            with patch.object(r, 'sample', return_value=METRICS):
                self.assertEqual(r.acquire(queue, 'first', os.getpid(), time.time() + 60, LIMITS, 2), (2, 4096))

    def test_twelve_submitters_share_one_host_budget(self):
        # Real independent processes/locks, tiny sleeping commands. Mock only
        # host measurements, so this fixture never requires an overloaded host.
        with tempfile.TemporaryDirectory() as temp:
            script = '''import importlib.util, os, pathlib, sys, time
s=importlib.util.spec_from_file_location('r',sys.argv[1]); r=importlib.util.module_from_spec(s); s.loader.exec_module(r)
r.sample=lambda: dict(cores=10,load=0,available_mb=12000,pressure=1)
q=r.Queue(sys.argv[2]); token=sys.argv[3]
a=r.acquire(q,token,os.getpid(),time.time()+90,dict(MAX_JOBS=1,MAX_WORKERS=2,CPU_RESERVE=2,MEM_RESERVE_MB=2048,WORKER_MB=2048),2)
assert a==(2,4096)
with q.transaction() as d: seq=d['jobs'][token]['sequence']
with open(pathlib.Path(sys.argv[2])/'events','a') as f: f.write('start %s %s\\n'%(token,seq))
time.sleep(0.05)
with open(pathlib.Path(sys.argv[2])/'events','a') as f: f.write('end %s %s\\n'%(token,seq))
q.release(token)
'''
            children = [subprocess.Popen([sys.executable, '-B', '-c', script,
                         str(KIT / 'remote/resources.py'), temp, str(i)]) for i in range(12)]
            try:
                for child in children:
                    self.assertEqual(child.wait(timeout=120), 0)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=60)
            lines = (Path(temp) / 'events').read_text().splitlines()
            self.assertEqual(len(lines), 24)
            sequences = []
            for start, end in zip(lines[::2], lines[1::2]):
                self.assertEqual(start.split()[1:], end.split()[1:])
                self.assertTrue(start.startswith('start '))
                self.assertTrue(end.startswith('end '))
                sequences.append(int(start.split()[2]))
            self.assertEqual(sequences, sorted(sequences))

    def test_memory_overrun_kills_only_owned_group(self):
        with tempfile.TemporaryDirectory() as temp:
            child = subprocess.Popen(['sleep', '60'], start_new_session=True)
            unrelated = subprocess.Popen(['sleep', '60'], start_new_session=True)
            try:
                original = r.processes
                def high_rss():
                    rows = original()
                    for row in rows.values():
                        if row['group'] == child.pid:
                            row['rss'] = 9000
                    return rows
                with patch.object(r, 'processes', side_effect=high_rss):
                    self.assertEqual(r.watch(child.pid, 30, 1, str(Path(temp) / 'stop')), 75)
                child.wait(timeout=60)
                self.assertIsNone(unrelated.poll())
                self.assertEqual((Path(temp) / 'stop').read_text(), '75')
            finally:
                for proc in (child, unrelated):
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=60)

    def test_memory_kill_waits_for_kill_percent_of_grant(self):
        # 9500 KB counts as 9 MB: over an 8 MB grant, under the default 150% kill line (12 MB).
        child = subprocess.Popen(['sleep', '60'], start_new_session=True)
        try:
            original = r.processes
            def high_rss():
                rows = original()
                for row in rows.values():
                    if row['group'] == child.pid:
                        row['rss'] = 9500
                return rows
            with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp), \
                    patch.object(r, 'processes', side_effect=high_rss):
                self.assertEqual(r.policy('runner')['KILL_PCT'], 150)
                self.assertEqual(r.watch(child.pid, 15, 8, str(Path(temp) / 'grace')), 74)
                self.assertEqual((Path(temp) / 'grace').read_text(), '74')
            child.wait(timeout=60)
            child = subprocess.Popen(['sleep', '60'], start_new_session=True)
            with tempfile.TemporaryDirectory() as temp, \
                    patch.dict(os.environ, HOME=temp, AGENT_CI_KILL_PCT='100'), \
                    patch.object(r, 'processes', side_effect=high_rss):
                self.assertEqual(r.watch(child.pid, 30, 8, str(Path(temp) / 'stop')), 75)
            child.wait(timeout=60)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=60)

    def test_timeout_and_descendants(self):
        with tempfile.TemporaryDirectory() as temp:
            child = subprocess.Popen(['/bin/sh', '-c', 'sleep 60 & wait'], start_new_session=True)
            try:
                with patch.object(r, 'sample', return_value=METRICS):
                    self.assertEqual(r.watch(child.pid, 0, 500, str(Path(temp) / 'stop')), 74)
                child.wait(timeout=60)
                self.assertFalse(any(p['group'] == child.pid for p in r.processes().values()))
            finally:
                r.terminate(child.pid)
                child.wait(timeout=60)

    def test_vitest_lower_limit_is_preserved(self):
        with patch.dict(os.environ, VITEST_MAX_WORKERS='1'):
            self.assertEqual(r.worker_env(2)['VITEST_MAX_WORKERS'], '1')
        with patch.dict(os.environ, VITEST_MAX_WORKERS='100'):
            self.assertEqual(r.worker_env(2)['VITEST_MAX_WORKERS'], '2')

    def safe_check_fixture(self, root):
        tools = root / 'tools'
        (tools / 'bin').mkdir(parents=True)
        target = tools / 'bin/safe-check'
        target.write_bytes((KIT.parents[1] / 'bin/safe-check').read_bytes())
        (tools / 'skills/remote-ci/remote').mkdir(parents=True)
        for name in ('ci_telemetry.py', 'process_tree.py'):
            (tools / 'skills/remote-ci/remote' / name).write_bytes((KIT / 'remote' / name).read_bytes())
        helper = tools / 'skills/remote-ci/remote/resources.py'
        helper.write_text((KIT / 'remote/resources.py').read_text().replace(
            "if __name__ == '__main__':", "sample=lambda: dict(cores=10,load=0,available_mb=12000,pressure=1)\nif __name__ == '__main__':"))
        repo = root / 'repo'; repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        return tools, repo, target

    def test_single_device_safe_check_runs_locally_with_one_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tools, repo, target = self.safe_check_fixture(root)
            call = subprocess.run(['/bin/bash', str(target), '--run',
                                   'printf "%s" "$AGENT_CI_WORKERS" > grant'], cwd=repo,
                                  env=dict(os.environ, HOME=temp), capture_output=True, text=True, timeout=60)
            self.assertEqual(call.returncode, 0, call.stdout + call.stderr)
            self.assertEqual((repo / 'grant').read_text(), '1')

    def test_safe_check_remote_failure_never_runs_local_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tools, repo, target = self.safe_check_fixture(root)
            (repo / '.remote-ci.conf').write_text('REMOTE_CI_NAME=fixture\n')
            client = tools / 'skills/remote-ci/bin/remote-ci'
            client.parent.mkdir(); client.write_text('#!/bin/sh\nexit 3\n'); client.chmod(0o755)
            call = subprocess.run(['/bin/bash', str(target), '--run', 'touch escaped'], cwd=repo,
                                  env=dict(os.environ, HOME=temp), capture_output=True, text=True, timeout=60)
            self.assertEqual(call.returncode, 3)
            self.assertFalse((repo / 'escaped').exists())

    def test_outage_hook_does_not_execute_local_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(['git', 'init', '-q', temp], check=True)
            client = 'remote-ci' if KIT.name == 'remote-ci' else 'remote-ci'
            prefix = 'MINI_CI' if client == 'remote-ci' else 'REMOTE_CI'
            (root / ('.' + client + '.conf')).write_text(prefix + '_CHECK="touch escaped"\n')
            stub = root / client
            stub.write_text('#!/bin/sh\nexit 3\n'); stub.chmod(0o755)
            result = subprocess.run(['/bin/sh', str(KIT / 'templates/pre-push')], cwd=temp,
                                    input='refs/heads/main abc refs/heads/main def\n', text=True,
                                    capture_output=True, env=dict(os.environ, PATH=temp+':'+os.environ['PATH']), timeout=60)
            self.assertEqual(result.returncode, 3)
            self.assertFalse((root / 'escaped').exists())


if __name__ == '__main__':
    unittest.main()
