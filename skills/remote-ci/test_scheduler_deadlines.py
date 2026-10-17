"""Real phase subprocesses must respect the whole-job deadline, including silent ones."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'remote'))
import resources as r
import scheduler_process as sp


class Deadlines(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = patch.dict(os.environ, HOME=tmp.name)
        home.start()
        self.addCleanup(home.stop)
        pressure = patch.object(r, 'sample', return_value=dict(pressure=1, available_mb=20000))
        pressure.start()
        self.addCleanup(pressure.stop)
        old = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(sig, handler) for sig, handler in old.items()])
        self.q = r.Queue(tmp.name)
        self.marker = str(self.root / 'stop')

    def grant(self, deadline, fast=None):
        with self.q.transaction() as data:
            data['jobs']['job'] = dict(version=2, state='running', owner=os.getpid(),
                owner_birth=sp.birth(os.getpid()), lease_generation=7, workers=1,
                memory_mb=2048, deadline=deadline, job_deadline=fast)

    def execute(self, phase, sleep=0):
        code = 'import pathlib,sys,time; time.sleep(float(sys.argv[1])); pathlib.Path(sys.argv[2]).touch()'
        return sp.run(self.q, 'job', phase, 30, self.marker,
                      [sys.executable, '-c', code, str(sleep), str(self.root / 'completed')])

    def assert_reaped(self):
        with self.q.transaction() as data:
            self.assertFalse(data['jobs']['job'].get('process'))
            self.assertFalse(data['jobs']['job'].get('groups'))

    def test_expired_normal_job_never_starts_candidate(self):
        self.grant(time.time() - 1)
        self.assertEqual(self.execute('check'), 74)
        self.assertFalse((self.root / 'completed').exists())
        self.assertEqual(Path(self.marker).read_text(), '74')
        self.assert_reaped()

    def test_normal_job_deadline_stops_silent_phase_before_phase_limit(self):
        self.grant(time.time() + 3)
        started = time.monotonic()
        self.assertEqual(self.execute('check', 20), 74)
        self.assertLess(time.monotonic() - started, 15)
        self.assertFalse((self.root / 'completed').exists())
        self.assert_reaped()

    def test_cleanup_can_finish_after_original_job_deadline(self):
        self.grant(time.time() - 1)
        self.assertEqual(self.execute('cleanup', 0.1), 0)
        self.assertTrue((self.root / 'completed').exists())
        self.assert_reaped()

    def test_fast_deadline_cannot_extend_original_deadline(self):
        self.grant(time.time() - 1, fast=time.time() + 60)
        self.assertEqual(self.execute('check'), 74)
        self.assertFalse((self.root / 'completed').exists())

    def test_silent_work_inside_budget_passes(self):
        self.grant(time.time() + 15)
        self.assertEqual(self.execute('check', 0.2), 0)
        self.assertTrue((self.root / 'completed').exists())
        self.assert_reaped()


if __name__ == '__main__':
    unittest.main()
