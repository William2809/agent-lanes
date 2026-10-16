"""Brief requirements: pool eligibility, ranking, bounded wake and one assignment.
Fixtures only; SSH/WOL are injected. No real check suite or device is started.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

KIT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Pool(unittest.TestCase):
    def setUp(self):
        self.pool = module('pool', KIT / 'bin/pool.py')
        self.runners = self.pool.registry('small=small-route:unit big=big-route:db:browser:wol', {})

    def metrics(self, active=0, load=0, warm=False):
        return dict(active=active, waiting=0, load=load, cores=4, pressure=1,
                    policy={'MAX_JOBS': 1}, warm=warm)

    def test_capabilities_before_load(self):
        """Req 3: the idle but ineligible host must lose to a busy DB host."""
        chosen = self.pool.select(self.runners, {'db', 'browser'},
                                  lambda r: self.metrics(active=4 if r['name'] == 'big' else 0))
        self.assertEqual(chosen['name'], 'big')

    def test_finish_load_and_cache(self):
        """Req 3: queue, load and warm cache each affect eligible ranking."""
        runners = self.pool.registry('a=a-route:unit b=b-route:unit', {})
        for a, b in [(self.metrics(active=2), self.metrics()),
                     (self.metrics(load=4), self.metrics()),
                     (self.metrics(), self.metrics(warm=True))]:
            with self.subTest(a=a, b=b):
                self.assertEqual(self.pool.select(runners, {'unit'},
                    lambda r: a if r['name'] == 'a' else b)['name'], 'b')

    def test_no_eligible(self):
        """Req 3/4: an unmatched need must fail before probing any host."""
        with self.assertRaisesRegex(self.pool.Infra, 'no eligible runner'):
            self.pool.select(self.runners, {'pdf'}, lambda r: self.fail('probe'))

    def test_fallback_is_eligible(self):
        """Req 3: outage cannot widen the capability set."""
        with self.assertRaisesRegex(self.pool.Infra, 'unreachable'):
            self.pool.select(self.runners, {'db'}, lambda r: None)

    def test_wake_deadline_and_recovery(self):
        """Req 4: bounded wake; no assignment or reservation while asleep."""
        runners = self.pool.registry('big=big-route:db:wol',
            {'CI_RUNNER_big_WOL_COMMAND': 'wake-fixture', 'CI_RUNNER_big_WAKE_TIMEOUT': '2'})
        ticks = [0.0]
        wakes = []
        def sleep(seconds):
            ticks[0] += seconds
        with self.assertRaisesRegex(self.pool.Infra, 'runner asleep'):
            self.pool.select(runners, {'db'}, lambda r: None, wake=lambda r: wakes.append(r['name']),
                             clock=lambda: ticks[0], sleep=sleep)
        self.assertEqual(wakes, ['big'])
        self.assertLessEqual(ticks[0], 2)
        ticks[0] = 0
        chosen = self.pool.select(runners, {'db'}, lambda r: self.metrics() if ticks[0] else None,
                                 wake=lambda r: None, clock=lambda: ticks[0], sleep=sleep)
        self.assertEqual(chosen['name'], 'big')

    def test_awake_eligible_avoids_wake(self):
        """Req 4/finding 2: an idle host beats an equal-duration sleeper."""
        runners = self.pool.registry('a=a-route:unit b=b-route:unit:wol',
                                    {'CI_RUNNER_b_WOL_COMMAND': 'wake-fixture'})
        self.assertEqual(self.pool.select(runners, {'unit'},
            lambda r: self.metrics() if r['name'] == 'a' else None,
            wake=lambda r: self.fail('unexpected wake'))['name'], 'a')

    def test_registry_policy_and_local_rejection(self):
        """Req 2/3: identity differs from route; disabled/local hosts cannot run."""
        self.assertEqual(self.runners[0]['route'], 'small-route')
        disabled = self.pool.registry('a=a-route:unit', {'CI_RUNNER_a_ENABLED': '0'})
        with self.assertRaises(self.pool.Infra):
            self.pool.select(disabled, {'unit'}, lambda r: self.fail('disabled probe'))
        for route in ('local', 'localhost', '127.0.0.1', 'user@localhost', '-oProxyCommand=x'):
            with self.subTest(route=route), self.assertRaises(self.pool.Infra):
                self.pool.registry('a=' + route + ':unit', {})

    def test_single_assignment(self):
        """Req 3: one job retains its original runner and generation on retry."""
        with tempfile.TemporaryDirectory() as tmp:
            first = self.pool.assign(Path(tmp), 'job', lambda: self.runners[0])
            again = self.pool.assign(Path(tmp), 'job', lambda: self.fail('reassigned'))
            self.assertEqual(first, again)
            self.assertEqual(first['runner'], 'small')
            self.assertTrue(first['generation'])
            self.assertEqual(len(list(Path(tmp).glob('*.json'))), 1)

    def test_assignment_race(self):
        """Req 3: concurrent coordinators publish exactly one generation."""
        with tempfile.TemporaryDirectory() as tmp:
            code = """import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('pool', sys.argv[1])
