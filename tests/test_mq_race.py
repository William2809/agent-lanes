"""Brief 1: one repo has one landing runner, including forced adds and handoffs."""
import os
import signal
import shutil
import shlex
import subprocess
import time
import unittest

from workflow_fixture import ROOT, WorkflowFixture


class RunnerRaceTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(('b',))
        self.addCleanup(self.f.close)
        self.tools = self.f.root / 'tools'
        for directory in ('bin', 'skills/agent-workers/scripts', 'skills/wt-dev/bin',
                          'skills/remote-ci/bin', 'skills/remote-ci/remote'):
            shutil.copytree(ROOT / directory, self.tools / directory)
        if os.environ.get('MQ_RACE_SOURCE'):
            shutil.copyfile(os.environ['MQ_RACE_SOURCE'], self.tools / 'bin/mq')
        if os.environ.get('MQ_RACE_HELPER_SOURCE'):
            shutil.copyfile(os.environ['MQ_RACE_HELPER_SOURCE'], self.tools / 'bin/mq-lock.py')
        self.mq = str(self.tools / 'bin/mq')
        self.f.env['PATH'] = self.f.env['PATH'].replace(str(ROOT / 'bin'), str(self.tools / 'bin'))
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.shim('land', '''#!/usr/bin/env python3
import os, pathlib, time
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
with (root / 'land.calls').open('a') as stream:
    stream.write(str(os.getpid()) + '\\n')
while not (root / 'land.release').exists(): time.sleep(0.02)
print('LANDED fixture')
''')
        self.addCleanup(self.release_runner)

    def wait_for(self, predicate):
        # A detached runner pins and verifies its tools first; allow 60 s while other suites run.
        until = time.monotonic() + 60
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.02)
        self.fail('runner did not reach the required boundary')

    def release_runner(self):
        (self.f.root / 'land.release').touch()
        self.wait_for(lambda: not (self.f.state / 'run.lock').exists())

    def test_parallel_force_add_and_run_cannot_replay_handoff(self):
        # Brief 1. Boundary: a delayed child retains a token after another child starts.
        # Gaps: land is a gated stub; this test does not run a project's checks.
        self.f.run(self.mq, 'add', 'b', env={'MQ_NO_AUTORUN': '0', 'MQ_FORCE': '1'})
        self.wait_for(lambda: (self.f.root / 'land.calls').exists())
        token = (self.f.state / 'run.lock/handoff').read_text().strip()
        # Release defective runners too, so the old-code failure is a count assertion.
        releaser = subprocess.Popen(['python3', '-c', '''
import pathlib, sys, time
root = pathlib.Path(sys.argv[1])
until = time.monotonic() + 20
while len(list(root.glob('added.*'))) < 4 and time.monotonic() < until: time.sleep(0.02)
time.sleep(1)
(root / 'land.release').touch()
''', str(self.f.root)])
        self.addCleanup(releaser.wait, 25)
        commands = [['/bin/sh', '-c', '"$1" add b; rc=$?; touch "$2"; exit "$rc"',
                     'sh', self.mq, str(self.f.root / ('added.' + str(i)))] for i in range(4)]
        commands += [[self.mq, 'run'] for _ in range(4)]
        results = self.f.parallel(commands, env={'MQ_NO_AUTORUN': '0', 'MQ_FORCE': '1',
                                                 'MQ_LOCK_HANDOFF': token})
        self.assertTrue(all(rc == 0 for rc, _, _ in results), results)
        self.release_runner()
        self.assertEqual(len((self.f.root / 'land.calls').read_text().splitlines()), 1)
        events = (self.f.state / 'events').read_text().splitlines()
        self.assertEqual(sum('\tlanding\t' in row for row in events), 1)
        self.assertEqual(self.f.queue(), [])

    def test_crashed_runner_keeps_slot_until_land_exits(self):
        # Brief 1. Boundary: SIGKILL after land starts, then a stale directory takeover.
        # Gaps: a gated land stub holds the inherited descriptor; no remote check runs.
        self.f.run(self.mq, 'add', 'b', env={'MQ_NO_AUTORUN': '0'})
        self.wait_for(lambda: (self.f.root / 'land.calls').exists())
        lock = self.f.state / 'run.lock'
        pid = int((lock / 'pid').read_text())
        os.kill(pid, signal.SIGKILL)
        self.wait_for(lambda: not subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                                                 capture_output=True, text=True).stdout.strip())
        old = time.time() - 20
        os.utime(lock / 'handoff', (old, old))
        releaser = subprocess.Popen(['/bin/sh', '-c', '/bin/sleep 7; touch "$1"',
                                     'sh', str(self.f.root / 'land.release')])
        self.addCleanup(releaser.wait, 10)
        result = self.f.run(self.mq, 'run')
        self.assertIn('runner already active', result.stdout)
        self.assertEqual(len((self.f.root / 'land.calls').read_text().splitlines()), 1)
        (self.f.root / 'land.release').touch()
        # Retry only after the orphaned landing helper exits and releases its descriptor.
        land_pid = int((self.f.root / 'land.calls').read_text().splitlines()[0])
        self.wait_for(lambda: not subprocess.run(['ps', '-p', str(land_pid), '-o', 'command='],
                                                 capture_output=True, text=True).stdout.strip())
        self.assertIn('LANDED', self.f.run(self.mq, 'run').stdout)
        self.assertEqual(self.f.queue(), [])

    def test_delayed_copy_keeps_start_slot_without_pid_grace(self):
        # Brief 1. Boundary: startup pauses before run.lock registration for more than its grace.
        # Gaps: old lock age is advanced with utime; wall time need not pass ten seconds.
        self.f.shim('tar', '''#!/usr/bin/env python3
import os, pathlib, sys, time
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
if os.environ.get('RACE_COPY') == '1' and '-cf' in sys.argv:
    (root / 'copy.entered').touch()
    while not (root / 'copy.release').exists(): time.sleep(0.02)
os.execv('/usr/bin/tar', ['tar', *sys.argv[1:]])
''')
        self.addCleanup((self.f.root / 'copy.release').touch)
        self.f.run(self.mq, 'add', 'b', env={'MQ_NO_AUTORUN': '0', 'RACE_COPY': '1'})
        self.wait_for(lambda: (self.f.root / 'copy.entered').exists())
        handoff = self.f.state / 'run.lock/handoff'
        if handoff.exists():
            old = time.time() - 20
            os.utime(handoff, (old, old))
        (self.f.root / 'land.release').touch()
        result = self.f.run(self.mq, 'run')
        self.assertIn('runner already active', result.stdout)
        self.assertFalse((self.f.root / 'land.calls').exists())
        (self.f.root / 'copy.release').touch()
        self.wait_for(lambda: (self.f.root / 'land.calls').exists())
        self.release_runner()
        self.assertEqual(len((self.f.root / 'land.calls').read_text().splitlines()), 1)

    def test_reused_live_pid_does_not_hold_dead_runner_lock(self):
        # Review P1 PID reuse. Boundary: the guard is free; pid and handoff name a live daemon.
        # Gap: stale birth is injected; the OS need not recycle a PID during the test.
        self.f.run(self.mq, 'add', 'b')
        daemon = subprocess.Popen(['/bin/sleep', '60'])
        self.addCleanup(daemon.wait, 10)
        self.addCleanup(daemon.terminate)
        lock = self.f.state / 'run.lock'
        lock.mkdir()
        (lock / 'pid').write_text(str(daemon.pid) + '\n')
        (lock / 'birth').write_text('Thu Jan  1 00:00:00 1970\n')
        (lock / 'handoff').write_text(str(daemon.pid) + '\n')
        old = time.time() - 20
        os.utime(lock / 'handoff', (old, old))
        (self.f.root / 'land.release').touch()
        result = self.f.run(self.mq, 'run')
        # Release the injected stale directory before the generic cleanup runs on old code.
        if lock.exists():
            lock.replace(self.f.root / 'stale.lock')
        self.assertIn('mq done: landed 1;', result.stdout)
        self.assertIsNone(daemon.poll(), 'the unrelated PID must stay alive')
        self.assertEqual(self.f.queue(), [])

    def test_reused_self_pid_must_replace_stale_birth(self):
        # Review P1 PID reuse. Boundary: the new shell gets the stale owner's PID.
        # Gap: exec preserves a fixture PID; reuse and stale birth are injected.
        self.f.run(self.mq, 'add', 'b')
        (self.f.root / 'land.release').touch()
        self.f.shim('land', """#!/bin/sh
cat "$HOME/.claude/state/mq/demo/run.lock/birth" >"$FIXTURE_ROOT/owner.birth"
echo 'LANDED fixture'
""")
        result = self.f.run('sh', '-c', """
lock="$HOME/.claude/state/mq/demo/run.lock"
mkdir "$lock"
echo $$ >"$lock/pid"
echo 'Thu Jan  1 00:00:00 1970' >"$lock/birth"
exec "$1" run
""", 'sh', self.mq)
        self.assertIn('mq done: landed 1;', result.stdout)
        birth = (self.f.root / 'owner.birth').read_text().strip()
        self.assertTrue(birth)
        self.assertNotEqual(birth, 'Thu Jan  1 00:00:00 1970')

    def test_old_add_reservation_launches_new_runner(self):
        # Review P1 old handoff. Boundary: the old add parent still owns its reservation.
        # Re-review B: wait for the live parent, then use the next run trigger.
        # Gap: the launcher repeats the old add protocol; land is a stub.
        self.f.run(self.mq, 'add', 'b')
        (self.f.root / 'land.release').touch()
        first = self.f.run('sh', '-c', """
lock="$HOME/.claude/state/mq/demo/run.lock"
mkdir "$lock"
echo $$ >"$lock/pid"
echo $$ >"$lock/handoff"
MQ_COPY= MQ_LOCK_HANDOFF=$$ "$1" run &
wait $!
""", 'sh', self.mq)
        self.assertIn('an older runner', first.stdout)
        result = self.f.run(self.mq, 'run')
        if (self.f.state / 'run.lock').exists():
            (self.f.state / 'run.lock').replace(self.f.root / 'legacy.reservation')
        self.assertIn('mq done: landed 1;', result.stdout)
        self.assertEqual(self.f.queue(), [])

    def test_old_add_departed_parent_handoff_skips_grace(self):
        # Review P1 old handoff. Boundary: the old parent exited less than ten seconds ago.
        # Gap: a nonexistent PID models departure; no real installer runs.
        self.f.run(self.mq, 'add', 'b')
        lock = self.f.state / 'run.lock'
        lock.mkdir()
        (lock / 'pid').write_text('99999999\n')
        (lock / 'handoff').write_text('99999999\n')
        (self.f.root / 'land.release').touch()
        result = self.f.run(self.mq, 'run', env={'MQ_LOCK_HANDOFF': '99999999'})
        if lock.exists():
            lock.replace(self.f.root / 'legacy.reservation')
        self.assertIn('mq done: landed 1;', result.stdout)
        self.assertEqual(self.f.queue(), [])

    def old_child_overwrites_owner(self, train):
        self.f.run(self.mq, 'add', 'b')
        if train:
            self.f.add_lane('c')
            self.f.commit(self.f.lanes['c'], 'c.txt', 'c\n')
            self.f.run(self.mq, 'add', 'c')
        lock = self.f.state / 'run.lock'
        lock.mkdir()
        (lock / 'pid').write_text('99999999\n')
        (lock / 'handoff').write_text('99999999\n')
        old = time.time() - 20
        os.utime(lock / 'handoff', (old, old))
        # These are the old child's token-read and unchecked PID-write steps.
        old_child = subprocess.Popen(['sh', '-c', """
lock="$HOME/.claude/state/mq/demo/run.lock"
token=$(cat "$lock/handoff")
touch "$FIXTURE_ROOT/old.read"
while [ ! -e "$FIXTURE_ROOT/old.release" ]; do /bin/sleep 0.02; done
[ "$token" = "$MQ_LOCK_HANDOFF" ] || exit 1
echo $$ >"$lock/pid"
land b
[ "$(cat "$lock/pid" 2>/dev/null)" != "$$" ] || mv "$lock" "$FIXTURE_ROOT/old.completed"
"""], cwd=self.f.repo, env={**self.f.env, 'MQ_LOCK_HANDOFF': '99999999'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def finish(process):
            for name in ('old.release', 'new.release', 'land.release'):
                (self.f.root / name).touch()
            if process.poll() is None:
                process.communicate(timeout=15)
        self.addCleanup(finish, old_child)
        self.wait_for(lambda: (self.f.root / 'old.read').exists())
        self.f.shim('diskguard', """#!/usr/bin/env python3
import os, pathlib, time
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
(root / 'new.before.land').write_text((root / 'home/.claude/state/mq/demo/run.lock/pid').read_text())
while not (root / 'new.release').exists(): time.sleep(0.02)
print('disk: 999 GB')
""")
        runner = subprocess.Popen([self.mq, 'run'], cwd=self.f.repo,
                                  env={**self.f.env, 'MQ_TRAIN': '2' if train else '1'},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(finish, runner)
        self.wait_for(lambda: (self.f.root / 'new.before.land').exists())
        self.assertEqual((lock / 'pid').read_text(), (self.f.root / 'new.before.land').read_text())
        (self.f.root / 'old.release').touch()
        self.wait_for(lambda: (self.f.root / 'land.calls').exists())
        self.assertNotEqual((lock / 'pid').read_text(), (self.f.root / 'new.before.land').read_text())
        (self.f.root / 'new.release').touch()
        self.wait_for(lambda: runner.poll() is not None or
                      len((self.f.root / 'land.calls').read_text().splitlines()) == 2)
        (self.f.root / 'land.release').touch()
        out, err = runner.communicate(timeout=15)
        old_child.communicate(timeout=15)
        self.assertEqual(old_child.returncode, 0)
        self.assertEqual(len((self.f.root / 'land.calls').read_text().splitlines()), 1)
        self.assertNotEqual(runner.returncode, 0, err)
        self.assertIn('ownership changed', out)
        self.assertTrue(all(row[1] == 'ready' for row in self.f.queue()))
        self.assertEqual(len(self.f.queue()), 2 if train else 1)

    def test_old_child_pid_overwrite_stops_new_solo_land(self):
        # Review P1 overlap. Boundary: old token read, stale takeover, old PID write, solo land.
        # Gaps: old critical steps are a shell fixture; land is a gated stub.
        self.old_child_overwrites_owner(False)

    def test_old_child_pid_overwrite_stops_new_train_land(self):
        # Review P1 overlap. Boundary: the same overwrite before a multi-lane land.
        # Gaps: old critical steps are a shell fixture; land is a gated stub.
        self.old_child_overwrites_owner(True)

    def lost_owner_before_transient_retry(self, train):
        self.f.run(self.mq, 'add', 'b')
        if train:
            self.f.add_lane('c')
            self.f.commit(self.f.lanes['c'], 'c.txt', 'c\n')
            self.f.run(self.mq, 'add', 'c')
        self.f.shim('land', """#!/usr/bin/env python3
import os, pathlib, sys
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
count = root / 'retry.calls'
n = int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(n))
if n == 1:
    (root / 'home/.claude/state/mq/demo/run.lock/pid').write_text('99999999\\n')
    print('Error: R2 InternalError')
    print('FAILED preflight')
    sys.exit(1)
print('LANDED fixture')
""")
        result = self.f.run(self.mq, 'run', check=False,
                            env={'MQ_TRAIN': '2' if train else '1'})
        lock = self.f.state / 'run.lock'
        if lock.exists():
            lock.replace(self.f.root / 'lost.lock')
        self.assertIn('TRANSIENT', result.stdout)
        self.assertEqual((self.f.root / 'retry.calls').read_text(), '1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ownership changed', result.stdout)
        self.assertEqual(len(self.f.queue()), 2 if train else 1)
        self.assertTrue(all(row[1] == 'ready' for row in self.f.queue()))

    def test_lost_owner_stops_solo_transient_retry(self):
        # Review P1 overlap. Boundary: ownership changes after the first solo land fails.
        # Gap: a land stub writes the stale PID; the wait uses the fixture sleep shim.
        self.lost_owner_before_transient_retry(False)

    def test_lost_owner_stops_train_transient_retry(self):
        # Review P1 overlap. Boundary: ownership changes before the train retry.
        # Gap: a land stub writes the stale PID; no real storage fault runs.
        self.lost_owner_before_transient_retry(True)

    def test_live_old_handoff_waits_then_next_add_starts_runner(self):
        # Re-review B. Boundary: a live birth-less parent and a matching launch token.
        # Gap: a sleep process models the parent; land is a stub.
        self.f.run(self.mq, 'add', 'b')
        (self.f.root / 'land.release').touch()
        owner = subprocess.Popen(['/bin/sleep', '120'])
        lock = self.f.state / 'run.lock'
        lock.mkdir()
        (lock / 'pid').write_text(str(owner.pid) + '\n')
        (lock / 'handoff').write_text(str(owner.pid) + '\n')
        old = time.time() - 20
        os.utime(lock / 'handoff', (old, old))
        try:
            expected = f'mq: an older runner (pid {owner.pid}) still holds the queue; it will be picked up when it exits\n'
            result = self.f.run(self.mq, 'run', env={'MQ_LOCK_HANDOFF': str(owner.pid)})
            self.assertEqual(result.stdout, expected)
            self.assertEqual((lock / 'pid').read_text(), str(owner.pid) + '\n')
            self.assertFalse((lock / 'birth').exists())
            self.assertFalse((self.f.root / 'land.calls').exists())
            self.assertEqual(self.f.queue()[0][1], 'ready')
            add = self.f.run(self.mq, 'add', 'b', env={'MQ_NO_AUTORUN': '0', 'MQ_FORCE': '1'})
            self.assertTrue(add.stdout.endswith(expected), add.stdout)
            self.assertFalse((self.f.root / 'land.calls').exists())
            owner.terminate()
            owner.wait(timeout=60)
            # This is the later trigger required by B, not an automatic retry claim.
            self.f.run(self.mq, 'add', 'b', env={'MQ_NO_AUTORUN': '0'})
            self.wait_for(lambda: (self.f.root / 'land.calls').exists() and not lock.exists())
            self.assertEqual(len((self.f.root / 'land.calls').read_text().splitlines()), 1)
            self.assertEqual(self.f.queue(), [])
        finally:
            if owner.poll() is None: owner.terminate()
            owner.wait(timeout=60)
            if lock.exists(): lock.replace(self.f.root / 'legacy.pending')

    def test_old_add_using_new_library_waits_then_next_run_starts(self):
        # Re-review B/finding 2. Boundary: old add sources the new birth-writing library.
        # Gap: the launcher uses the old add lines; there is no real upgrade.
        self.f.run(self.mq, 'add', 'b')
        (self.f.root / 'land.release').touch()
        try:
            first = self.f.run('sh', '-c', """
. "$2"
lock="$HOME/.claude/state/mq/demo/run.lock"
ops_try_lock "$lock" || exit 1
echo $$ >"$lock/handoff"
MQ_COPY= MQ_LOCK_HANDOFF=$$ "$1" run &
wait $!
""", 'sh', self.mq, str(self.tools / 'skills/agent-workers/scripts/state.sh'))
            self.assertIn('an older runner', first.stdout)
            self.assertFalse((self.f.root / 'land.calls').exists())
            self.assertEqual(self.f.queue()[0][1], 'ready')
            result = self.f.run(self.mq, 'run')
            self.assertIn('mq done: landed 1;', result.stdout)
            self.assertEqual(self.f.queue(), [])
        finally:
            lock = self.f.state / 'run.lock'
            if lock.exists(): lock.replace(self.f.root / 'legacy.pending')

    def test_birthless_reused_owner_and_mover_show_clear_command(self):
        # Re-review B/finding 3. Boundaries: live reused owner, mover and nested reap records.
        # Gaps: missing births model legacy reuse; the OS does not wrap its PID counter.
        self.f.run(self.mq, 'add', 'b')
        (self.f.root / 'land.release').touch()
        owner = subprocess.Popen(['/bin/sleep', '120'])
        try:
            for record in ('pid', 'mover', 'reap/pid', 'reap/mover'):
                with self.subTest(record=record):
                    lock = self.f.state / 'run.lock'
                    target = lock / record
                    target.parent.mkdir(parents=True)
                    (lock / 'pid').write_text('99999999\n')
                    target.write_text(str(owner.pid) + '\n')
                    try:
                        status = self.f.run(self.mq, 'ls')
                        clear = 'mv ' + shlex.quote(str(lock)) + ' "$(mktemp -d ' + shlex.quote(str(lock) + '.stale.XXXXXX') + ')/" && mq run'
                        self.assertIn('PID may have been reused', status.stdout)
                        self.assertIn(clear, status.stdout)
                        blocked = self.f.run(self.mq, 'run')
                        expected = f'mq: an older runner (pid {owner.pid}) still holds the queue; it will be picked up when it exits\n'
                        self.assertEqual(blocked.stdout, expected)
                        self.assertEqual(target.read_text(), str(owner.pid) + '\n')
                        # The fixture PID is known to be unrelated. Exercise the exact clear command.
                        landed = self.f.run('sh', '-c', clear)
                        self.assertIn('mq done: landed 1;', landed.stdout)
                        self.assertIsNone(owner.poll())
                        self.assertEqual(self.f.queue(), [])
                        self.f.run(self.mq, 'add', 'b')
                    finally:
                        if lock.exists(): lock.replace(self.f.root / ('legacy.' + record.replace('/', '.')))
        finally:
            owner.terminate()
            owner.wait(timeout=60)

    def test_birthless_self_pid_waits_instead_of_adopting(self):
        # Re-review B. Boundary: a new process gets a legacy owner's PID, with no birth.
        # Gap: exec and a fixture PID record inject reuse.
        self.f.run(self.mq, 'add', 'b')
        (self.f.root / 'land.release').touch()
        try:
            result = self.f.run('sh', '-c', """
lock="$HOME/.claude/state/mq/demo/run.lock"
mkdir "$lock"
echo $$ >"$lock/pid"
exec "$1" run
""", 'sh', self.mq, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('an older runner', result.stdout)
            self.assertFalse((self.f.root / 'land.calls').exists())
            self.assertEqual(self.f.queue()[0][1], 'ready')
        finally:
            lock = self.f.state / 'run.lock'
            if lock.exists(): lock.replace(self.f.root / 'legacy.self')

    def test_cached_old_child_token_cannot_take_live_parent_reservation(self):
        # Re-review B/finding 1. Boundary: old token read, live parent, attempted adoption.
        # Gap: the shell executes the old critical steps; land is a gated stub.
        self.f.run(self.mq, 'add', 'b')
        parent = subprocess.Popen(['/bin/sleep', '120'])
        self.addCleanup(parent.wait, 10)
        self.addCleanup(parent.terminate)
        lock = self.f.state / 'run.lock'
        lock.mkdir()
        (lock / 'pid').write_text(str(parent.pid) + '\n')
        (lock / 'handoff').write_text(str(parent.pid) + '\n')
        old_child = subprocess.Popen(['sh', '-c', """
lock="$HOME/.claude/state/mq/demo/run.lock"
token=$(cat "$lock/handoff")
touch "$FIXTURE_ROOT/old.read"
while [ ! -e "$FIXTURE_ROOT/old.release" ]; do /bin/sleep 0.02; done
[ "$token" = "$MQ_LOCK_HANDOFF" ] || exit 1
echo $$ >"$lock/pid"
land b
: >"$HOME/.claude/state/mq/demo/queue"
[ "$(cat "$lock/pid" 2>/dev/null)" != "$$" ] || mv "$lock" "$FIXTURE_ROOT/old.completed"
"""], cwd=self.f.repo, env={**self.f.env, 'MQ_LOCK_HANDOFF': str(parent.pid)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def finish(process):
            (self.f.root / 'old.release').touch()
            (self.f.root / 'land.release').touch()
            process.communicate(timeout=15)
            if lock.exists(): lock.replace(self.f.root / 'legacy.cached')
        self.addCleanup(finish, old_child)
        self.wait_for(lambda: (self.f.root / 'old.read').exists())
        runner = subprocess.Popen([self.mq, 'run'], cwd=self.f.repo,
                                  env={**self.f.env, 'MQ_LOCK_HANDOFF': str(parent.pid)},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(finish, runner)
        self.wait_for(lambda: runner.poll() is not None or (self.f.root / 'land.calls').exists())
        (self.f.root / 'old.release').touch()
        self.wait_for(lambda: (lock / 'pid').read_text().strip() == str(old_child.pid) and
                      (self.f.root / 'land.calls').exists())
        # Let even the defective new runner finish; fail on the observed duplicate count.
        (self.f.root / 'land.release').touch()
        out, err = runner.communicate(timeout=15)
        old_child.communicate(timeout=15)
        self.assertEqual(runner.returncode, 0, err)
        self.assertEqual(old_child.returncode, 0)
        self.assertEqual(len((self.f.root / 'land.calls').read_text().splitlines()), 1)
        expected = f'mq: an older runner (pid {parent.pid}) still holds the queue; it will be picked up when it exits\n'
        self.assertEqual(out, expected)
        self.assertEqual(self.f.queue(), [])
        self.assertIn('mq done: landed 0;', self.f.run(self.mq, 'run').stdout)
