"""V1 runner infrastructure commands have deadlines and tracked descendant cleanup."""
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import signal
import time
import unittest

import test_runner as fixtures


class V1StallBounds(unittest.TestCase):
    configure = fixtures.Runner.configure
    git = fixtures.Runner.git
    start = fixtures.Runner.start
    stop_runs = fixtures.Runner.stop_runs
    wait_for = fixtures.Runner.wait_for
    log = fixtures.Runner.log
    assert_result = fixtures.Runner.assert_result

    def setUp(self):
        # Compose the fixture instead of inheriting its large legacy test suite.
        fixtures.Runner.setUp(self)

    def short_deadlines(self):
        self.configure()
        with (self.dir / 'conf').open('a') as stream:
            stream.write('REMOTE_CI_TIMEOUT=2\nREMOTE_CI_WAIT_TIMEOUT=20\n')

    def assert_gone(self, marker):
        pid = int(marker.read_text())
        try:
            os.kill(pid, 0)
        except OSError as error:
            self.assertEqual(error.errno, errno.ESRCH)
        else:
            self.fail('tracked descendant survived infrastructure deadline: %s' % pid)

    def database_stall(self, tool, cleanup=False):
        self.short_deadlines()
        with (self.dir / 'conf').open('a') as stream:
            stream.write('REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\nREMOTE_CI_DB_CLEAN=fixture_%\n')
            if cleanup:
                stream.write('REMOTE_CI_WAIT_TIMEOUT=4\n')
        marker = self.home / ('stuck-' + tool)
        condition = '[ ! -f "$HOME/checks" ] || ' if cleanup else ''
        (self.dir / 'env.local').write_text(
            'stall_tool() { sleep 30 & child=$!; echo "$child" >"$HOME/stuck-%s"; wait "$child"; }\n'
            'export -f stall_tool\n'
            'database_url() { echo fixture; }\n'
            'pg_ctl() { %s return 0; }\n'
            'psql() { cat >/dev/null; %s return 0; }\n'
            'dropdb() { %s return 0; }\n'
            'createdb() { %s return 0; }\n' % (
                tool,
                'stall_tool;' if tool == 'pg_ctl' else '',
                'stall_tool;' if tool == 'psql' else '',
                condition + 'stall_tool;' if tool == 'dropdb' else '',
                'stall_tool;' if tool == 'createdb' else ''))
        command = 'touch "$HOME/checks"; sleep 5' if cleanup else 'touch "$HOME/candidate-ran"'
        retains_cleanup = cleanup or tool == 'psql'
        started = time.monotonic()
        run = 'stuck-' + tool
        self.assert_result(self.start(run, force=True, command=command), run, 76 if retains_cleanup else 74)
        self.assertLess(time.monotonic() - started, 12)
        self.assertTrue(marker.exists())
        self.assert_gone(marker)
        if not cleanup:
            self.assertFalse((self.home / 'candidate-ran').exists())
        if retains_cleanup:
            self.assertTrue((self.dir / 'results' / (run + '.cleanup')).exists())
            self.assertEqual((self.dir / 'slots/1.run').read_text().strip(), run)

    def test_stuck_git_fetch_is_infrastructure_and_reaped(self):
        self.short_deadlines()
        tools = self.ci / 'node/stall/bin'
        tools.mkdir(parents=True)
        marker = self.home / 'stuck-git'
        git = tools / 'git'
        git.write_text('#!/bin/bash\nif [ "$1" = fetch ]; then sleep 30 & child=$!; echo "$child" >"$HOME/stuck-git"; wait "$child"; fi\nexec /usr/bin/git "$@"\n')
        git.chmod(git.stat().st_mode | stat.S_IXUSR)
        with (self.dir / 'conf').open('a') as stream:
            stream.write('REMOTE_CI_NODE=stall\n')
        started = time.monotonic()
        self.assert_result(self.start('stuck-git', force=True, command='touch "$HOME/candidate-ran"'),
                           'stuck-git', 74)
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse((self.home / 'candidate-ran').exists())
        self.assert_gone(marker)

    def test_stuck_pg_ctl_is_infrastructure_and_reaped(self):
        self.database_stall('pg_ctl')

    def test_successful_pg_start_keeps_intended_service(self):
        self.short_deadlines()
        with (self.dir / 'conf').open('a') as stream:
            stream.write('REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\n')
        (self.dir / 'env.local').write_text(
            'database_url() { echo fixture; }\n'
            "pg_ctl() { case \" $* \" in *' status '*) return 1 ;; esac; "
            "python3 -c 'import subprocess,pathlib,os; p=subprocess.Popen([\"sleep\",\"30\"], start_new_session=True); pathlib.Path(os.environ[\"HOME\"],\"pg-service\").write_text(str(p.pid))'; }\n"
            'psql() { cat >/dev/null; return 0; }\n'
            'dropdb() { return 0; }\n'
            'createdb() { return 0; }\n')
        self.assert_result(self.start('pg-service', force=True, command='true'), 'pg-service', 0)
        marker = self.home / 'pg-service'
        self.assertTrue(marker.exists())
        pid = int(marker.read_text())
        os.kill(pid, 0)
        os.kill(pid, signal.SIGTERM)

    def test_stuck_cache_copy_is_infrastructure_and_reaped(self):
        self.short_deadlines()
        tools = self.ci / 'node/stall/bin'
        tools.mkdir(parents=True)
        marker = self.home / 'stuck-cp'
        cp = tools / 'cp'
        cp.write_text('#!/bin/bash\nsleep 30 & child=$!; echo "$child" >"$HOME/stuck-cp"; wait "$child"\n')
        cp.chmod(cp.stat().st_mode | stat.S_IXUSR)
        node = tools / 'node'
        node.write_text('#!/bin/bash\n[ "$1" = --version ] && { echo v20.0.0; exit 0; }\nexit 97\n')
        node.chmod(node.stat().st_mode | stat.S_IXUSR)
        with (self.dir / 'conf').open('a') as stream:
            stream.write("REMOTE_CI_NODE=stall\nREMOTE_CI_INSTALL='mkdir -p node_modules; touch node_modules/ready'\n")
        started = time.monotonic()
        self.assert_result(self.start('stuck-cp', force=True, command='touch "$HOME/candidate-ran"'),
                           'stuck-cp', 74)
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse((self.home / 'candidate-ran').exists())
        self.assertTrue(marker.exists())
        self.assert_gone(marker)

    def test_stuck_psql_is_infrastructure_and_reaped(self):
        self.database_stall('psql')

    def test_stuck_createdb_is_infrastructure_and_reaped(self):
        self.database_stall('createdb')

    def test_stuck_cleanup_dropdb_retains_ownership(self):
        # Candidate execution consumes the total wait. Cleanup still gets its
        # independent budget and records uncertainty instead of freeing the slot.
        self.database_stall('dropdb', cleanup=True)

    def test_killed_supervisor_retains_host_grant_until_recovery(self):
        ownership = self.home / 'orphan.infra-tree'
        descendant = self.home / 'orphan-child'
        token = 'fixture-orphan-dead'
        state = self.home / '.local/state/agent-ci'
        state.mkdir(parents=True)
        (state / 'queue.json').write_text(json.dumps({'sequence': 1, 'jobs': {token: {
            'project': 'fixture', 'run_id': 'orphan', 'owner': 999999,
            'start': 1, 'workers': 2, 'memory_mb': 1024,
        }}}) + '\n')
        proc = subprocess.Popen([
            'python3', str(self.bin / 'bounded_process.py'), '--timeout', '2',
            '--phase', 'checkout', '--ownership', str(ownership), '--token', token,
            '--', '/bin/bash', '-c',
            'sleep 30 & child=$!; echo "$child" >"$HOME/orphan-child"; wait "$child"',
        ], env=self.env)
        self.procs.append(proc)
        self.wait_for(lambda: ownership.exists() and descendant.exists())
        proc.kill()
        proc.wait(timeout=5)
        retained = subprocess.run([
            'python3', str(self.bin / 'resources.py'), 'release', '--token', token,
        ], env=self.env, timeout=10)
        self.assertEqual(retained.returncode, 76)
        queue = json.loads((state / 'queue.json').read_text())['jobs'][token]
        self.assertEqual(queue['infra_tree_marker'], str(ownership))
        self.assertEqual(queue['workers'], 2)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                os.kill(int(descendant.read_text()), 0)
            except OSError as error:
                if error.errno == errno.ESRCH:
                    break
            time.sleep(.05)
        self.assert_gone(descendant)
        recovered = subprocess.run([
            'python3', str(self.bin / 'bounded_process.py'), '--reap-marker', str(ownership),
            '--token', token,
        ], env=self.env, timeout=10)
        self.assertEqual(recovered.returncode, 0)
        self.assertFalse(ownership.exists())
        released = subprocess.run([
            'python3', str(self.bin / 'resources.py'), 'release', '--token', token,
        ], env=self.env, timeout=10)
        self.assertEqual(released.returncode, 0)
        self.assertNotIn(token, json.loads((state / 'queue.json').read_text())['jobs'])

    def test_silent_valid_command_below_deadline_passes(self):
        self.short_deadlines()
        started = time.monotonic()
        self.assert_result(self.start('quiet', force=True, command='sleep 1'), 'quiet', 0)
        self.assertGreaterEqual(time.monotonic() - started, 1)


if __name__ == '__main__':
    unittest.main()
