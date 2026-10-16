"""Run on the remote queue: tiny ownership fixtures, never a host stress test."""
from contextlib import contextmanager, redirect_stdout
import io
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT / 'remote'))
import ci_telemetry as telemetry
import process_tree
import resources as r


def proc(parent, group, birth, rss=1024):
    return dict(parent=parent, group=group, birth=birth, rss=rss, start=birth)


class Ownership(unittest.TestCase):
    def test_detached_and_reparented_child_remains_owned(self):
        tree = process_tree.Tree(10, '100.000000')
        initial = {10: proc(1, 10, '100.000000'), 20: proc(10, 20, '101.000000'), 30: proc(1, 30, '99.000000')}
        self.assertEqual(set(tree.observe(initial)), {10, 20})
        later = {20: proc(1, 20, '101.000000'), 21: proc(20, 21, '102.000000'), 30: initial[30]}
        self.assertEqual(set(tree.observe(later)), {20, 21})

    def test_reused_pid_cannot_authorize_descendants_or_signals(self):
        tree = process_tree.Tree(10, 'old')
        rows = {10: proc(1, 10, 'new'), 20: proc(10, 20, 'innocent')}
        with patch.object(process_tree.os, 'kill') as kill:
            self.assertTrue(tree.signal(signal.SIGKILL, rows))
        kill.assert_not_called()
        self.assertEqual(tree.known, {})

    def test_identity_rechecked_immediately_before_signal(self):
        tree = process_tree.Tree(10, 'old')
        with patch.object(process_tree, 'birth', return_value='new'), patch.object(process_tree.os, 'kill') as kill:
            tree.signal(signal.SIGTERM, {10: proc(1, 10, 'old')})
        kill.assert_not_called()

    def test_unknown_birth_retains_ownership_without_authorizing_children(self):
        tree = process_tree.Tree(10, '100.000000')
        rows = {10:dict(parent=1,group=10,rss=1,start='coarse'),
                20:dict(parent=10,group=20,rss=1,start='coarse')}
        with patch.object(process_tree,'birth',side_effect=PermissionError), patch.object(process_tree.os,'kill') as kill:
            self.assertEqual(set(tree.observe(rows)), {10})
            self.assertFalse(tree.reap(lambda:rows,grace=0))
        kill.assert_not_called()

    def test_unreaped_process_does_not_report_clean(self):
        tree = process_tree.Tree(10, '100.000000')
        with patch.object(process_tree, 'birth', return_value='100.000000'), patch.object(process_tree.os, 'kill'), \
                patch.object(process_tree.os, 'killpg'):
            self.assertFalse(tree.reap(lambda: {10: proc(1, 10, '100.000000')}, grace=0))

    def test_ownership_journal_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'tree'
            tree = process_tree.Tree(10, '100.000000')
            tree.observe({10: proc(1, 10, '100.000000'), 20: proc(10, 20, '101.000000')})
            tree.save(path)
            restored = process_tree.Tree.load(path)
            self.assertEqual(restored.known, tree.known)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_limit_verdict_is_written_before_cleanup_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp)/'stop'
            tree = process_tree.Tree(10,'100.000000')
            rows = {10:proc(1,10,'100.000000',rss=9000)}
            def cleanup(_snapshot):
                self.assertEqual(marker.read_text(),'75')
                tree.known = {}
                return True
            with patch.object(r,'Tree',return_value=tree), patch.object(tree,'reap',side_effect=cleanup), \
                    patch.object(r,'processes',return_value=rows), \
                    patch.object(r,'sample',return_value=dict(load=0,available_mb=1000,pressure=1)):
                self.assertEqual(r.watch(10,30,1,str(marker),telemetry_root=Path(temp)/'events'),75)

    def test_real_detached_child_reaped_after_parent_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            child_file, release = Path(temp)/'child', Path(temp)/'release'
            script = ('import pathlib,subprocess,sys,time; '
                      'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],start_new_session=True); '
                      'pathlib.Path(sys.argv[1]).write_text(str(p.pid)); '
                      '\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(.02)')
            root = subprocess.Popen([sys.executable, '-c', script, str(child_file), str(release)], start_new_session=True)
            unrelated = subprocess.Popen(['sleep', '30'], start_new_session=True)
            tree = process_tree.Tree(root.pid)
            child = None
            try:
                deadline = time.monotonic()+60
                while time.monotonic() < deadline:
                    members = tree.observe(r.processes())
                    if child_file.exists() and child_file.read_text():
                        child = int(child_file.read_text())
                        if child in members:
                            break
                    time.sleep(.05)
                self.assertIn(child, members)
                self.assertNotEqual(members[child]['group'], members[root.pid]['group'])
                release.touch()
                root.wait(timeout=60)
                self.assertIn(child, tree.observe(r.processes()))
                self.assertTrue(tree.reap(r.processes))
                self.assertNotIn(child, r.processes())
                self.assertIsNone(unrelated.poll())
            finally:
                tree.reap(r.processes)
                if root.poll() is None:
                    root.kill()
                root.wait(timeout=60)
                unrelated.terminate()
                unrelated.wait(timeout=60)


