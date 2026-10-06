import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys
sys.dont_write_bytecode = True


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / 'bin' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


failure = load('fail-summary')
blame = load('blame')


class Diagnostics(unittest.TestCase):
    def test_redaction(self):
        samples = [
            ('Error: postgres://example-user:example-pass@localhost/db', 'postgres://***@localhost/db'),
            ('https://user:p%40ss@host/x?token=example-value&x=1', 'https://***@host/x?token=[redacted]&x=1'),
            ('password=example-value token="two words" secret=example-value key=example-value', 'password=[redacted] token=[redacted] secret=[redacted] key=[redacted]'),
            ('Authorization: Basic example-value', 'Authorization: [redacted]'),
            ('authorization=Bearer example-value', 'Authorization: [redacted]'),
            ('"Authorization": "Basic example-value"', 'Authorization: [redacted]'),
            ('x ' + 'a' * 64, 'x [redacted]'),
            ('x ' + 'Ab3+/' * 12 + '=', 'x [redacted]'),
            ('x ' + 'AbCd09_-' * 8, 'x [redacted]'),
            ('x ' + 'AbCd09_-' * 8 + '.', 'x [redacted].'),
            ('x.' + 'AbCd09_-' * 8, 'x.[redacted]'),
            ('eyJ' + 'AbCd09_' * 8 + '.eyJ' + 'AbCd09_' * 8 + '.sig', '[redacted]'),
        ]
        for raw, expected in samples:
            with self.subTest(expected=expected):
                self.assertIn(expected, failure.redact(raw))
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    failure.emit('Error: ' + raw)
                self.assertNotIn('example-value', out.getvalue())
                self.assertNotIn('example-pass', out.getvalue())

    def test_output_boundary_and_paths(self):
        path = '/runner/ci/projects/project/logs/20261004-154242-83edf281-24906.log'
        file = 'modules/settings/components/account-number-input-validation.test.tsx'
        self.assertEqual(failure.redact(path), path)
        self.assertEqual(failure.redact(file), file)
        result = failure.parse('FAIL '+file+' > secret=example-value\nError: postgres://user:example-pass@host/db\n== finished rc=1', 'token=example-value')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            failure.emit(result, structured=True)
        self.assertNotIn('example-value', out.getvalue())
        self.assertNotIn('example-pass', out.getvalue())
        self.assertNotIn('\x1b', failure.redact('\x1b[31mError\x1b[0m'))
        self.assertEqual(failure.redact('token=' + 'x' * 500), 'token=[redacted]')

    def test_steps_and_errors(self):
        cases = [
            ('web:typecheck: src/a.ts(2,3): error TS2322: bad\n', 'typecheck', 'src/a.ts:2:3: error TS2322: bad'),
            ('/repo/work/apps/web/src/a.ts\n  4:5  error  bad rule\n', 'lint', '/repo/work/apps/web/src/a.ts:4:5: error  bad rule'),
            ('preflight failed: bad input\n', 'preflight', 'preflight failed'),
            ('web:test: FAIL src/a.test.ts > suite > superseded keeps status\nweb:test: × superseded keeps status 12ms\n', 'test', 'superseded keeps status'),
        ]
        for text, step, expected in cases:
            result = failure.parse(text + '== finished rc=1')
            self.assertEqual(result['summary'][0], f'Failure: {step} (rc=1)')
            self.assertIn(expected, '\n'.join(result['summary']))
        for rc in (0, 3, 75):
            self.assertFalse(failure.parse(f'FAIL src/a.ts\n== finished rc={rc}')['failed'])
        self.assertFalse(failure.parse('FAIL src/a.ts')['failed'])
        self.assertLessEqual(len(failure.parse('\n'.join('FAIL src/f%d.test.ts' % n for n in range(50)) + '\n== finished rc=1')['summary']), 15)

    def test_latest_is_not_latest_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            red = Path(tmp) / '20261004-abcdef01-1.log'
            red.write_text('== sha ' + 'abcdef01' * 5 + '\nFAIL src/a.ts\n== finished rc=1')
            self.assertTrue(failure.latest(tmp, 'abcdef0')['failed'])
            green = Path(tmp) / '20261004-abcdef01-2.log'
            green.write_text('== sha ' + 'abcdef01' * 5 + '\n== finished rc=0')
            self.assertFalse(failure.latest(tmp, 'latest')['failed'])
            self.assertFalse(failure.latest(tmp, 'abcdef01')['failed'])
            self.assertFalse(failure.latest(tmp, '12345678')['failed'])

    def test_ranking_never_invents_overlap(self):
        failed = ['apps/web/headers/status.test.ts']
        dirs = ['apps/web', 'packages/db']
        self.assertEqual(blame.rank(failed, failed, dirs)[0], 3)
        self.assertEqual(blame.rank(['apps/web/headers/status.ts'], failed, dirs)[0], 2)
        self.assertEqual(blame.rank(['apps/web/other.ts'], failed, dirs)[0], 1)
        self.assertEqual(blame.rank(['packages/db/status.ts'], failed, dirs), (0, 'no failing-file overlap'))

    def test_manifest_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                return subprocess.run(['git', '-C', tmp, *args], capture_output=True, text=True, check=True).stdout.strip()
            git('init', '-q')
            package = Path(tmp) / 'apps/web'
            package.mkdir(parents=True)
            (package / 'package.json').write_text('{"name":"web"}')
            (package / 'test.ts').write_text('')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=user@example.invalid', 'commit', '-qm', 'fixture')
            sha = git('rev-parse', 'HEAD')
            manifests = blame.packages(tmp, sha)
            self.assertEqual(blame.normalized(tmp, sha, [{'file':'test.ts', 'package':'web'}], manifests), ['apps/web/test.ts'])
            self.assertEqual(blame.normalized(tmp, sha, [{'file':'missing.ts', 'package':'web'}], manifests), [])

    def test_shell_client_exits_and_read_only_protocol_on_bash_32(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True, capture_output=True)
            (root / '.remote-ci.conf').write_text('REMOTE_CI_NAME=fixture\n')
            config = root / 'config'
            config.write_text('REMOTE_CI_DIRECT_HOST=fixture\nunset REMOTE_CI_HOSTKEY_ALIAS\n')
            logs = root / 'logs'
            logs.mkdir()
            log = logs / '20261004-abcdef01-1.log'
            log.write_text('== sha ' + 'abcdef01' * 5 + '\nError: postgres://user:example-pass@host/db\n== finished rc=1')
            ssh = root / 'ssh'
            ssh.write_text('''#!/usr/bin/env python3
import os, shlex, subprocess, sys
command = sys.argv[-1]
if command == 'true': sys.exit(0)
args = shlex.split(command)
if args[:3] != ['python3', '-', '--logs']: sys.exit(99)
args[3] = os.environ['HOME'] + '/logs'
sys.exit(subprocess.run(args, input=sys.stdin.read(), text=True).returncode)
''')
            ssh.chmod(0o755)
            env = dict(os.environ, HOME=tmp, AGENT_LANES_CONFIG=str(config), PATH=tmp + os.pathsep + os.environ['PATH'])
            client = str(Path(__file__).parent / 'bin/remote-ci')
            def call(*args):
                return subprocess.run(['/bin/bash', client, *args], cwd=tmp, env=env, capture_output=True, text=True)
            result = call('fail', 'abcdef01')
            self.assertEqual(result.returncode, 0)
            self.assertIn('postgres://***@host/db', result.stdout)
            self.assertNotIn('example-pass', result.stdout + result.stderr)
            self.assertEqual(call('fail', '12345678').returncode, 1)
            self.assertEqual(call('blame', 'abcdef01', 'lane').stdout.strip(), 'suspect unknown: failing files not named')
            log.write_text('== sha ' + 'abcdef01' * 5 + '\n== finished rc=0')
            self.assertEqual(call('fail', 'latest').returncode, 1)
            self.assertEqual(call('blame', 'abcdef01', 'lane').returncode, 1)


if __name__ == '__main__':
    unittest.main()