pool=importlib.util.module_from_spec(spec); spec.loader.exec_module(pool)
row=pool.assign(pathlib.Path(sys.argv[2]), 'race', lambda: dict(name=sys.argv[3],route='route'))
print(row['runner'],row['generation'])
"""
            calls = [subprocess.Popen([sys.executable, '-c', code, str(KIT / 'bin/pool.py'), tmp, name],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ('a', 'b', 'c', 'd')]
            results = [call.communicate(timeout=60) for call in calls]
            self.assertTrue(all(call.returncode == 0 for call in calls), results)
            self.assertEqual(len({stdout for stdout, _ in results}), 1)

    def test_cli_wake_timeout_exit_three(self):
        """Req 4: an offline SSH fixture reaches exit 3 and stays unassigned."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ssh = root / 'ssh'
            ssh.write_text('#!/bin/sh\nexit 255\n')
            ssh.chmod(0o755)
            result = subprocess.run([sys.executable, str(KIT / 'bin/pool.py'),
                '--project', 'fixture', '--job', 'sleeping', '--state', str(root / 'state')],
                env=dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'],
                    CI_RUNNERS='a=a-route:db:wol', REMOTE_CI_NEEDS='db',
                    CI_RUNNER_a_WOL_COMMAND='true', CI_RUNNER_a_WAKE_TIMEOUT='1'),
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 3)
            self.assertIn('runner asleep', result.stderr)
            row = json.loads((root / 'state/sleeping.json').read_text())
            self.assertEqual(row['state'], 'pending')
            self.assertNotIn('runner', row)

    def test_cli_lookup_uses_recorded_runner(self):
        """cipool2 gap 1: follow-ups go to the runner that ran the sha, without probing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ssh = root / 'ssh'
            ssh.write_text('#!/bin/sh\necho "$@" >>"$0.log"\n'
                           'printf \'%s\\n\' \'{"active":0,"waiting":0,"load":0,"cores":4}\'\n')
            ssh.chmod(0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'],
                       CI_RUNNERS='a=a-route:unit b=b-route', REMOTE_CI_NEEDS='unit')
            def pool(*args):
                return subprocess.run([sys.executable, str(KIT / 'bin/pool.py'), '--project', 'fixture',
                    '--state', str(root / 'state'), *args], env=env, capture_output=True, text=True, timeout=20)
            sha = 'ab' * 20
            self.assertEqual(pool('--runner', 'b', '--record', sha).returncode, 0)
            self.assertEqual(pool('--record', sha).returncode, 3)
            self.assertEqual(pool('--lookup', sha[:8]).stdout.split(), ['b', 'b-route', '-', 'ran-sha'])
            self.assertEqual(pool('--lookup', 'latest').stdout.split(), ['b', 'b-route', '-', 'last-check'])
            self.assertFalse((root / 'ssh.log').exists())
            self.assertEqual(pool('--lookup', '1234567').stdout.split(), ['a', 'a-route', '-', 'scored'])
            self.assertEqual(pool('--runner', 'a', '--lookup', sha).stdout.split(), ['a', 'a-route', '-', 'named'])
            self.assertTrue((root / 'ssh.log').exists())

    def test_cli_no_eligible_exit_three(self):
        """Req 4: public infrastructure code is 3, with no transport invoked."""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(KIT / 'bin/pool.py'),
                '--project', 'fixture', '--job', 'job', '--state', tmp],
                env=dict(os.environ, CI_RUNNERS='a=a-route:unit', REMOTE_CI_NEEDS='db'),
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 3)
            self.assertIn('no eligible runner', result.stderr)


class Client(unittest.TestCase):
    def test_client_registry_and_no_local_execution(self):
        """Req 2/3/4: shell config reaches pool; unmet needs never run locally."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            (root / '.remote-ci.conf').write_text("REMOTE_CI_NAME=fixture\nREMOTE_CI_NEEDS=db\n")
            config = root / 'config'
            config.write_text('CI_RUNNERS="small=small-route:unit big=big-route:db"\n')
            ssh = root / 'ssh'
            ssh.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"active":0,"waiting":0,"load":0,"cores":4}\'\n')
            ssh.chmod(0o755)
            env = dict(os.environ, HOME=tmp, AGENT_LANES_CONFIG=str(root / 'absent'),
                       PATH=tmp + os.pathsep + os.environ['PATH'])
            # Public export merges the config names; the fixture config must win.
            env['AGENT_LANES_CONFIG'] = str(config)
            call = ['/bin/bash', str(KIT / 'bin/remote-ci')]
            result = subprocess.run(call + ['info'], env=env, cwd=tmp,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('route=pool big', result.stdout)
            (root / '.remote-ci.conf').write_text("REMOTE_CI_NAME=fixture\nREMOTE_CI_NEEDS=pdf\n")
            result = subprocess.run(call + ['check', '--run', 'touch local-execution'], env=env,
                                    cwd=tmp, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertFalse((root / 'local-execution').exists())

    def client(self, tmp, conf='REMOTE_CI_NAME=fixture\n', runners='a=a-route:unit b=b-route:unit'):
        """Git repo, pool config and an ssh stub that logs routes and prints an idle status."""
        root = Path(tmp)
        (root / '.remote-ci.conf').write_text(conf)
        (root / 'config').write_text('CI_RUNNERS="%s"\n' % runners)
        ssh = root / 'ssh'
        ssh.write_text('#!/bin/sh\necho "$@" >>"$0.log"\ncat >/dev/null\n'
                       'printf \'%s\\n\' \'{"active":0,"waiting":0,"load":0,"cores":4}\'\n')
        ssh.chmod(0o755)
        env = dict(os.environ, HOME=tmp, AGENT_LANES_CONFIG=str(root / 'absent'),
                   PATH=tmp + os.pathsep + os.environ['PATH'],
                   GIT_AUTHOR_NAME='F', GIT_AUTHOR_EMAIL='user@example.invalid',
                   GIT_COMMITTER_NAME='F', GIT_COMMITTER_EMAIL='user@example.invalid')
        # Separate assignment survives the public config-name substitution.
        env['AGENT_LANES_CONFIG'] = str(root / 'config')
        for name in ('REMOTE_CI_RUNNER', 'REMOTE_CI_NEEDS', 'CI_RUNNERS'):
            env.pop(name, None)
        subprocess.run(['git', 'init', '-q', tmp], check=True)
        subprocess.run(['git', '-C', tmp, 'add', '.remote-ci.conf'], check=True)
        subprocess.run(['git', '-C', tmp, 'commit', '-qm', 'fixture'], check=True, env=env)
        run = lambda *args, **extra: subprocess.run(['/bin/bash', str(KIT / 'bin/remote-ci'), *args],
            env=dict(env, **extra), cwd=tmp, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=20)
        return root, run

    def test_followups_reach_the_runner_that_ran_the_sha(self):
        """cipool2 gap 1: check records its runner; fail SHA and info use the record."""
        pool = module('pool', KIT / 'bin/pool.py')
        with tempfile.TemporaryDirectory() as tmp:
            # Only a can run checks (need x); b holds a recorded run.
            root, run = self.client(tmp, 'REMOTE_CI_NAME=fixture\nREMOTE_CI_NEEDS=x\n', 'a=a-route:x b=b-route')
            state = root / '.local/state/remote-ci/pool'
            # The stub is not a runner kit: the check stops after assignment (74), never locally.
            self.assertEqual(run('check', '--run', 'true').returncode, 74)
            self.assertEqual(pool.recorded(state, 'fixture', 'latest'), 'a')
            sha = json.loads((state / 'projects/fixture.json').read_text())['runs'][0][0]
            pool.record(state, 'fixture', 'f' * 40, 'b')
            (root / 'ssh.log').write_text('')
            run('fail', sha[:8])
            routes = (root / 'ssh.log').read_text()
            self.assertIn('a-route', routes)
            self.assertNotIn('b-route', routes)
            result = run('info')
            self.assertIn('route=pool b (last-check)', result.stdout, result.stderr)

    def test_environment_needs_add_to_conf_needs(self):
        """cipool2 gap 4: conf db plus environment browser selects the runner with both."""
        with tempfile.TemporaryDirectory() as tmp:
            _, run = self.client(tmp, 'REMOTE_CI_NAME=fixture\nREMOTE_CI_NEEDS=db\n',
                                 'a=a-route:db b=b-route:db:browser c=c-route:browser')
            self.assertIn('route=pool b ', run('info', REMOTE_CI_NEEDS='browser').stdout)
            self.assertNotEqual(run('info', REMOTE_CI_NEEDS='pdf').returncode, 0)

    def test_admin_needs_a_named_runner(self):
        """cipool2 gap 3: freeze/thaw/install-runner refuse a scored runner (exit 2, no ssh)."""
        with tempfile.TemporaryDirectory() as tmp:
            # Host-level: b lacks the project's browser need and is still frozen when named.
            root, run = self.client(tmp, 'REMOTE_CI_NAME=fixture\nREMOTE_CI_NEEDS=browser\n',
                                    'a=a-route:browser b=b-route')
            for command in ('freeze', 'thaw', 'install-runner'):
                with self.subTest(command=command):
                    result = run(command)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn('REMOTE_CI_RUNNER=<name> remote-ci ' + command, result.stderr)
                    self.assertFalse((root / 'ssh.log').exists())
            result = run('freeze', REMOTE_CI_RUNNER='b')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            routes = (root / 'ssh.log').read_text()
            self.assertIn('b-route', routes)
            self.assertNotIn('a-route', routes)


class Adapters(unittest.TestCase):
    def setUp(self):
        self.platform = module('platform_info', KIT / 'remote/platform_info.py')

    def test_node_matrix(self):
        """Req 1: OS and CPU select the Node artifact; unknown targets fail."""
        for system, arch, expected in [('Darwin', 'arm64', 'darwin-arm64'),
            ('Darwin', 'x86_64', 'darwin-x64'), ('Linux', 'aarch64', 'linux-arm64'),
            ('Linux', 'x86_64', 'linux-x64')]:
            self.assertEqual(self.platform.node_target(system, arch), expected)
        with self.assertRaises(ValueError):
            self.platform.node_target('Windows', 'x86')

    def test_linux_pressure_and_fallback(self):
        """Req 1: PSI prevents normal admission; absent PSI uses available RAM."""
        mem = 'MemTotal: 1000000 kB\nMemAvailable: 500000 kB\n'
        self.assertEqual(self.platform.linux_memory(mem, 'some avg10=0.00\nfull avg10=0.00\n'), (488, 1))
        self.assertEqual(self.platform.linux_memory(mem, 'some avg10=12.00\nfull avg10=0.00\n')[1], 2)
        self.assertEqual(self.platform.linux_memory(mem, 'some avg10=0.00\nfull avg10=6.00\n')[1], 4)
        self.assertEqual(self.platform.linux_memory(mem, None)[1], 1)
        self.assertEqual(self.platform.linux_memory(mem.replace('500000', '40000'), None)[1], 4)

    def test_lock_exclusion_and_stale_pid(self):
        """Req 1: portable PID lock excludes a live owner and reclaims a dead one."""
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'lock'
            self.assertTrue(self.platform.pid_lock(lock, os.getpid()))
            self.assertFalse(self.platform.pid_lock(lock, os.getpid()))
            child = subprocess.Popen([sys.executable, '-c', 'pass'])
            child.wait()
            lock.write_text(str(child.pid))
            self.assertTrue(self.platform.pid_lock(lock, os.getpid()))
            self.assertEqual(lock.read_text().strip(), str(os.getpid()))

    def test_duplicate_delivery_fence(self):
        """Req 3: duplicate delivery cannot enter execution a second time."""
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, str(KIT / 'remote/platform_info.py'), 'fence',
                       str(Path(tmp) / 'started'), 'generation']
            self.assertEqual(subprocess.run(command).returncode, 0)
            self.assertEqual(subprocess.run(command).returncode, 3)

    def test_native_sampler(self):
        """Req 1: real host sampling returns bounded pressure and memory units."""
        def output(args):
            return subprocess.check_output(args, text=True)
        metrics = self.platform.sample(output)
        self.assertGreater(metrics['cores'], 0)
        self.assertGreater(metrics['available_mb'], 0)
        self.assertIn(metrics['pressure'], (1, 2, 4))

    def test_macos_reclaimable_memory(self):
        """Req 1: preserve master's kernel estimate and page-count floor."""
        # Fixture: 1024 MiB total and 512 reclaimable 4-KiB pages (2 MiB).
        # The expected values follow these inputs, not the adapter calculation.
        values = {'/usr/bin/vm_stat': 'Mach Virtual Memory Statistics: (page size of 4096 bytes)\n'
                  'Pages free: 256.\nPages inactive: 128.\nPages speculative: 128.\n',
                  'hw.memsize': '1073741824', 'kern.memorystatus_vm_pressure_level': '1'}
        for level, expected in [('75', 768), ('0', 2), ('150', 1024), ('-1', 2)]:
            with self.subTest(level=level), mock.patch.object(sys, 'platform', 'darwin'):
                values['kern.memorystatus_level'] = level
                result = self.platform.sample(lambda args: values[args[-1]])
                self.assertEqual(result['available_mb'], expected)

    def test_shell_decode_and_time(self):
        """Req 1: decode real bytes and execute the native time adapter."""
        result = subprocess.run(['/bin/bash', '-c',
            '. "$1"; printf aGVsbG8= | platform_decode; "${platform_time[@]}" true',
            '_', str(KIT / 'remote/platform.sh')], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'hello')

    def test_postgres_path_before_install(self):
        """Req 1/5: a missing PostgreSQL package still uses the host tool prefix."""
        # The installed package must not be the oracle for its future location.
        if sys.platform == 'darwin':
            prefix = subprocess.check_output(['brew', '--prefix'], text=True).strip()
            expected = prefix + '/opt/postgresql@999/bin'
        else:
            expected = '/usr/lib/postgresql/999/bin'
        result = subprocess.run(['/bin/bash', '-c', '. "$1"; platform_pgbin 999',
                                 '_', str(KIT / 'remote/platform.sh')],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), expected)


if __name__ == '__main__':
    unittest.main()
