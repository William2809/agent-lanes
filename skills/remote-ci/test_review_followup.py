"""PR1 review requirements. Execute through the remote resource queue."""
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

sys.path.insert(0, str(Path(__file__).resolve().parent / 'remote'))
import process_tree
import resources
import ci_telemetry
import test_runner
import test_receipts


class IdentityRace(unittest.TestCase):
    def test_reused_child_is_rejected_after_snapshot(self):
        # Review 2: a replacement with another parent must never become owned.
        tree = process_tree.Tree(10, '100.000000')
        rows = {10: dict(parent=1, birth='100.000000'), 20: dict(parent=10)}
        # The ps ancestry predates PID reuse. The identity probe sees the replacement.
        with patch.object(process_tree, 'birth', return_value='200.000000'), \
             patch.object(process_tree, 'identity', return_value=('200.000000', 1), create=True):
            self.assertEqual(set(tree.observe(rows)), {10})

    def test_child_cannot_predate_parent(self):
        # Review 2: even a matching ppid cannot authorize an older process.
        tree = process_tree.Tree(10, '100.000002')
        rows = {10: dict(parent=1, birth='100.000002'), 20: dict(parent=10, birth='100.000001')}
        self.assertEqual(set(tree.observe(rows)), {10})


class PolicyAndEvents(unittest.TestCase):
    def test_only_integer_kill_percent_controls_margin(self):
        # Review 3: the obsolete factor cannot override the host's 300 percent.
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp,
                AGENT_CI_KILL_PCT='300', AGENT_CI_KILL_FACTOR='0.1'):
            limits = resources.policy('runner')
        self.assertEqual(limits['KILL_PCT'], 300)
        self.assertNotIn('KILL_FACTOR', limits)

    def test_queue_events_use_receipt_id(self):
        # Review 4: all queue events can join the receipt without a hash mapping.
        with tempfile.TemporaryDirectory() as temp:
            queue = resources.Queue(temp)
            resources.event(queue, 'receipt-run', 'queued')
            self.assertEqual(list(ci_telemetry.read(queue.root/'telemetry'))[0]['run_id'], 'receipt-run')

    def test_telemetry_rejects_cost_fields_and_events(self):
        # Review 4: receipts are the only cost schema.
        with tempfile.TemporaryDirectory() as temp:
            for event, fields in [('sample', {}), ('phase-ended', {}), ('finished', {}),
                                  ('cleanup', {'duration_s': 1}), ('cleanup', {'peak_rss_mb': 1}),
                                  ('admitted', {'rss_mb': 1})]:
                with self.subTest(event=event, fields=fields):
                    self.assertFalse(ci_telemetry.emit(temp, event, run_id='run', **fields))


class ReceiptTree(unittest.TestCase):
    def test_reparented_member_contributes_to_receipt_peak(self):
        # Review 4: use the watchdog journal even after the parent exits.
        r = test_receipts.module()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'fixture/run.json'
            r.begin(path, 'fixture', 'run', 'targeted', now=100)
            journal = Path(temp)/'run.tree'
            tree = process_tree.Tree(43, '100.000000')
            tree.known[44] = '101.000000'
            tree.save(journal)
            stopped = r.threading.Event()
            def tick(_):
                stopped.set()
            with patch.object(r.threading, 'Event', return_value=stopped), \
                 patch.object(stopped, 'wait', side_effect=tick), \
                 patch.object(r.signal, 'signal'), patch.object(r.os, 'kill'), \
                 patch.object(r, 'output', return_value='42 1 42 8 S 0:01.00 owner\n44 1 44 32 S 0:02.00 child\n'), \
                 patch.object(process_tree, 'birth', return_value='101.000000'), \
                 patch.object(r, 'process_writes', return_value={}):
                r.monitor(path, 42, journal)
            self.assertEqual(json.loads(path.read_text())['phases']['queue']['peak_rss_bytes'], 32768)


    def test_empty_supervised_journal_keeps_other_phase_cost(self):
        # Review 4: receipts still own RSS for cleanup after a supervised phase.
        r = test_receipts.module()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'fixture/run.json'
            r.begin(path, 'fixture', 'run', 'targeted', now=100)
            r.transition(path, 'cleanup', now=101, host={})
            journal = Path(temp)/'run.tree'
            tree = process_tree.Tree(43, '100.000000')
            tree.known = {}
            tree.save(journal)
            stopped = r.threading.Event()
            with patch.object(r.threading, 'Event', return_value=stopped), \
                 patch.object(stopped, 'wait', side_effect=lambda _: stopped.set()), \
                 patch.object(r.signal, 'signal'), patch.object(r.os, 'kill'), \
                 patch.object(r, 'output', return_value='42 1 42 8 S 0:01.00 owner\n44 42 44 16 S 0:02.00 cleanup\n'), \
                 patch.object(r, 'process_writes', return_value={}):
                r.monitor(path, 42, journal)
            self.assertEqual(json.loads(path.read_text())['phases']['cleanup']['peak_rss_bytes'], 24576)