class ReviewMemory(unittest.TestCase):
    def test_tree_budget_margin_and_peak(self):
        # Brief 2: 1.2x and exactly 1.5x may run; 2x and pressure 4 must stop.
        for mb, pressure, expected in ((120, 1, 0), (150, 1, 0), (200, 1, 75), (120, 4, 75)):
            with self.subTest(mb=mb, pressure=pressure), tempfile.TemporaryDirectory() as temp:
                marker = Path(temp)/'stop'
                tree = process_tree.Tree(10, '100.000000')
                rows = {10: proc(1, 10, '100.000000', rss=20*1024),
                        20: proc(10, 20, '101.000000', rss=(mb-20)*1024)}
                stream = io.StringIO()
                with patch.object(r, 'Tree', return_value=tree), \
                        patch.object(tree, 'reap', return_value=True), \
                        patch.object(r, 'processes', side_effect=[rows, {}]), \
                        patch.object(r, 'sample', return_value=dict(load=0, available_mb=1000, pressure=pressure)), \
                        patch.object(r.time, 'sleep'), patch.dict(os.environ, HOME=temp, AGENT_CI_KILL_PCT='150'), \
                        redirect_stdout(stream):
                    self.assertEqual(r.watch(10, 30, 100, marker, telemetry_root=Path(temp)/'events'), expected)
                self.assertEqual(stream.getvalue().count(f'== memory peak {mb} MB of 100 granted'), 1)
                self.assertEqual(marker.exists(), expected != 0)

    def test_host_reads_margin_and_quarantine_with_lanes_precedence(self):
        # Brief 2/3: host keys follow the existing host config precedence.
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp):
            for name, percent, seconds in (('agent-lanes', 200, 20), ('agent-lanes', 175, 30)):
                path = Path(temp)/'.config'/name/'config'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'AGENT_CI_KILL_PCT={percent}\nAGENT_CI_CLEANUP_QUARANTINE_S={seconds}\n')
            limits = r.policy('runner')
            self.assertEqual(limits['KILL_PCT'], 175)
            self.assertEqual(limits['CLEANUP_QUARANTINE_S'], 30)


