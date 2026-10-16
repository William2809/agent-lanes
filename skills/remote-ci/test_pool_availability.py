"""cipool2rv requirements: immediate admission, parallel probes and host holds."""
import contextlib
import io
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from test_pool import KIT, module


class Availability(unittest.TestCase):
    def setUp(self):
        self.pool = module('pool', KIT / 'bin/pool.py')
        self.runners = self.pool.registry('hosta=hosta-route:unit pc=pc-route:unit:wol', {
            'CI_RUNNER_pc_EXPECTED_SECONDS': '180',
            'CI_RUNNER_pc_WOL_COMMAND': 'wake-fixture',
            'CI_RUNNER_pc_WAKE_TIMEOUT': '60',
        })

    @contextlib.contextmanager
    def controlled_clock(self, ticks):
        clock = SimpleNamespace(monotonic=lambda: ticks[0])
        class Replies(queue.Queue):
            def get(self, block=True, timeout=None):
                # Product time advances explicitly. Give host thread scheduling
                # its own generous guard instead of imposing a real one-second race.
                return super().get(block=block, timeout=60 if timeout is not None else None)
        with mock.patch.object(self.pool, 'time', clock), \
                mock.patch.object(self.pool, 'queue',
                                  SimpleNamespace(Queue=Replies, Empty=queue.Empty), create=True):
            yield clock

    def test_ready_mini_beats_faster_sleeper(self):
        """Fix 1: ready warm hosta (270) beats PC (180 + 60), without wake."""
        chosen = self.pool.select(self.runners, {'unit'},
            lambda r: dict(can_admit=True, warm=True) if r['name'] == 'hosta' else None,
            wake=lambda r: self.fail('ready hosta must not wait for wake'))
        self.assertEqual(chosen['name'], 'hosta')

    def test_failed_wake_uses_awake_fallback(self):
        """Fix 1: both a wake error and its deadline fall back to the queued hosta."""
        for fail in (False, True):
            with self.subTest(fail=fail):
                ticks, wakes = [0.0], []
                def sleep(seconds):
                    ticks[0] += seconds
                def wake(runner):
                    wakes.append(runner['name'])
                    if fail:
                        raise self.pool.Infra('runner asleep')
                chosen = self.pool.select(self.runners, {'unit'},
                    lambda r: dict(can_admit=False, waiting=7) if r['name'] == 'hosta' else None,
                    wake=wake, clock=lambda: ticks[0], sleep=sleep)
                self.assertEqual(chosen['name'], 'hosta')
                self.assertEqual(wakes, ['pc'])
                self.assertLessEqual(ticks[0], 60)

    def test_ready_host_does_not_wait_for_slow_probe(self):
        """Fix 1: ready selection precedes a blocked reply, regardless of host load."""
        release = threading.Event()
        blocked = threading.Event()
        finished = threading.Event()
        ticks = [0.0]
        def probe(runner):
            self.assertIn('deadline', runner)
            if runner['name'] == 'pc':
                blocked.set()
                try:
                    self.assertTrue(release.wait(timeout=60), 'fixture release timed out')
                    ticks[0] = 1.0
                finally:
                    finished.set()
                return None
            self.assertTrue(blocked.wait(timeout=60), 'other probe did not start')
            return dict(can_admit=True)
        try:
            with self.controlled_clock(ticks) as clock:
                started = clock.monotonic()
                chosen = self.pool.select(list(reversed(self.runners)), {'unit'}, probe)
                elapsed = clock.monotonic() - started
        finally:
            release.set()
            if blocked.is_set():
                self.assertTrue(finished.wait(timeout=60), 'blocked fixture did not finish')
        self.assertEqual(chosen['name'], 'hosta')
        self.assertLess(elapsed, .5)

    def test_three_unreachable_probes_share_deadline(self):
        """Fix 1: all probes start before one shared deadline expires."""
        runners = self.pool.registry('a=a-route:unit b=b-route:unit c=c-route:unit', {})
        started_probes = threading.Barrier(3, timeout=60)
        ticks, deadlines = [0.0], []
        finished = queue.Queue()
        def unreachable(runner):
            self.assertIn('deadline', runner)
            deadlines.append(runner['deadline'])
            # All three must be in flight before the simulated deadline. The
            # fixture has 60 real seconds; host scheduling is not product time.
            started_probes.wait()
            ticks[0] = 1.0
            try:
                return self.pool.probe(runner, 'fixture')
            finally:
                finished.put(runner['name'])
        with tempfile.TemporaryDirectory() as tmp:
            ssh = Path(tmp) / 'ssh'
            ssh.write_text('#!/bin/sh\nexec sleep 5\n')
            ssh.chmod(0o755)
            with mock.patch.dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH']), \
                    self.controlled_clock(ticks) as clock:
                started = clock.monotonic()
                with self.assertRaisesRegex(self.pool.Infra, 'runner unreachable'):
                    self.pool.select(runners, {'unit'}, unreachable)
                elapsed = clock.monotonic() - started
                self.assertLess(elapsed, 3)
                self.assertEqual(deadlines, [1.0, 1.0, 1.0])
                self.assertEqual({finished.get(timeout=60) for _ in runners}, {'a', 'b', 'c'})

    def status(self, rows, available=12000):
        resources = module('resources', KIT / 'remote/resources.py')
        metrics = dict(cores=10, load=0, available_mb=available, pressure=1)
        limits = dict(MAX_JOBS=4, MAX_WORKERS=1, CPU_RESERVE=2,
                      MEM_RESERVE_MB=1024, WORKER_MB=1024)
        with tempfile.TemporaryDirectory() as tmp:
            queue = resources.Queue(tmp)
            with queue.transaction() as data:
                data['jobs'] = {str(i): dict(row, owner=1, start='fixture')
                                for i, row in enumerate(rows)}
            out = io.StringIO()
            with mock.patch.object(resources, 'Queue', return_value=queue), \
                    mock.patch.object(resources, 'sample', return_value=metrics), \
                    mock.patch.object(resources, 'policy', return_value=limits), \
                    mock.patch.object(resources, 'processes', return_value={1: dict(start='fixture')}), \
                    mock.patch('sys.argv', ['resources.py', 'status', '--json']), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(resources.main(), 0)
            return json.loads(out.getvalue())

    def test_worker_capacity_routes_to_idle_cold_host(self):
        """Fix 2: MAX_JOBS=4 cannot hide the exhausted single-worker budget."""
        full = self.status([dict(workers=1, memory_mb=1024)])
        idle = self.status([])
        full['warm'] = True
        runners = self.pool.registry('a=a-route:unit b=b-route:unit', {})
        chosen = self.pool.select(runners, {'unit'}, lambda r: full if r['name'] == 'a' else idle)
        self.assertEqual(chosen['name'], 'b')
        self.assertIs(full['can_admit'], False)
        self.assertEqual(full['free_workers'], 0)
        self.assertEqual(full['free_memory_mb'], 9952)
        self.assertIs(idle['can_admit'], True)
        self.assertEqual(idle['free_workers'], 1)

    def test_memory_and_fifo_status_deny_immediate_admission(self):
        """Fix 2: memory below one worker and an earlier waiter both deny admission."""
        for rows, available, memory, workers in [([], 2047, 1023, 0),
                                                ([dict(sequence=1)], 12000, 10976, 1)]:
            with self.subTest(available=available):
                status = self.status(rows, available)
                self.assertIs(status.get('can_admit'), False)
                self.assertEqual(status['free_memory_mb'], memory)
                self.assertEqual(status['free_workers'], workers)

    def test_frozen_or_draining_host_yields_to_other_host(self):
        """Fix 3: a held warm host loses; when all are held, keep the wait path."""
        runners = self.pool.registry('a=a-route:unit b=b-route:unit', {})
        for hold in ('frozen', 'draining'):
            with self.subTest(hold=hold):
                statuses = dict(a=dict(warm=True, can_admit=True, **{hold: True}),
                                b=dict(can_admit=True))
                self.assertEqual(self.pool.select(runners, {'unit'},
                    lambda r: statuses[r['name']])['name'], 'b')
                statuses['b'][hold] = True
                self.assertEqual(self.pool.select(runners, {'unit'},
                    lambda r: statuses[r['name']])['name'], 'a')

    def test_probe_reads_freeze_and_live_drain(self):
        """Fix 3: read real marker files; a dead drain owner is not a hold."""
        for marker, value, field, expected in [('freeze', 'maintenance', 'frozen', True),
                ('drain', str(os.getpid()), 'draining', True),
                ('drain', '99999999', 'draining', False)]:
            with self.subTest(marker=marker, value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'ci/bin').mkdir(parents=True)
                (root / 'ci/projects/fixture/repo.git').mkdir(parents=True)
                (root / 'ci/bin/resources.py').write_text('print(\'{"active":0}\')\n')
                (root / ('ci/' + marker)).write_text(value + '\n')
                ssh = root / 'ssh'
                ssh.write_text('#!/bin/sh\nfor arg; do command=$arg; done\nexec /bin/bash -c "$command"\n')
                ssh.chmod(0o755)
                with mock.patch.dict(os.environ, HOME=tmp, PATH=tmp + os.pathsep + os.environ['PATH']):
                    status = self.pool.probe(self.runners[0], 'fixture')
                self.assertIsNotNone(status)
                self.assertEqual(bool(status.get(field)), expected)

    def test_runner_without_project_is_not_eligible(self):
        """cipool2 gap 2: no repo.git means absent (even when frozen); setup still reaches it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'ci/bin').mkdir(parents=True)
            (root / 'ci/bin/resources.py').write_text('print(\'{"active":0}\')\n')
            (root / 'ci/freeze').write_text('maintenance\n')
            ssh = root / 'ssh'
            ssh.write_text('#!/bin/sh\nfor arg; do command=$arg; done\nexec /bin/bash -c "$command"\n')
            ssh.chmod(0o755)
            with mock.patch.dict(os.environ, HOME=tmp, PATH=tmp + os.pathsep + os.environ['PATH']):
                self.assertIs(self.pool.probe(self.runners[0], 'fixture').get('absent'), True)
                self.assertEqual(self.pool.probe(self.runners[0], 'fixture', setup=True), {})
        runners = self.pool.registry('a=a-route:unit b=b-route:unit:wol',
                                     {'CI_RUNNER_b_WOL_COMMAND': 'wake-fixture'})
        busy = dict(active=4, waiting=2, policy={'MAX_JOBS': 1}, can_admit=False)
        for b, expected in [(busy, 'b'), (None, 'b')]:
            with self.subTest(b=b):
                woke = []
                chosen = self.pool.select(runners, {'unit'},
                    lambda r: {'absent': True} if r['name'] == 'a' else (busy if woke else b),
                    wake=lambda r: woke.append(r['name']), sleep=lambda s: None)
                self.assertEqual(chosen['name'], expected)
        for asleep in (False, True):
            woke = []
            with self.subTest(asleep=asleep), self.assertRaisesRegex(self.pool.Infra, 'not set up'):
                self.pool.select(runners, {'unit'},
                    lambda r: None if r['name'] == 'b' and asleep and not woke else {'absent': True},
                    wake=lambda r: woke.append(r['name']), sleep=lambda s: None)
            self.assertEqual(woke, ['b'] if asleep else [])
