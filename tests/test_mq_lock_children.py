"""Brief 1: a background service must not keep a completed runner's slot."""
import os
import shutil
import subprocess
import time
import unittest

from workflow_fixture import ROOT, WorkflowFixture


class RunnerChildLockTests(unittest.TestCase):
    def test_background_service_does_not_keep_completed_runner_lock(self):
        # Brief 1; reproduced defect in the kernel-lock fix: a refresh daemon inherits FD 9.
        # Boundary: land exits after spawning a background process that stays alive.
        # Gaps: land and the daemon are local stubs; no real project server starts.
        f = WorkflowFixture(('b',))
        self.addCleanup(f.close)
        tools = f.root / 'tools'
        for directory in ('bin', 'skills/agent-workers/scripts', 'skills/wt-dev/bin',
                          'skills/remote-ci/bin', 'skills/remote-ci/remote'):
            shutil.copytree(ROOT / directory, tools / directory)
        if os.environ.get('MQ_CHILD_SOURCE'):
            shutil.copyfile(os.environ['MQ_CHILD_SOURCE'], tools / 'bin/mq')
        if os.environ.get('MQ_CHILD_HELPER_SOURCE'):
            shutil.copyfile(os.environ['MQ_CHILD_HELPER_SOURCE'], tools / 'bin/mq-lock.py')
        mq = str(tools / 'bin/mq')
        f.env['PATH'] = f.env['PATH'].replace(str(ROOT / 'bin'), str(tools / 'bin'))
        f.commit(f.lanes['b'], 'b.txt', 'b\n')
        f.run(mq, 'add', 'b')
        release = f.root / 'background.release'
        self.addCleanup(release.touch)
        f.shim('land', '''#!/usr/bin/env python3
import os, pathlib, subprocess, time
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
subprocess.Popen(['python3', '-c', """
import os, pathlib, time
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
(root / 'background.started').touch()
while not (root / 'background.release').exists(): time.sleep(0.02)
"""], close_fds=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
until = time.monotonic() + 15
while not (root / 'background.started').exists() and time.monotonic() < until: time.sleep(0.02)
print('LANDED fixture')
''')
        self.assertIn('mq done: landed 1;', f.run(mq, 'run').stdout)
        self.assertTrue((f.root / 'background.started').exists())
        self.assertFalse(release.exists())
        probe = f.run('python3', '-c', '''
import fcntl, os
fd = os.open('.git/mq-run.guard', os.O_RDWR)
try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError: raise SystemExit(1)
''', check=False)
        self.assertEqual(probe.returncode, 0, 'background service retained the kernel lock')
        self.assertIn('mq done: landed 0;', f.run(mq, 'run').stdout)

    def test_reused_owner_marker_without_descriptor_cannot_bypass_lock(self):
        # Brief 1 (PID reuse). Boundary: the old owner PID matches a new process with no guard FD.
        # Gap: reuse is injected in the environment; the OS does not recycle a real PID here.
        f = WorkflowFixture(('b',))
        self.addCleanup(f.close)
        tools = f.root / 'tools'
        for directory in ('bin', 'skills/agent-workers/scripts', 'skills/wt-dev/bin',
                          'skills/remote-ci/bin', 'skills/remote-ci/remote'):
            shutil.copytree(ROOT / directory, tools / directory)
        if os.environ.get('MQ_CHILD_SOURCE'):
            shutil.copyfile(os.environ['MQ_CHILD_SOURCE'], tools / 'bin/mq')
        if os.environ.get('MQ_CHILD_HELPER_SOURCE'):
            shutil.copyfile(os.environ['MQ_CHILD_HELPER_SOURCE'], tools / 'bin/mq-lock.py')
        mq = str(tools / 'bin/mq')
        f.env['PATH'] = f.env['PATH'].replace(str(ROOT / 'bin'), str(tools / 'bin'))
        f.commit(f.lanes['b'], 'b.txt', 'b\n')
        f.run(mq, 'add', 'b')
        f.shim('land', '#!/bin/sh\ntouch "$FIXTURE_ROOT/unlocked.land"\necho "LANDED fixture"\n')
        holder = subprocess.Popen(['python3', '-c', '''
import fcntl, os, pathlib, time
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
fd = os.open('.git/mq-run.guard', os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
(root / 'guard.entered').touch()
while not (root / 'guard.release').exists(): time.sleep(0.02)
'''], cwd=f.repo, env=f.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def close_holder():
            (f.root / 'guard.release').touch()
            holder.wait(timeout=60)
        self.addCleanup(close_holder)
        until = time.monotonic() + 15
        while not (f.root / 'guard.entered').exists() and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue((f.root / 'guard.entered').exists())
        result = f.run('sh', '-c', 'MQ_RUN_OWNER=$$; export MQ_RUN_OWNER; exec sh "$1" run', 'sh', mq)
        self.assertIn('runner already active', result.stdout)
        self.assertFalse((f.root / 'unlocked.land').exists())
        self.assertEqual(f.queue()[0][1], 'ready')

    def test_birthless_owner_of_native_guard_reports_older_runner(self):
        # Re-review B. Boundary: an earlier guard-enabled runner has no birth record.
        # Gap: a Python holder models that runner; no old binary or production land runs.
        f = WorkflowFixture(('b',))
        self.addCleanup(f.close)
        tools = f.root / 'tools'
        for directory in ('bin', 'skills/agent-workers/scripts', 'skills/wt-dev/bin',
                          'skills/remote-ci/bin', 'skills/remote-ci/remote'):
            shutil.copytree(ROOT / directory, tools / directory)
        if os.environ.get('MQ_CHILD_HELPER_SOURCE'):
            shutil.copyfile(os.environ['MQ_CHILD_HELPER_SOURCE'], tools / 'bin/mq-lock.py')
        mq = str(tools / 'bin/mq')
        f.env['PATH'] = f.env['PATH'].replace(str(ROOT / 'bin'), str(tools / 'bin'))
        f.commit(f.lanes['b'], 'b.txt', 'b\n')
        f.run(mq, 'add', 'b')
        holder = subprocess.Popen(['python3', '-c', """
import fcntl, os, pathlib, time
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
fd = os.open('.git/mq-run.guard', os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
lock = root / 'home/.claude/state/mq/demo/run.lock'
lock.mkdir()
(lock / 'pid').write_text(str(os.getpid()) + '\\n')
(root / 'guard.entered').touch()
while not (root / 'guard.release').exists(): time.sleep(0.02)
"""], cwd=f.repo, env=f.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def close_holder():
            (f.root / 'guard.release').touch()
            holder.wait(timeout=60)
        self.addCleanup(close_holder)
        until = time.monotonic() + 15
        while not (f.root / 'guard.entered').exists() and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue((f.root / 'guard.entered').exists())
        expected = f'mq: an older runner (pid {holder.pid}) still holds the queue; it will be picked up when it exits\n'
        result = f.run(mq, 'run')
        self.assertEqual(result.stdout, expected)
        add = f.run(mq, 'add', 'b', env={'MQ_NO_AUTORUN': '0'})
        self.assertTrue(add.stdout.endswith(expected), add.stdout)
        self.assertEqual((f.state / 'run.lock/pid').read_text(), str(holder.pid) + '\n')
        self.assertFalse((f.state / 'run.lock/birth').exists())
        self.assertEqual(f.queue()[0][1], 'ready')
        close_holder()
        self.assertIn('mq done: landed 1;', f.run(mq, 'run').stdout)
        self.assertEqual(f.queue(), [])