class ReviewQuarantine(unittest.TestCase):
    def fixture(self, temp, kind):
        path = Path(temp)/'ownership.tree'
        rows = {}
        if kind == 'corrupt':
            path.write_text('{broken')
        elif kind == 'shape':
            path.write_text('[]')
        elif kind == 'unreadable':
            process_tree.Tree(10, '100.000000').save(path)
            rows[10] = proc(1, 10, None)
        return dict(owner=30, start='owner', watcher=40, watcher_start='watch',
                    tree_marker=str(path), workers=1, memory_mb=100), rows

    def test_unknown_cleanup_expires_after_owner_and_watcher_die(self):
        # Brief 3: each uncertain case gets 600 seconds after supervision ends.
        for kind in ('missing', 'corrupt', 'shape', 'unreadable'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                row, rows = self.fixture(temp, kind)
                with patch.object(process_tree, 'birth', side_effect=PermissionError), \
                        patch.dict(os.environ, HOME=temp, AGENT_CI_CLEANUP_QUARANTINE_S='600'):
                    for pid, identity in ((30, 'owner'), (40, 'watch')):
                        with patch.object(r.time, 'time', return_value=1000):
                            self.assertTrue(r.live(row, rows | {pid: proc(1, pid, identity)}))
                    with patch.object(r.time, 'time', return_value=2000):
                        self.assertTrue(r.live(row, rows))
                    with patch.object(r.time, 'time', return_value=2599):
                        self.assertTrue(r.live(row, rows))
                    with patch.object(r.time, 'time', return_value=2600):
                        self.assertFalse(r.live(row, rows))

    def test_release_expires_uncertainty_and_emits_cleanup_once(self):
        # Brief 3: release frees the reservation and records quarantine expiry.
        for kind in ('missing', 'corrupt', 'shape', 'unreadable'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                queue = r.Queue(temp)
                row, rows = self.fixture(temp, kind)
                with queue.transaction() as data:
                    data['jobs']['job'] = row
                with patch.object(process_tree, 'birth', side_effect=PermissionError), \
                        patch.object(r, 'processes', return_value=rows), \
                        patch.dict(os.environ, HOME=temp, AGENT_CI_CLEANUP_QUARANTINE_S='600'):
                    with patch.object(r.time, 'time', return_value=2000):
                        self.assertFalse(queue.release('job'))
                    with patch.object(r.time, 'time', return_value=2599):
                        self.assertFalse(queue.release('job'))
                    with patch.object(r.time, 'time', return_value=2600):
                        self.assertTrue(queue.release('job'))
                        self.assertTrue(queue.release('job'))
                with queue.transaction() as data:
                    self.assertEqual(data['jobs'], {})
                events = list(telemetry.read(queue.root/'telemetry'))
                self.assertEqual([(e['event'], e['reason']) for e in events], [('cleanup', 'quarantine-expired')])


class Telemetry(unittest.TestCase):
    def test_allowlist_rejects_commands_env_and_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as temp:
            for fields in (dict(command='secret'), dict(environment={'TOKEN':'secret'}),
                           dict(wait_s=float('nan')), dict(wait_s=10**400), dict(reason='token=secret')):
                self.assertFalse(telemetry.emit(temp, 'admitted', **fields))
            self.assertEqual(list(telemetry.read(temp)), [])

    def test_rotation_bounds_disk_and_keeps_parseable_events(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(telemetry, 'MAX_BYTES', 512):
            for i in range(40):
                self.assertTrue(telemetry.emit(temp, 'admitted', run_id='test', wait_s=i))
            files = list(Path(temp).glob('*.jsonl'))
            self.assertEqual(len(files), 2)
            self.assertLessEqual(sum(p.stat().st_size for p in files), 1024)
            self.assertEqual(list(telemetry.read(temp))[-1]['wait_s'], 39)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in files))

    def test_corrupt_tail_and_missing_files_are_nonfatal(self):
        with tempfile.TemporaryDirectory() as temp:
            telemetry.emit(temp, 'admitted', wait_s=12)
            with (Path(temp)/'events.jsonl').open('a') as stream:
                stream.write('{broken\n')
            self.assertEqual(telemetry.summary(temp)['wait_p95_s'], 12)

    def test_full_disk_does_not_change_execution_outcome(self):
        with patch.object(telemetry.os, 'open', side_effect=OSError('disk full')):
            self.assertFalse(telemetry.emit('/tmp', 'stopped', exit_code=75))


class Admission(unittest.TestCase):
    def test_snapshot_taken_after_lock_cannot_erase_new_submitter(self):
        # The other submitter appears while this caller acquires the ledger lock.
        with tempfile.TemporaryDirectory() as temp:
            queue = r.Queue(temp)
            original = queue.transaction
            inside = [False]
            rows = {10: proc(1, 10, 'a'), 20: proc(1, 20, 'b')}

            @contextmanager
            def transaction():
                with original() as data:
                    inside[0] = True
                    if not data['jobs']:
                        data['jobs']['other'] = dict(owner=20, start='b', sequence=1, workers=1, memory_mb=1024)
                        data['sequence'] = 1
                    yield data
                    inside[0] = False

            def snapshot():
                self.assertTrue(inside[0], 'pre-lock process snapshot can erase a concurrently registered job')
                return rows

            metrics = dict(cores=8, load=0, available_mb=12000, pressure=1)
            limits = dict(MAX_JOBS=2, MAX_WORKERS=2, CPU_RESERVE=2, MEM_RESERVE_MB=1024, WORKER_MB=1024)
            with patch.object(queue, 'transaction', transaction), patch.object(r, 'processes', snapshot), \
                    patch.object(r, 'sample', return_value=metrics), patch.object(r, 'local_allowed'):
                self.assertEqual(r.acquire(queue, 'self', 10, time.time()+10, limits, 1), (1, 1024))
            with original() as data:
                self.assertEqual(set(data['jobs']), {'self', 'other'})

    def test_live_watchdog_keeps_orphan_capacity_until_cleanup(self):
        row = dict(owner=10, start='gone', watcher=20, watcher_start='watch')
        self.assertTrue(r.live(row, {20: proc(1, 20, 'watch')}))
        self.assertFalse(r.live(row, {20: proc(1, 20, 'reused')}))

    def test_release_retains_live_descendant_grant_until_reaped(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = r.Queue(temp)
            path = Path(temp)/'ownership'
            tree = process_tree.Tree(10,'100.000000')
            tree.save(path)
            with queue.transaction() as data:
                data['jobs']['job'] = dict(tree_marker=str(path),workers=1,memory_mb=1024)
            with patch.object(r,'processes',return_value={10:proc(1,10,'100.000000')}):
                self.assertFalse(queue.release('job'))
            with queue.transaction() as data:
                self.assertTrue(data['jobs']['job']['cleanup_pending'])
            with patch.object(r,'processes',return_value={}):
                self.assertTrue(queue.release('job'))


@unittest.skipUnless(sys.platform == 'darwin', 'runner fixture requires macOS')
class RunnerIntegration(unittest.TestCase):
    import test_runner as fixtures
    setUp = fixtures.Runner.setUp
    git = fixtures.Runner.git
    configure = fixtures.Runner.configure
    stop_runs = fixtures.Runner.stop_runs
    start = fixtures.Runner.start
    wait_for = fixtures.Runner.wait_for
    log = fixtures.Runner.log
    count = fixtures.Runner.count
    assert_result = fixtures.Runner.assert_result

    def test_no_exclusive_host_runs_in_slot_and_returns_check_rc(self):
        # Brief 1: fallback must run the check and keep its result, including nonzero rc.
        with (self.home/'.config/agent-lanes/config').open('a') as stream:
            stream.write('AGENT_CI_ALLOW_EXCLUSIVE=0\n')
        for rc in (0, 1):
            with self.subTest(rc=rc):
                self.configure(rc=rc, exclusive=True)
                with (self.dir/'conf').open('a') as stream:
                    stream.write("REMOTE_CI_PREPARE='sleep 1'\n")
                run = f'fallback-{rc}'
                first = self.start(run, force=True)
                self.wait_for(lambda: (self.dir/'slots/1.run').exists() or first.poll() is not None)
                self.assertIsNone(first.poll(), self.log(run))
                self.assertEqual((self.dir/'slots/1.run').read_text().strip(), run)
                (self.home/'release').touch()
                self.assert_result(first, run, rc)
                self.assertEqual((self.dir/'results'/(run+'.check')).read_text().strip(), str(rc))
                self.assertEqual(self.log(run).count('exclusive request uses a project slot on this host'), 1)
                self.assertEqual(self.log(run).count('== memory peak '), 1)
                (self.home/'release').rename(self.home/f'released-{rc}')
        self.assertEqual(self.count(), 2)

    def test_load_project_does_not_pollute_captured_remote_output(self):
        # Brief 1: remote() merges stderr; loading config must leave both streams empty.
        self.configure(exclusive=True)
        with (self.home/'.config/agent-lanes/config').open('a') as stream:
            stream.write('AGENT_CI_ALLOW_EXCLUSIVE=0\n')
        call = subprocess.run(['/bin/bash', '-c', '. "$1"; load_project fixture', '_', str(self.bin/'lib.sh')],
                              env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(call.returncode, 0)
        self.assertEqual(call.stdout + call.stderr, '')

    def test_prune_removes_stop_records_only_with_expired_results(self):
        # Brief 4: prune the old exit and its two resource-stop companions together.
        results = self.dir/'results'
        for i in range(41):
            for suffix in ('.exit', '.resource-stop', '.resource-stop.tree'):
                path = results/(f'run-{i:02d}' + suffix)
                path.write_text('0')
                os.utime(path, (1000+i, 1000+i))
        orphan = results/'pending.resource-stop.tree'
        orphan.write_text('{}')
        call = subprocess.run(['/bin/bash', str(self.bin/'prune.sh')], env=self.env,
                              text=True, capture_output=True, timeout=60)
        self.assertEqual(call.returncode, 0, call.stdout+call.stderr)
        for suffix in ('.exit', '.resource-stop', '.resource-stop.tree'):
            self.assertFalse((results/('run-00'+suffix)).exists())
            self.assertTrue((results/('run-01'+suffix)).exists())
        self.assertTrue(orphan.exists())

    def test_host_cannot_silently_downgrade_required_isolation(self):
        self.configure(exclusive=True)
        with (self.home/'.config/agent-lanes/config').open('a') as stream:
            stream.write('AGENT_CI_ALLOW_EXCLUSIVE=0\n')
        # Brief 1 replaces rejection with visible slot fallback. Keep exact assertions.
        (self.home/'release').touch()
        self.assert_result(self.start('isolation'),'isolation',0)
        self.assertEqual(self.count(),1)
        self.assertEqual(self.log('isolation').count('exclusive request uses a project slot on this host'), 1)

    def test_receipt_cost_and_queue_events_share_run_id(self):
        # Review 4 transfers the existing peak/completion contract to receipts.
        first = self.start('telemetry')
        self.wait_for(lambda: self.count() == 1)
        time.sleep(3)  # Receipt sampler interval is two seconds.
        (self.home/'release').touch()
        self.assert_result(first, 'telemetry', 0)
        events = list(telemetry.read(self.home/'.local/state/agent-ci/telemetry'))
        import ci_profiles
        receipt = json.loads((self.ci/'receipts/fixture/telemetry.json').read_text())
        observations = [ci_profiles.normalize(receipt)]
        self.assertEqual(len(observations), 1)
        self.assertTrue(observations[0]['complete'])
        self.assertGreater(observations[0]['peak_rss_mb'], 0)
        self.assertEqual(receipt['exit_code'], 0)
        self.assertTrue(events)
        self.assertEqual({e['run_id'] for e in events}, {receipt['run_id']})
        self.assertTrue({'queued', 'admitted', 'cleanup'} <= {e['event'] for e in events})
        self.assertTrue(all(e['event'] in {'queued', 'blocked', 'admitted', 'stopped', 'cleanup'} for e in events))

    def test_recovery_refuses_to_signal_a_live_owner(self):
        first = self.start('live')
        self.wait_for(lambda: self.count() == 1)
        saved = self.dir/'runs/live'
        saved.mkdir(parents=True)
        (saved/'conf').write_bytes((self.dir/'conf').read_bytes())
        pending = self.dir/'results/live.cleanup'
        pending.write_text('run=live\nslot=1\ndatabase=fixture_ci\nrole=ci\npid=%s\n' % first.pid)
        recovered = subprocess.run(['/bin/bash', str(self.bin/'run.sh'), '--cleanup', str(pending)],
                                   env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(recovered.returncode, 76, recovered.stdout+recovered.stderr)
        self.assertIsNone(first.poll())
        (self.home/'release').touch()
        self.assert_result(first,'live',0)

    def test_observed_detached_child_is_cleaned_on_success(self):
        script = ('import subprocess,sys,pathlib,time; '
                  'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],start_new_session=True); '
                  'pathlib.Path.home().joinpath("detached").write_text(str(p.pid)); '
                  '\nwhile not pathlib.Path.home().joinpath("release").exists(): time.sleep(.05)')
        (self.home/'spawn.py').write_text(script)
        first = self.start('detached', command='python3 "$HOME/spawn.py"')
        path = self.dir/'results/detached.resource-stop.tree'
        child = self.home/'detached'
        self.wait_for(lambda: child.exists() and path.exists() and
                      child.read_text() in json.loads(path.read_text())['known'])
        pid = int(child.read_text())
        (self.home/'release').touch()
        self.assert_result(first,'detached',0)
        self.assertNotIn(pid,r.processes())


if __name__ == '__main__':
    unittest.main()