@unittest.skipUnless(sys.platform == 'darwin', 'shell runner fixture requires macOS')
class ShellRecovery(unittest.TestCase):
    setUp = test_runner.Runner.setUp
    git = test_runner.Runner.git
    configure = test_runner.Runner.configure
    stop_runs = test_runner.Runner.stop_runs
    start = test_runner.Runner.start
    wait_for = test_runner.Runner.wait_for
    log = test_runner.Runner.log
    count = test_runner.Runner.count
    assert_result = test_runner.Runner.assert_result

    def cleanup_fixture(self, kind):
        run = 'expired-' + kind
        saved = self.dir/'runs'/run
        saved.mkdir(parents=True)
        (saved/'conf').write_bytes((self.dir/'conf').read_bytes())
        pending = self.dir/'results'/(run+'.cleanup')
        pending.write_text(f'run={run}\nslot=1\ndatabase=fixture_ci\nrole=ci\npid=999999\n')
        journal = self.dir/'results'/(run+'.resource-stop.tree')
        if kind == 'corrupt':
            journal.write_text('{broken')
        else:
            process_tree.Tree(999990, '100.000000').save(journal)
            script = self.bin/'process_tree.py'
            script.write_text(script.read_text() + '\n_original_birth = birth\ndef birth(pid):\n'
                              '    if pid == 999990: raise PermissionError(pid)\n    return _original_birth(pid)\n')
            script = self.bin/'resources.py'
            script.write_text(script.read_text().replace("if __name__ == '__main__':",
                "_original_processes = processes\ndef processes():\n"
                "    rows = _original_processes()\n"
                "    rows[999990] = dict(parent=1, group=999990, rss=1, start='unknown')\n"
                "    return rows\nif __name__ == '__main__':"))
        queue = resources.Queue(self.home/'.local/state/agent-ci')
        token = f'fixture-{run}-999999'
        with queue.transaction() as data:
            data['jobs'][token] = dict(owner=999999, start='gone', tree_marker=str(journal),
                workers=1, memory_mb=100, quarantine_since=time.time(), run_id=run)
        def recover():
            return subprocess.run(['/bin/bash', str(self.bin/'run.sh'), '--cleanup', str(pending)],
                                  env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(recover().returncode, 76)
        self.assertTrue(pending.exists())
        with queue.transaction() as data:
            data['jobs'][token]['quarantine_since'] = time.time()-601
        result = recover()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertFalse(pending.exists())
        self.assertIn('cleanup recovered', result.stdout)
        with queue.transaction() as data:
            self.assertNotIn(token, data['jobs'])
        events = list(ci_telemetry.read(queue.root/'telemetry'))
        self.assertEqual([(e['event'], e['reason'], e['run_id']) for e in events],
                         [('cleanup', 'quarantine-expired', run)])
        (self.home/'release').touch()
        self.assert_result(self.start('next-'+kind), 'next-'+kind, 0)

    def test_corrupt_journal_shell_cleanup_expires(self):
        self.cleanup_fixture('corrupt')

    def test_unreadable_identity_shell_cleanup_expires(self):
        self.cleanup_fixture('unreadable')

    def test_hung_watchdog_cannot_block_exit(self):
        # Review 5: real shell wait must terminate even if the watchdog ignores TERM.
        script = self.bin/'resources.py'
        script.write_text(script.read_text().replace("if __name__ == '__main__':", """
if __name__ == '__main__' and sys.argv[1] == 'watch':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(os.environ['HOME'], 'watchdog-pid').write_text(str(os.getpid()))
    time.sleep(180)
if __name__ == '__main__':"""))
        def cleanup():
            path = self.home/'watchdog-pid'
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(cleanup)
        run = self.start('hung-watchdog')
        self.wait_for(lambda: (self.home/'watchdog-pid').exists() and self.count() == 1)
        (self.home/'release').touch()
        self.assert_result(run, 'hung-watchdog', 76)
        self.assertIn('watchdog shutdown deadline', self.log('hung-watchdog'))
