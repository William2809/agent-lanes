"""Host kit, protocol floor and freeze. Local route (REMOTE_CI_HOST=local) with a temporary HOME; real Git, no SSH or suite."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from fixture_tools import isolate_corepack

sys.dont_write_bytecode = True
KIT = Path(__file__).resolve().parent
FIXTURE_ID = ['-c', 'user.name=Fixture', '-c', 'user.email=user@example.invalid']


class HostKit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='remote-ci-hostkit-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / 'source'
        self.root.mkdir()
        self.kit = self.home / 'kit'
        shutil.copytree(KIT / 'remote', self.kit / 'remote',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        isolate_corepack(self.kit / 'remote', self.home)
        shutil.copytree(KIT / 'bin', self.kit / 'bin')
        self.client = self.kit / 'bin/remote-ci'
        config = self.home / 'config'
        config.write_text('REMOTE_CI_HOST=local\n')
        (self.root / '.remote-ci.conf').write_text('REMOTE_CI_NAME=fixture\nREMOTE_CI_WAIT_TIMEOUT=600\n')
        self.project = self.home / 'ci/projects/fixture'
        for name in ('logs', 'results', 'runs'):
            (self.project / name).mkdir(parents=True)
        (self.project / '.ready').touch()
        stubs = self.home / 'stubs'
        stubs.mkdir()
        # Push records its arguments and stops the check before any run starts.
        (stubs / 'git').write_text(f'#!{sys.executable}\n' + '''import os, sys
args = sys.argv[1:]
if args[:1] == ['push']:
    open(os.environ['HOME'] + '/push-args', 'w').write('\\n'.join(args))
    if os.environ.get('FIXTURE_PUSH') == 'freeze':  # an operator freezes between push and launch
        open(os.environ['HOME'] + '/ci/freeze', 'w').write('late freeze\\n')
        sys.exit(0)
    sys.exit(98)
os.execv('/usr/bin/git', ['git', *args])
''')
        # The first wait ends a freeze, like an operator running `remote-ci thaw`.
        (stubs / 'sleep').write_text('#!/bin/bash\necho "$1" >>"$HOME/slept"\n'
                                     '[ -n "${FIXTURE_THAW:-}" ] && [ -f "$HOME/ci/freeze" ] && mv "$HOME/ci/freeze" "$HOME/thawed"\n'
                                     'exec /bin/sleep 0.05\n')
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), AGENT_LANES_CONFIG=str(config),
                        PYTHONDONTWRITEBYTECODE='1',
                        PATH=str(stubs) + os.pathsep + os.environ['PATH'])
        self.git(self.root, 'init', '-q')
        self.git(self.root, 'add', '.remote-ci.conf')
        self.git(self.root, *FIXTURE_ID, 'commit', '-qm', 'fixture')

    def git(self, cwd, *args, check=True):
        return subprocess.run(['/usr/bin/git', *map(str, args)], cwd=cwd, capture_output=True,
                              text=True, check=check)

    def remote_ci(self, *args):
        return subprocess.run(['/bin/bash', str(self.client), *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def host_kit(self):
        """A host kit whose run.sh differs from the client's, as after an install from a newer master."""
        kit = self.home / 'ci/kits/abcdef12-1'
        shutil.copytree(KIT / 'remote', kit, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        isolate_corepack(kit, self.home)
        (kit / 'run.sh').write_text('#!/bin/bash\n# host kit run.sh\n')
        (kit / 'VERSION').write_text('abcdef12\n')
        (kit / 'PROTOCOL').write_text('2\n')
        (self.home / 'ci/kit').symlink_to('kits/abcdef12-1')
        return kit

    def test_check_copies_host_kit_and_sends_protocol(self):
        # Requirement: with a host kit, a run executes the host's scripts, not the client's, and says its protocol.
        self.host_kit()
        call = self.remote_ci('check')
        self.assertEqual(call.returncode, 74, call.stdout + call.stderr)  # the push stub stops it
        runs = list(self.project.glob('runs/*/bin/run.sh'))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].read_text(), '#!/bin/bash\n# host kit run.sh\n')
        self.assertFalse((self.home / 'ci/bin/run.sh').exists(), 'a host-kit client must not overwrite ~/ci/bin')
        push = (self.home / 'push-args').read_text().split('\n')
        self.assertIn('-o', push)
        self.assertEqual(push[push.index('-o') + 1], 'remote-ci=2')

    def test_pass_key_names_the_host_kit(self):
        # Requirement: a cached pass credits the host kit that ran it, not the client's scripts.
        self.host_kit()
        from test_cache import ReviewClient
        sha = self.git(self.root, 'rev-parse', 'HEAD').stdout.strip()
        tree = self.git(self.root, 'rev-parse', 'HEAD^{tree}').stdout.strip()
        conf = self.git(self.root, 'hash-object', '.remote-ci.conf').stdout.strip()[:8]
        (self.project / f'results/tree-{tree}-{conf}-abcdef12.pass').write_text(sha + '\n')
        call = self.remote_ci('check')
        self.assertEqual(call.returncode, 74, call.stdout + call.stderr)
        key = ReviewClient.identity(self)
        (self.project / ('results/tree-' + key + '.pass')).write_text(sha + '\n')
        call = self.remote_ci('check')
        self.assertEqual(call.returncode, 0, call.stdout + call.stderr)
        self.assertIn('already passed', call.stdout)
        # A client script change cannot alter the host kit's execution identity.
        with (self.kit / 'remote/run.sh').open('a') as stream:
            stream.write('\n# client only\n')
        self.assertEqual(self.remote_ci('check').returncode, 0)

    def test_check_without_host_kit_ships_its_own_scripts(self):
        # Requirement: a runner without a host kit keeps the old behaviour (bootstrap and other runners).
        call = self.remote_ci('check')
        self.assertEqual(call.returncode, 74, call.stdout + call.stderr)
        runs = list(self.project.glob('runs/*/bin/run.sh'))
        self.assertEqual(runs[0].read_bytes(), (KIT / 'remote/run.sh').read_bytes())
        self.assertNotIn('-o', (self.home / 'push-args').read_text().split('\n'))

    def test_frozen_runner_holds_new_checks_until_thaw(self):
        # Requirement: during a freeze a new check waits (one line), then continues after the thaw.
        self.host_kit()
        (self.home / 'ci/freeze').write_text('runner fix\n')
        self.env['FIXTURE_THAW'] = '1'
        call = self.remote_ci('check')
        self.assertEqual(call.returncode, 74, call.stdout + call.stderr)
        self.assertEqual(call.stdout.count('runner frozen for maintenance (runner fix'), 1)
        self.assertTrue((self.home / 'thawed').exists())
        self.assertTrue((self.home / 'push-args').exists(), 'the check must continue after the thaw')

    def test_freeze_after_push_stops_the_launch(self):
        # Requirement: no run starts during a freeze, even when the client probed before it.
        self.host_kit()
        self.env['FIXTURE_PUSH'] = 'freeze'
        call = self.remote_ci('check')
        self.assertEqual(call.returncode, 74, call.stdout + call.stderr)
        self.assertIn('runner frozen after submission', call.stdout)
        self.assertEqual(list(self.project.glob('logs/*.log')), [])

    def test_install_wait_has_a_deadline_and_stays_frozen(self):
        self.git(self.kit, 'init', '-q', '-b', 'master')
        self.git(self.kit, 'add', '.')
        self.git(self.kit, *FIXTURE_ID, 'commit', '-qm', 'kit')
        fake_run = self.project / 'runs/live/bin/run.sh'
        fake_run.parent.mkdir(parents=True)
        fake_run.write_text('/bin/sleep 30\n')
        live = subprocess.Popen(['bash', str(fake_run)])
        self.addCleanup(live.wait)
        self.addCleanup(live.terminate)
        self.env['REMOTE_CI_INSTALL_WAIT'] = '0'
        call = self.remote_ci('install-runner')
        self.assertEqual(call.returncode, 75, call.stdout + call.stderr)
        self.assertIn('nothing installed', call.stderr)
        self.assertFalse((self.home / 'ci/kit').exists())
        self.assertTrue((self.home / 'ci/freeze').exists(), 'the operator decides when to thaw')

    def test_freeze_and_thaw_commands(self):
        self.assertEqual(self.remote_ci('freeze', 'runner', 'fix').returncode, 0)
        self.assertTrue((self.home / 'ci/freeze').read_text().startswith('runner fix ('))
        self.assertEqual(self.remote_ci('thaw').returncode, 0)
        self.assertFalse((self.home / 'ci/freeze').exists())
        self.assertTrue((self.home / 'ci/logs/freeze.last').exists())

    def make_runner_repo(self):
        repo = self.project / 'repo.git'
        self.git(self.home, 'init', '-q', '--bare', repo)
        return repo

    def test_install_runner_from_master(self):
        # Requirement: install makes a clean master kit the host kit, refreshes ~/ci/bin and every repo's floor,
        # and waits for live runs before switching.
        repo = self.make_runner_repo()
        self.git(self.kit, 'init', '-q', '-b', 'master')
        self.git(self.kit, 'add', '.')
        self.git(self.kit, *FIXTURE_ID, 'commit', '-qm', 'kit')
        fake_run = self.project / 'runs/live/bin/run.sh'
        fake_run.parent.mkdir(parents=True)
        # Hold the run until the installer observes it. A one-second sleep can
        # end during kit staging on a loaded host, before the first live scan.
        release = self.home / 'release-live'
        fake_run.write_text('while [ ! -f "$1" ]; do /bin/sleep 0.05; done\n')
        live = subprocess.Popen(['bash', str(fake_run), str(release)])
        installer = None
        log = self.home / 'install.log'
        waiting = 'waiting for 1 running or queued check'
        try:
            with log.open('w') as output:
                installer = subprocess.Popen(['/bin/bash', str(self.client), 'install-runner'],
                    cwd=self.root, env=self.env, stdout=output, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 60
                while waiting not in log.read_text() and installer.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIn(waiting, log.read_text())
                self.assertIsNone(live.poll(), 'fixture must stay alive until the wait is observed')
                self.assertIsNone(installer.poll(), 'install must not finish before the run is released')
                self.assertFalse((self.home / 'ci/kit').exists(), 'the live run must block kit publication')
                release.touch()
                code = installer.wait(timeout=120)
            call = subprocess.CompletedProcess(installer.args, code, log.read_text(), '')
            self.assertEqual(call.returncode, 0, call.stdout + call.stderr)
            self.assertIsNotNone(live.poll(), 'install must wait for the live run to end')
            self.assertIn('waiting for 1 running or queued check', call.stdout)
        finally:
            release.touch()
            if installer is not None:
                if installer.poll() is None:
                    installer.terminate()
                installer.wait(timeout=60)
            live.wait(timeout=60)
        host = self.home / 'ci/kit'
        self.assertTrue(host.is_symlink())
        self.assertEqual((host / 'PROTOCOL').read_text().strip(), '2')
        self.assertEqual((host / 'run.sh').read_bytes(), (KIT / 'remote/run.sh').read_bytes())
        self.assertEqual((self.home / 'ci/bin/run.sh').read_bytes(), (KIT / 'remote/run.sh').read_bytes())
        self.assertEqual((repo / 'hooks/pre-receive').read_bytes(), (KIT / 'remote/pre-receive.sh').read_bytes())
        self.assertEqual(self.git(repo, 'config', 'receive.advertisePushOptions').stdout.strip(), 'true')
        self.assertFalse((self.home / 'ci/freeze').exists(), 'install thaws a freeze it created')
        # A check after the install runs the installed kit and passes the floor's protocol.
        self.assertEqual(self.remote_ci('check').returncode, 74)
        run = next(p for p in self.project.glob('runs/*/bin/run.sh') if 'live' not in str(p))
        self.assertEqual(run.read_bytes(), (KIT / 'remote/run.sh').read_bytes())
        self.assertIn('remote-ci=2', (self.home / 'push-args').read_text().split('\n'))

    def test_install_runner_refuses_a_branch_checkout(self):
        self.git(self.kit, 'init', '-q', '-b', 'lane')
        self.git(self.kit, 'add', '.')
        self.git(self.kit, *FIXTURE_ID, 'commit', '-qm', 'kit')
        call = self.remote_ci('install-runner')
        self.assertEqual(call.returncode, 2)
        self.assertIn('only from a master checkout', call.stderr)
        self.assertFalse((self.home / 'ci/kit').exists())
        self.assertFalse((self.home / 'ci/freeze').exists())


class ProtocolFloor(unittest.TestCase):
    """The runner repo hook, with real git push options."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='remote-ci-floor-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo = self.home / 'repo.git'
        self.env = dict(os.environ, HOME=str(self.home))
        subprocess.run(['/usr/bin/git', 'init', '-q', '--bare', str(self.repo)], check=True)
        subprocess.run(['/usr/bin/git', '-C', str(self.repo), 'config', 'receive.advertisePushOptions', 'true'],
                       check=True)
        shutil.copyfile(KIT / 'remote/pre-receive.sh', self.repo / 'hooks/pre-receive')
        (self.repo / 'hooks/pre-receive').chmod(0o755)
        self.source = self.home / 'source'
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.source)], check=True)
        subprocess.run(['/usr/bin/git', '-C', str(self.source), *FIXTURE_ID, 'commit', '-q', '--allow-empty',
                        '-m', 'fixture'], check=True)

    def push(self, *options, ref='run/a'):
        return subprocess.run(['/usr/bin/git', '-C', str(self.source), 'push', '-q', *options, str(self.repo),
                               'HEAD:refs/heads/' + ref], env=self.env, capture_output=True, text=True)

    def floor(self, protocol):
        (self.home / 'ci/kit').mkdir(parents=True, exist_ok=True)
        (self.home / 'ci/kit/PROTOCOL').write_text(protocol + '\n')

    def test_old_client_is_refused_with_update_hint(self):
        # Requirement: a client without the protocol option cannot submit to a runner with a host kit.
        self.floor('2')
        call = self.push()
        self.assertNotEqual(call.returncode, 0)
        self.assertIn('runner needs protocol 2, this client sent 0', call.stderr)
        self.assertIn('update your client toolkit', call.stderr)
        self.assertNotEqual(subprocess.run(['/usr/bin/git', '-C', str(self.repo),
            'show-ref', '--verify', 'refs/heads/run/a'], capture_output=True).returncode, 0)

    def test_current_client_is_accepted(self):
        self.floor('2')
        self.assertEqual(self.push('-o', 'remote-ci=2').returncode, 0)
        self.assertEqual(self.push('-o', 'remote-ci=3', ref='run/b').returncode, 0)

    def test_older_protocol_is_refused(self):
        self.floor('3')
        self.assertNotEqual(self.push('-o', 'remote-ci=2').returncode, 0)

    def test_no_host_kit_means_no_floor(self):
        self.assertEqual(self.push().returncode, 0)


if __name__ == '__main__':
    unittest.main()
