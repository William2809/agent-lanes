"""Submission deadline helper preserves results and contains its process group."""
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


HELPER = Path(__file__).resolve().parent / 'bin/client_process.py'


class ClientProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='client-process-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=60)

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError as error:
            if error.errno == errno.ESRCH:
                return False
            raise

    def wait_gone(self, pid, seconds=300):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.alive(pid):
                return
            time.sleep(.05)
        self.fail('process survived bounded cleanup: %s' % pid)

    def wait_file(self, path, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(.02)
        self.fail('fixture did not publish process identity: %s' % path)

    def test_preserves_stdout_stderr_and_status(self):
        for status in (0, 7, 74):
            with self.subTest(status=status):
                result = subprocess.run([
                    sys.executable, str(HELPER), '5', sys.executable, '-c',
                    'import sys; print("transport-out"); print("transport-err", file=sys.stderr); '
                    'sys.exit(int(sys.argv[1]))', str(status),
                ], capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, status)
                self.assertEqual(result.stdout, 'transport-out\n')
                self.assertEqual(result.stderr, 'transport-err\n')

    def test_timeout_closes_captured_pipes_and_stops_grandchild(self):
        marker = self.root / 'grandchild.pid'
        script = (
            'import pathlib,subprocess,time; '
            'p=subprocess.Popen(["sleep","30"]); '
            'pathlib.Path(%r).write_text(str(p.pid)); '
            'print("before-timeout", flush=True); '
            'time.sleep(30)' % str(marker)
        )
        started = time.monotonic()
        result = subprocess.run([
            sys.executable, str(HELPER), '1', sys.executable, '-c', script,
        ], capture_output=True, text=True, timeout=60)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 74)
        self.assertLess(elapsed, 15)
        self.assertIn('before-timeout', result.stdout)
        self.assertIn('submission deadline', result.stderr)
        self.assertTrue(marker.exists())
        self.wait_gone(int(marker.read_text()))

    def test_supervisor_death_leaves_bounded_orphan_group(self):
        marker = self.root / 'orphan.pid'
        script = (
            'import pathlib,subprocess,time; '
            'p=subprocess.Popen(["sleep","30"]); '
            'pathlib.Path(%r).write_text(str(p.pid)); '
            'time.sleep(30)' % str(marker)
        )
        supervisor = subprocess.Popen([
            sys.executable, str(HELPER), '1', sys.executable, '-c', script,
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(supervisor)
        self.wait_file(marker)
        orphan = int(marker.read_text())
        supervisor.kill()
        supervisor.wait(timeout=60)
        self.wait_gone(orphan)

    def test_timeout_does_not_signal_unrelated_process_group(self):
        unrelated = subprocess.Popen(['sleep', '30'], start_new_session=True)
        self.processes.append(unrelated)
        result = subprocess.run([
            sys.executable, str(HELPER), '1', sys.executable, '-c',
            'import time; time.sleep(30)',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 74)
        self.assertIsNone(unrelated.poll())
        os.kill(unrelated.pid, signal.SIGTERM)
        unrelated.wait(timeout=60)


if __name__ == '__main__':
    unittest.main()
