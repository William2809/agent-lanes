"""Receipt brief requirements; all fixtures run through remote-ci, never locally."""
import importlib.util
import fcntl
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
# Product limits from review 2, not fixture readiness waits. Keep them strict.
PUBLICATION_LIMIT_SECONDS = 5
HOOK_LIMIT_SECONDS = 2


def module():
    spec = importlib.util.spec_from_file_location('receipts', KIT / 'remote/receipts.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Receipts(unittest.TestCase):
    def setUp(self):
        self.r = module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'fixture/run.json'

    def start(self, path=None):
        path = path or self.path
        self.r.begin(path, 'fixture', path.stem, 'targeted', now=100)
        return path

    def read(self, path=None):
        return json.loads((path or self.path).read_text())

    def test_phase_grant_and_private_atomic_receipt(self):
        """Requirement: per-phase time/grant/RSS and mode 600; boundary: two phases."""
        with patch.object(self.r, 'host_sample', return_value=dict(
                pressure=1, swap_used_bytes=10, pageout_bytes=100, swapout_bytes=200)):
            self.start()
        self.r.transition(self.path, 'checkout', now=103, host=dict(
            pressure=2, swap_used_bytes=20, pageout_bytes=130, swapout_bytes=250))
        self.r.grant(self.path, 2, 4096)
        self.r.record_sample(self.path, 8192, {'21:birth': 500})
        self.r.record_sample(self.path, 4096, {'21:birth': 700, '22:birth': 100})
        self.r.transition(self.path, 'cleanup', now=108, host={})
        self.r.finish(self.path, 0, now=110, host={})
        row = self.read()
        self.assertEqual(row['phases']['queue']['duration_seconds'], 3)
        self.assertEqual(row['phases']['queue']['host_delta'], dict(
            pressure=1, swap_used_bytes=10, pageout_bytes=30, swapout_bytes=50))
        self.assertEqual(row['phases']['checkout']['duration_seconds'], 5)
        self.assertEqual(row['phases']['checkout']['peak_rss_bytes'], 8192)
        self.assertEqual(row['phases']['checkout']['os_write_bytes'], 800)
        self.assertEqual(row['granted'], {'workers': 2, 'memory_mb': 4096})
        self.assertEqual(row['duration_seconds'], 10)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.parent.glob('*.new')), [])

    def test_unknown_is_not_zero_and_classification(self):
        """Requirement: exit classes; boundary: killed/busy observations cannot teach budgets."""
        for code, expected in [(0, 'candidate'), (1, 'candidate'), (74, 'infra'),
                               (75, 'busy'), (76, 'infra')]:
            self.start()
            # Completeness is an observer fact. Give this exit-class fixture
            # explicit healthy samples only for its original candidate cases.
            if code in (0, 1):
                self.r.mutate(self.path, lambda row: row.update(sampler=dict(clean_exit=True,
                    gap_count=0, started_at=100, first_sample_at=100, last_sample_at=102, samples=2)))
            self.r.finish(self.path, code, now=102, host={})
            row = self.read()
            self.assertEqual(row['classification'], expected)
            self.assertIsNone(row['phases']['queue']['peak_rss_bytes'])
            self.assertEqual(row['complete_observation'], code in (0, 1))
        self.start()
        self.r.finish(self.path, 75, now=102, host={}, classification='superseded')
        self.assertEqual(self.read()['classification'], 'superseded')

    def test_postgres_reset_and_database_churn(self):
        """Requirement: PG18 deltas; boundary: reset must not yield negative or false zero."""
        before = {'wal': {'wal_bytes': 100, 'stats_reset': 'a'},
                  'io': [{'backend_type': 'client backend', 'object': 'relation',
                          'context': 'normal', 'write_bytes': 200, 'stats_reset': 'a'}],
                  'checkpointer': {'num_requested': 2, 'stats_reset': 'a'}, 'databases': [1, 2]}
        after = {'wal': {'wal_bytes': 160, 'stats_reset': 'a'},
                 'io': [{'backend_type': 'client backend', 'object': 'relation',
                         'context': 'normal', 'write_bytes': 250, 'stats_reset': 'a'}],
                 'checkpointer': {'num_requested': 3, 'stats_reset': 'a'}, 'databases': [1, 3]}
        delta = self.r.pg_delta(before, after)
        self.assertEqual(delta['wal']['wal_bytes'], 60)
        self.assertEqual(delta['io'][0]['write_bytes'], 50)
        self.assertEqual(delta['checkpointer']['num_requested'], 1)
        self.assertEqual(delta['databases_created_observed'], 1)
        self.assertEqual(delta['databases_dropped_observed'], 1)
        after['wal']['stats_reset'] = 'b'
        self.assertIsNone(self.r.pg_delta(before, after)['wal']['wal_bytes'])

    def test_postgres_samples_inside_index_lock(self):
        """Review 3: serialize SQL with registration; boundary: contending lock descriptor."""
        self.start()
        def sample():
            with (self.root / '.lock').open('a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return {'wal': {}, 'io': [], 'checkpointer': {}, 'databases': []}
        with patch.object(self.r, 'pg_sample', side_effect=sample), patch.object(
                sys, 'argv', ['receipts.py', 'postgres', str(self.path)]):
            self.assertEqual(self.r.main(), 0)

    def test_sampler_health_controls_completeness(self):
        """Review 4: observer health, not candidate rc; boundary: missing/crashed/gapped."""
        for code, health, expected in [
                (0, None, False),
                (0, dict(clean_exit=False, gap_count=0, started_at=100, first_sample_at=100, last_sample_at=104, samples=3), False),
                (0, dict(clean_exit=True, gap_count=1, started_at=100, first_sample_at=100, last_sample_at=104, samples=3), False),
                (0, dict(clean_exit=True, gap_count=0, started_at=100, first_sample_at=100, last_sample_at=101, samples=1), False),
                (74, dict(clean_exit=True, gap_count=0, started_at=100, first_sample_at=100, last_sample_at=108, samples=5), True)]:
            with self.subTest(code=code, health=health):
                self.start()
                if health:
                    self.r.mutate(self.path, lambda row: row.update(sampler=health))
                self.r.finish(self.path, code, now=108, host={})
                self.assertEqual(self.read()['complete_observation'], expected)

    def test_late_first_sample_is_incomplete(self):
        """Review 4: coverage starts at the first sample; boundary: first sample at seven seconds."""
        self.start()
        clock = [2]
        stopped = self.r.threading.Event()
        def sample(_):
            clock[0] = 7
            return '42 1 42 8 S 0:01.00 birth\n'
        def tick(_):
            clock[0] = 8
            self.r.finish(self.path, 0, now=108, host={})
            stopped.set()
        with patch.object(self.r.threading, 'Event', return_value=stopped), \
             patch.object(stopped, 'wait', side_effect=tick), \
             patch.object(self.r.signal, 'signal'), patch.object(self.r.os, 'kill'), \
             patch.object(self.r.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(self.r.time, 'time', side_effect=lambda: 100 + clock[0]), \
             patch.object(self.r, 'output', side_effect=sample), \
             patch.object(self.r, 'process_writes', return_value={}):
            self.r.monitor(self.path, 42)
        self.assertFalse(self.read()['complete_observation'])

    def test_dead_owner_does_not_recreate_receipt_directories(self):
        """Reproduced runner cleanup defect: abandon buffered writes after owner SIGKILL."""
        self.path = self.root / 'ci/receipts/fixture/run.json'
        self.start()
        before = self.path.read_text()
        alive = [True]
        stopped = self.r.threading.Event()
        def kill(*_):
            if not alive[0]:
                raise ProcessLookupError()
        def tick(_):
            alive[0] = False
            (self.root / 'ci').rename(self.root / 'retired')
        with patch.object(self.r.threading, 'Event', return_value=stopped), \
             patch.object(stopped, 'wait', side_effect=tick), \
             patch.object(self.r.signal, 'signal'), patch.object(self.r.os, 'kill', side_effect=kill), \
             patch.object(self.r, 'output', return_value='42 1 42 8 S 0:01.00 birth\n'), \
             patch.object(self.r, 'process_writes', return_value={}):
            self.assertEqual(self.r.monitor(self.path, 42), 0)
        self.assertFalse((self.root / 'ci').exists())
        self.assertEqual((self.root / 'retired/receipts/fixture/run.json').read_text(), before)

    def test_cpu_time_and_summary(self):
        """Review 5: CPU seconds; boundary: sum user+system across two commands."""
        self.start()
        for text in [' 2.50 real 1.25 user 0.75 sys\n', ' 1.00 real 0.50 user 0.25 sys\n']:
            self.r.record_rusage(self.path, self.r.parse_time(text))
        usage = self.read()['phases']['queue']['rusage']
        self.assertEqual(usage['user_seconds'], 1.75)
        self.assertEqual(usage['system_seconds'], 1.0)
        self.assertEqual(usage['cpu_seconds'], 2.75)
        self.r.finish(self.path, 0, now=108, host={})
        self.assertEqual(self.r.summarize(self.root)['groups'][0]['cpu_seconds'],
                         dict(median=2.75, p90=2.75, samples=1))

    def test_sampler_detects_long_gap_and_tree_cpu(self):
        """Reviews 4/5: sample health and tree CPU; boundary: eight-second gap, three groups."""
        self.start()
        clock = [0]
        rounds = []
        stopped = self.r.threading.Event()
        def tick(_):
            clock[0] += [2, 8, 2][len(rounds)]
            rounds.append(1)
            if len(rounds) == 3:
                self.r.finish(self.path, 0, now=112, host={})
                stopped.set()
        tables = ['42 1 42 8 S 0:01.00 birth\n43 42 43 16 S 0:02.00 child\n44 43 44 32 S 0:03.00 grandchild\n'] * 3
        with patch.object(self.r.threading, 'Event', return_value=stopped), \
             patch.object(stopped, 'wait', side_effect=tick), \
             patch.object(self.r.signal, 'signal'), patch.object(self.r.os, 'kill'), \
             patch.object(self.r.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(self.r.time, 'time', side_effect=lambda: 100 + clock[0]), \
             patch.object(self.r, 'output', side_effect=tables), \
             patch.object(self.r, 'process_writes', return_value={}):
            self.r.monitor(self.path, 42)
        row = self.read()
        self.assertTrue(row['sampler']['clean_exit'])
        self.assertEqual(row['sampler']['gap_count'], 1)
        self.assertEqual(row['sampler']['max_gap_seconds'], 8)
        self.assertFalse(row['complete_observation'])
        self.assertEqual(row['phases']['queue']['peak_rss_bytes'], 57344)
        self.assertEqual(row['phases']['queue']['sampled_cpu_seconds'], 6)
        self.assertEqual(self.r.summarize(self.root)['groups'][0]['sampled_cpu_seconds'],
                         dict(median=6, p90=6, samples=1))

    def test_byte_retention_and_stale_partial_files(self):
        """Review 6: byte and age caps; boundary: fresh partial/active receipt survive."""
        active = self.start()
        rows = []
        for name, ended in [('old', 101), ('middle', 102), ('new', 103)]:
            p = self.start(self.path.parent / (name + '.json'))
            self.r.finish(p, 0, now=ended, host={})
            rows.append(p)
        stale = self.path.parent / 'orphan.json.new'
        fresh = self.path.parent / 'fresh.json.new'
        stale.write_text('partial')
        fresh.write_text('partial')
        os.utime(stale, (time.time() - 3601, time.time() - 3601))
        cap = sum(p.stat().st_size for p in [active, *rows[1:]])
        with patch.object(self.r.subprocess, 'run') as run:
            self.r.prune(self.path.parent, max_bytes=cap)
        retired = run.call_args.args[0][2:]
        self.assertEqual(set(retired), {str(rows[0]), str(stale)})
        # A corrupt file and a single oversized receipt cannot evade the cap.
        broken = self.root / 'corrupt/broken.json'
        broken.parent.mkdir()
        broken.write_text('invalid' * 100)
        with patch.object(self.r.subprocess, 'run') as run:
            self.r.prune(broken.parent, max_bytes=100)
        self.assertEqual(run.call_args.args[0][2:], [str(broken)])
        large = self.start(self.root / 'large/only.json')
        self.r.finish(large, 0, now=101, host={})
        with patch.object(self.r.subprocess, 'run') as run:
            self.r.prune(large.parent, max_bytes=100)
        self.assertEqual(run.call_args.args[0][2:], [str(large)])

    def runner_fixture(self):
        sys.path.insert(0, str(KIT))
        from test_runner import Runner
        fixture = Runner('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_unknown_platform_skips_external_timing(self):
        """Portability brief: unsupported timing cannot suppress or alter a check."""
        for platform, code in [('Unknown', 0), ('Unknown', 7)]:
            with self.subTest(platform=platform, code=code):
                fixture = self.runner_fixture()
                fixture.configure(rc=code)
                timer = fixture.home / 'unsupported-time'
                timer.write_text('#!/bin/bash\nprintf called >>"$HOME/timer-called"\nexit 93\n')
                timer.chmod(0o755)
                # Keep native bootstrap tools; vary only the timing policy.
                adapter = fixture.bin / 'platform.sh'
                adapter.write_text(adapter.read_text().replace('/usr/bin/time', str(timer)))
                script = fixture.bin / 'run.sh'
                source = '. "$runner_dir/lib.sh"\n'
                script.write_text(script.read_text().replace(
                    source, source + 'platform_os=' + platform + '\n', 1).replace(
                    '/usr/bin/time', str(timer)))
                (fixture.home / 'release').touch()
                fixture.assert_result(fixture.start('portable'), 'portable', 0 if code == 0 else 1)
                self.assertEqual(fixture.count(), 1)
                self.assertEqual((fixture.dir / 'results/portable.check').read_text().strip(), str(code))
                self.assertFalse((fixture.home / 'timer-called').exists())
                receipt = fixture.ci / 'receipts/fixture/portable.json'
                fixture.wait_for(lambda: receipt.exists() and json.loads(receipt.read_text())['exit_code'] is not None)
                self.assertEqual(json.loads(receipt.read_text())['phases']['check']['timing_status'],
                                 'timing unavailable')

    def test_unwritable_timing_path_keeps_candidate_result(self):
        """Review 1: timing cannot gate execution; boundary: path is a directory, rc 7."""
        fixture = self.runner_fixture()
        fixture.configure(rc=7)
        (fixture.dir / 'results/untimed.time').mkdir()
        (fixture.home / 'release').touch()
        fixture.assert_result(fixture.start('untimed'), 'untimed', 1)
        self.assertEqual(fixture.count(), 1)
        self.assertEqual((fixture.dir / 'results/untimed.check').read_text().strip(), '7')
        receipt = fixture.ci / 'receipts/fixture/untimed.json'
        fixture.wait_for(lambda: receipt.exists() and json.loads(receipt.read_text())['exit_code'] is not None)
        row = json.loads(receipt.read_text())
        self.assertEqual(row['phases']['check']['timing_status'], 'timing unavailable')

    def test_hanging_sampler_cannot_hold_exit(self):
        """Review 2: publish before observer completion; boundary: sampler ignores TERM."""
        fixture = self.runner_fixture()
        script = fixture.bin / 'receipts.py'
        script.write_text(script.read_text().replace("if __name__ == '__main__':", """
if __name__ == '__main__' and sys.argv[1] == 'monitor':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(os.environ['HOME'], 'sampler-pid').write_text(str(os.getpid()))
    time.sleep(60)
if __name__ == '__main__':"""))
        def stop_sampler():
            pidfile = fixture.home / 'sampler-pid'
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(stop_sampler)
        proc = fixture.start('hung')
        fixture.wait_for(lambda: fixture.count() == 1)
        (fixture.home / 'release').touch()
        deadline = time.monotonic() + PUBLICATION_LIMIT_SECONDS
        result = fixture.dir / 'results/hung.exit'
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(result.exists(), 'sampler blocked terminal publication')
        fixture.assert_result(proc, 'hung', 0)

    def test_receipt_hook_has_hard_deadline(self):
        """Review 2: contested observer cannot wait five seconds in a phase hook."""
        script = (KIT / 'remote/run.sh').read_text()
        hook = script[script.index('receipt() {'):script.index('receipt_mode=')]
        (self.root / 'receipts.py').write_text('import time; time.sleep(10)\n')
        started = time.monotonic()
        result = subprocess.run(['/bin/bash', '-c', hook + '\nreceipt phase check', '_'],
            env=dict(os.environ, runner_dir=str(self.root), receipt_file=str(self.path)),
            capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, HOOK_LIMIT_SECONDS)

    def test_cluster_overlap_updates_both_runs(self):
        """Requirement: shared cluster attribution; boundary: first run finishes first."""
        a = self.start()
        b = self.start(self.root / 'fixture/other.json')
        snapshot = {'wal': {'wal_bytes': 100}, 'io': [], 'checkpointer': {}, 'databases': []}
        self.r.postgres(a, 'cluster-id', snapshot, now=101)
        historic = self.root / 'fixture/historic.json'
        historic.write_text('{}')
        with patch.object(self.r, 'read', wraps=self.r.read) as reads:
            self.r.postgres(b, 'cluster-id', snapshot, now=102)
        self.assertNotIn(historic, [call.args[0] for call in reads.call_args_list])
        snapshot['wal']['wal_bytes'] = 200
        self.r.postgres(a, 'cluster-id', snapshot, now=103, end=True)
        self.r.postgres(b, 'cluster-id', snapshot, now=103, end=True)
        self.assertEqual(self.read(a)['postgres']['overlapping_runs'], ['fixture/other'])
        self.assertEqual(self.read(b)['postgres']['overlapping_runs'], ['fixture/run'])
        self.assertEqual(self.read(a)['postgres']['attribution'], 'cluster-window')
        self.assertEqual(self.read(b)['postgres']['attribution'], 'cluster-window')
        self.assertEqual(self.read(a)['postgres']['delta']['wal']['wal_bytes'], 100)
        self.assertEqual(self.read(b)['postgres']['delta']['wal']['wal_bytes'], 100)
        self.r.finish(a, 0, now=104, host={})
        self.r.finish(b, 0, now=104, host={})
        self.assertEqual(self.r.summarize(self.root)['groups'][0]['wal_bytes']['samples'], 0)

    def test_summary_quantiles_missing_values_and_cache_rates(self):
        """Requirement: per-project/mode medians/p90; boundary: unknown counters excluded."""
        for n, duration in enumerate([1, 2, 3, 4, 100]):
            path = self.start(self.root / ('fixture/r%s.json' % n))
            self.r.cache(path, 'install', 'hit' if n < 2 else 'miss', 'input-match')
            self.r.cache(path, 'template', 'hit' if n == 0 else 'miss', 'template-ready')
            self.r.finish(path, 0, now=100 + duration, host={})
        summary = self.r.summarize(self.root, 'fixture')['groups'][0]
        self.assertEqual(summary['duration_seconds'], {'median': 3, 'p90': 100, 'samples': 5})
        self.assertEqual(summary['peak_rss_bytes']['samples'], 0)
        self.assertEqual(summary['install_hit_rate'], 0.4)
        self.assertEqual(summary['template_hit_rate'], 0.2)
        self.assertIsNone(summary['wal_bytes']['median'])

    def test_retention_uses_trash_and_keeps_active(self):
        """Requirement: bounded retention; boundary: active receipts survive the cap."""
        self.start()
        for n in range(3):
            p = self.start(self.root / ('fixture/done%s.json' % n))
            self.r.finish(p, 0, now=101 + n, host={})
        # Missing bundle uses the installed ctrash; exercise that explicit fallback.
        with patch.object(self.r.subprocess, 'run') as run, patch.object(self.r.Path, 'is_file', return_value=False):
            self.r.prune(self.root / 'fixture', keep=2)
        self.assertEqual(run.call_args.args[0], ['ctrash', str(self.root / 'fixture/done0.json')])
        with patch.object(self.r.subprocess, 'run') as run:
            self.r.prune(self.root / 'fixture', keep=2)
        self.assertEqual(run.call_args.args[0], ['/bin/sh', str(KIT / 'remote/ctrash.sh'),
                                                str(self.root / 'fixture/done0.json')])
        (self.root / 'fixture/done0.json.new').write_text('interrupted write')
        with patch.object(self.r.subprocess, 'run') as run:
            self.r.prune(self.root / 'fixture', keep=2)
        self.assertEqual(run.call_args.args[0], ['/bin/sh', str(KIT / 'remote/ctrash.sh'),
            str(self.root / 'fixture/done0.json'), str(self.root / 'fixture/done0.json.new')])
        folder = self.root / 'ci/receipts/fixture'
        for name, ended in [('old', 101), ('new', 102)]:
            p = self.start(folder / (name + '.json'))
            self.r.finish(p, 0, now=ended, host={})
        timing = self.root / 'ci/projects/fixture/results/old.time'
        timing.parent.mkdir(parents=True)
        timing.write_text('BSD time fixture')
        with patch.object(self.r.subprocess, 'run') as run:
            self.r.prune(folder, keep=1)
        self.assertEqual(run.call_args.args[0], ['/bin/sh', str(KIT / 'remote/ctrash.sh'),
            str(folder / 'old.json'), str(timing)])

    def test_platform_time_units_no_secret_capture(self):
        """Requirement: portable rusage; boundary: normalize Linux KiB and ignore commands."""
        parsed = self.r.parse_time('secret-value\n 1048576 maximum resident set size\n 7 block output operations\n')
        self.assertEqual(parsed, {'max_rss_bytes': 1048576, 'block_output_operations': 7})
        linux = self.r.parse_time('''Command being timed: "secret-value"
User time (seconds): 1.25
System time (seconds): 0.50
Maximum resident set size (kbytes): 2048
File system outputs: 9
''')
        self.assertEqual(linux, {'user_seconds': 1.25, 'system_seconds': 0.5,
            'cpu_seconds': 1.75, 'max_rss_bytes': 2097152, 'block_output_operations': 9})
        self.start()
        self.r.record_rusage(self.path, parsed)
        self.r.record_rusage(self.path, {'max_rss_bytes': 524288, 'block_output_operations': 3})
        self.assertEqual(self.read()['phases']['queue']['rusage'], {
            'max_rss_bytes': 1048576, 'block_output_operations': 10})

    def test_sampler_buffers_writes_and_flushes_at_exit(self):
        """Requirement: light SSD-wear observer; boundary: samples survive final verdict."""
        self.start()
        stopped = self.r.threading.Event()
        rounds = []
        def tick(_):
            rounds.append(1)
            if len(rounds) == 2:
                self.r.transition(self.path, 'check', now=102, host={})
            if len(rounds) == 4:
                self.r.finish(self.path, 0, now=104, host={})
                stopped.set()
        original_write = self.r.write
        with patch.object(self.r.threading, 'Event', return_value=stopped), \
             patch.object(stopped, 'wait', side_effect=tick), \
             patch.object(self.r.signal, 'signal'), patch.object(self.r.os, 'kill'), \
             patch.object(self.r.time, 'monotonic', return_value=0), \
             patch.object(self.r, 'output', side_effect=['42 1 42 8 S 0:01.00 birth\n',
                 '42 1 42 8 S 0:02.00 birth\n', '42 1 42 8 S 0:03.00 birth\n',
                 '42 1 42 8 S 0:04.00 birth\n']), \
             patch.object(self.r, 'process_writes', side_effect=[{'42': n} for n in (100, 200, 300, 400)]), \
             patch.object(self.r, 'write', wraps=original_write) as writes:
            self.r.monitor(self.path, 42)
        # Two phase/verdict writes plus two sample flushes, not four sample writes.
        self.assertEqual(writes.call_count, 4)
        row = self.read()
        self.assertEqual(row['phases']['queue']['samples'], 2)
        self.assertEqual(row['phases']['check']['samples'], 2)
        self.assertEqual(row['phases']['queue']['os_write_bytes'], 200)
        self.assertEqual(row['phases']['check']['os_write_bytes'], 200)
        self.assertEqual(row['phases']['check']['peak_rss_bytes'], 8192)
        self.assertEqual(row['phases']['queue']['sampled_cpu_seconds'], 2)
        self.assertEqual(row['phases']['check']['sampled_cpu_seconds'], 2)
        self.assertTrue(row['sampler']['clean_exit'])
        self.assertEqual(row['exit_code'], 0)

    def test_linux_reaped_child_bytes_are_not_added_twice(self):
        """Requirement: OS write bytes; boundary: Linux transfers reaped child I/O to parent."""
        self.start()
        processes = {10: {'group': 10, 'parent': 1}, 11: {'group': 10, 'parent': 10}}
        counts = {10: 100, 11: 50}
        def content(path):
            pid = int(str(path).split('/')[2])
            return 'write_bytes: %s\n' % counts[pid] if str(path).endswith('/io') else '0 (fixture) ' + ' '.join(['1'] * 20)
        with patch.object(self.r.sys, 'platform', 'linux'), patch.object(self.r.Path, 'read_text', content):
            first = self.r.process_writes(processes)
            counts[10] = 150
            second = self.r.process_writes({10: processes[10]})
        self.r.record_sample(self.path, 1024, first)
        self.r.record_sample(self.path, 1024, second)
        self.assertEqual(self.read()['phases']['queue']['os_write_bytes'], 150)

    def test_loop_stats_json_endpoint(self):
        """Requirement: ci-board JSON endpoint; boundary: read-only empty input."""
        proc = subprocess.run([sys.executable, str(KIT.parent / 'loop-stats/loop-stats.py'),
                               '--receipts', '--json', '--repo', 'fixture',
                               '--receipts-root', str(self.root)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {'schema_version': 1, 'groups': []})

    def test_runner_hooks_publish_finished_receipt(self):
        """Requirement: real phase hooks; boundary: pass, candidate fail and cached run."""
        sys.path.insert(0, str(KIT))
        from test_runner import Runner
        fixture = Runner('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        (fixture.source / '.gitignore').write_text('node_modules/\n')
        fixture.git('-C', fixture.source, 'add', '.gitignore')
        fixture.git('-C', fixture.source, '-c', 'user.name=Fixture', '-c',
                    'user.email=user@example.invalid', 'commit', '-qm', 'fixture install cache')
        fixture.sha = fixture.git('-C', fixture.source, 'rev-parse', 'HEAD')
        # Keep Runner's valid execution identity; legacy keys deliberately miss.
        (fixture.home / 'release').touch()
        for name, code in [('first', 0), ('cached', 0), ('failed', 1)]:
            fixture.configure(rc=code)
            with (fixture.dir / 'conf').open('a') as conf:
                conf.write("REMOTE_CI_INSTALL='mkdir -p node_modules'\nREMOTE_CI_INSTALL_INPUTS=source.txt\n")
            proc = fixture.start(name, force=bool(code))
            fixture.assert_result(proc, name, code)
            receipt = fixture.ci / 'receipts/fixture' / (name + '.json')
            fixture.wait_for(lambda: receipt.exists() and json.loads(receipt.read_text())['exit_code'] is not None)
            row = json.loads(receipt.read_text())
            self.assertEqual(row['exit_code'], code)
            self.assertIn('cleanup', row['phases'])
            self.assertEqual(row['cache']['tree']['outcome'], 'hit' if name == 'cached' else 'miss')
            if name != 'cached':
                self.assertIn('check', row['phases'])
                self.assertIsNotNone(row['phases']['check']['rusage']['max_rss_bytes'])
                self.assertEqual(row['cache']['install'], {
                    'outcome': 'miss' if name == 'first' else 'hit',
                    'reason': 'inputs-changed-or-missing' if name == 'first' else 'input-match'})
                self.assertEqual(row['turbo_size_delta_bytes'], 0)


def live_probe():
    """Independent runner-only comparison; isolated PG18 cluster and 64 MiB process."""
    r = module()
    pg_bin = Path('/opt/homebrew/opt/postgresql@18/bin')
    if not pg_bin.is_dir():
        raise RuntimeError('PostgreSQL 18 unavailable; probe not run')
    with tempfile.TemporaryDirectory(prefix='receipt-probe-', dir='/tmp') as temp:
        root = Path(temp)
        data = root / 'pg'
        env = dict(os.environ, PATH=str(pg_bin) + ':' + os.environ['PATH'],
                   PGHOST=temp, PGPORT='55439', PGUSER=os.environ['USER'],
                   PGDATABASE='postgres', PGCONNECT_TIMEOUT='2')
        def pg(*args):
            return subprocess.check_output([str(pg_bin / args[0]), *args[1:]],
                env=env, text=True, stderr=subprocess.DEVNULL, timeout=30).strip()
        pg('initdb', '-D', str(data), '-A', 'trust', '--no-locale', '--encoding=UTF8')
        pg('pg_ctl', '-D', str(data), '-l', str(root / 'pg.log'), '-o',
           "-k %s -h '' -p 55439 -c shared_buffers=32MB" % temp, 'start', '-w')
        try:
            def sql(query):
                return pg('psql', '-X', '-Atqc', query)
            path = root / 'receipts/probe/live.json'
            r.begin(path, 'probe', 'live', 'targeted')
            r.transition(path, 'check')
            with patch.dict(os.environ, env):
                before = int(sql('select wal_bytes from pg_stat_wal'))
                r.postgres(path, 'isolated-probe')
                sql("create table receipt_probe as select n, repeat(md5(n::text), 16) as payload from generate_series(1, 16000) n")
                sql('checkpoint')
                time.sleep(1.1)
                r.postgres(path, 'isolated-probe', end=True)
                after = int(sql('select wal_bytes from pg_stat_wal'))
            measured = r.read(path)['postgres']['delta']['wal']['wal_bytes']
            assert measured == after - before and measured > 8000000, (measured, after - before)
            usage = root / 'time.txt'
            # The command and its memory-heavy descendant use other
            # process groups. Independent ps observations prove that boundary.
            payload = 'import os,time; os.setpgid(0,0); data=bytearray(64*1024*1024); time.sleep(5)'
            launcher = ('import os,subprocess,sys; os.setpgid(0,0); '
                        'sys.exit(subprocess.call([sys.executable,"-c",sys.argv[1]]))')
            done, release = root / 'work-done', root / 'release'
            # Keep the runner owner alive until the sampler flushes, as run.sh
            # does after publishing .exit. A dead owner must abandon its buffer.
            owner = ('done=$1; release=$2; shift 2; "$@"; code=$?; : >"$done"; '
                     'while [ ! -f "$release" ]; do sleep .05; done; exit "$code"')
            child = subprocess.Popen(['/bin/bash', '-c', owner, '_', str(done), str(release),
                '/usr/bin/time', '-l', '-o', str(usage),
                sys.executable, '-c', launcher, payload],
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sampler = subprocess.Popen([sys.executable, str(KIT / 'remote/receipts.py'),
                'monitor', str(path), str(child.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(1)
                table = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,pgid='], text=True)
                processes = {int(pid): (int(parent), int(group)) for pid, parent, group in
                             (line.split() for line in table.splitlines())}
                owned = {child.pid}
                for _ in range(4):
                    owned.update(pid for pid, (parent, _) in processes.items() if parent in owned)
                groups = {processes[pid][1] for pid in owned if pid in processes}
                assert len(groups) >= 3, groups
                deadline = time.monotonic() + 20
                while not done.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                assert done.exists(), 'probe command did not finish'
                sampler.terminate()
                assert sampler.wait(timeout=60) == 0
            finally:
                release.touch()
                if sampler.poll() is None:
                    sampler.kill()
                    sampler.wait(timeout=60)
                assert child.wait(timeout=60) == 0
            reference = r.parse_time(usage.read_text())['max_rss_bytes']
            sampled = r.read(path)['phases']['check']['peak_rss_bytes']
            assert 0.75 * reference <= sampled <= 1.5 * reference, (sampled, reference)
            r.finish(path, 0)
            print('ok live receipt WAL=%s independent=%s RSS=%s time_maxrss=%s groups=%s' %
                  (measured, after - before, sampled, reference, len(groups)))
        finally:
            pg('pg_ctl', '-D', str(data), 'stop', '-m', 'fast', '-w')


if __name__ == '__main__':
    if '--live-probe' in sys.argv:
        live_probe()
    else:
        unittest.main()
