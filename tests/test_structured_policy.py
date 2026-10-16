"""Review regressions: unchanged failures cannot disappear through unreviewed settings."""
import contextlib
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from workflow_fixture import ROOT, WorkflowFixture


def pytest_python(env=None):
    """Use an installed pytest; local absence may skip, CI absence must fail."""
    for name in (sys.executable, 'python3.14', 'python3.13', 'python3.12', 'python3.11',
                 '/opt/homebrew/bin/python3', 'python3'):
        executable = shutil.which(name)
        if not executable:
            continue
        try:
            result = subprocess.run([executable, '-c', 'import pytest'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, env=env)
            if result.returncode == 0:
                return executable
        except (OSError, subprocess.TimeoutExpired):
            continue
    if os.environ.get('CI'):
        raise RuntimeError('pytest not installed: required in CI')
    print('skipped: pytest not installed', flush=True)
    raise unittest.SkipTest('pytest not installed')


class PytestRuntimeTests(unittest.TestCase):
    def test_alternate_python_with_pytest_is_selected(self):
        """Brief 2: select pytest under fixture HOME. Runtime probes are stubbed."""
        with patch('sys.executable', '/fixture/no-pytest'), \
                patch('shutil.which', side_effect=lambda name: name if name.startswith('/fixture/') or name == 'python3.14' else None), \
                patch('subprocess.run', side_effect=lambda args, **kw: subprocess.CompletedProcess(args, 0 if args[0] == 'python3.14' and kw.get('env') == {'HOME': '/fixture/home'} else 1)):
            self.assertEqual(pytest_python({'HOME': '/fixture/home'}), 'python3.14')

    def test_local_absence_has_one_visible_skip(self):
        """Brief 2: no pytest on any runtime; skip stays visible. Discovery is stubbed."""
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch('shutil.which', return_value=None), \
                contextlib.redirect_stdout(output), self.assertRaises(unittest.SkipTest):
            pytest_python()
        self.assertEqual(output.getvalue(), 'skipped: pytest not installed\n')

    def test_ci_absence_fails(self):
        """Brief 2: CI may never skip missing pytest. Discovery is stubbed."""
        with patch.dict(os.environ, {'CI': 'true'}, clear=True), \
                patch('shutil.which', return_value=None), self.assertRaisesRegex(RuntimeError, 'required in CI'):
            pytest_python()


class StructuredPolicyTests(unittest.TestCase):
    def fixture(self):
        f = WorkflowFixture(())
        f.env['PYTHONDONTWRITEBYTECODE'] = '1'
        self.addCleanup(f.close)
        return f

    def guard(self, f, lane):
        return f.run(sys.executable, str(ROOT / 'bin/mq-guard.py'), '--policy', str(lane), 'main', check=False)

    def test_guard_finds_toml_python_after_39_like_python3(self):
        """Brief 1: PATH starts with Python without TOML; alternate runtime is real."""
        f = self.fixture()
        lane = f.add_lane('candidate')
        f.commit(lane, 'pyproject.toml', '[project]\nname="demo"\n')
        runtimes = f.root / 'runtimes'
        runtimes.mkdir()
        toml_python = sys.executable
        if sys.version_info < (3, 11):
            toml_python = next((shutil.which(name) for name in
                                ('python3.14', 'python3.13', 'python3.12', 'python3.11')
                                if shutil.which(name)), sys.executable)
        old = runtimes / 'old.py'
        old.write_text("""import builtins, runpy, sys
original = builtins.__import__
def without_toml(name, *args, **kwargs):
    if name in ('tomllib', 'tomli'):
        raise ImportError('Python 3.9-like runtime without tomli')
    return original(name, *args, **kwargs)
builtins.__import__ = without_toml
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
""")
        for name, body in (
                ('python3', f'exec "{sys.executable}" "{old}" "$@"'),
                ('python3.14', f'echo selected >"{runtimes / "selected"}"\nexec "{toml_python}" "$@"')):
            script = runtimes / name
            script.write_text('#!/bin/sh\n' + body + '\n')
            script.chmod(0o755)
        f.env['PATH'] = str(runtimes) + ':' + f.env['PATH']
        result = f.run('python3', str(ROOT / 'bin/mq-guard.py'), '--policy', str(lane), 'main', check=False)
        self.assertEqual((result.returncode, result.stdout), (0, ''), result.stderr)
        self.assertTrue((runtimes / 'selected').exists(), 'guard must use the TOML-capable runtime')

    def test_pytest_configuration_cannot_hide_an_unchanged_failure(self):
        """Brief 2: run all three policy-hiding cases with pytest in the isolated HOME."""
        # Probe before subtests so a missing dependency produces exactly one skip.
        python = pytest_python(self.fixture().env)
        configs = {
            'pyproject.toml': '[tool.pytest.ini_options]\naddopts = "--ignore=test_bad.py"\n',
            'setup.cfg': '[tool:pytest]\naddopts = --ignore=test_bad.py\n',
            'pytest.ini': '[pytest]\naddopts = --ignore=test_bad.py\n',
        }
        for path, text in configs.items():
            with self.subTest(path=path):
                f = self.fixture()
                f.commit(f.repo, 'test_good.py', 'def test_good(): assert True\n')
                f.commit(f.repo, 'test_bad.py', 'def test_bad(): assert False\n')
                lane = f.add_lane('candidate')
                before = f.run(python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', cwd=lane, check=False)
                self.assertEqual(before.returncode, 1, before.stdout + before.stderr)
                f.commit(lane, path, text)
                after = f.run(python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', cwd=lane, check=False)
                self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
                self.assertEqual(f.git('diff', 'main', '--', 'test_bad.py', cwd=lane), '')
                guard = self.guard(f, lane)
                self.assertTrue(guard.returncode or path in guard.stdout, guard.stdout)
                f.mq('add', 'candidate')
                self.assertIn('PARKED candidate: check policy changed', f.mq('run').stdout)

    def test_sections_and_malformed_files_require_review(self):
        pairs = [
            ('package.json', '{"scripts":{"test":"jest"}}', '{"scripts":{"test":"jest"},"jest":{"testPathIgnorePatterns":["bad"]}}'),
            ('pyproject.toml', '[project]\nname="demo"\n', '[tool.pytest]\naddopts=["--ignore=test_bad.py"]\n'),
            ('pyproject.toml', '[tool.coverage.report]\nfail_under=90\n', '[tool.coverage.report]\nfail_under=0\n'),
            ('setup.cfg', '[coverage:report]\nfail_under=90\n', '[coverage:report]\nfail_under=0\n'),
            ('package.json', '{bad old', '{bad new'),
            ('package.json', '{}', '[]'),
            ('pyproject.toml', '[project]\nname="demo"\n', '[broken'),
            ('setup.cfg', '[metadata]\nname=demo\n', 'broken'),
        ]
        for path, before, after in pairs:
            with self.subTest(path=path, after=after):
                f=self.fixture(); f.commit(f.repo, path, before); lane=f.add_lane('candidate'); f.commit(lane,path,after)
                guard=self.guard(f,lane)
                self.assertTrue(guard.returncode or path in guard.stdout, guard.stdout)

    def test_dependency_only_edits_still_pass(self):
        for path, before, after in [
            ('package.json', '{"scripts":{"test":"jest"},"dependencies":{"a":"1"}}', '{"scripts":{"test":"jest"},"dependencies":{"a":"2"}}'),
            ('pyproject.toml', '[tool.pytest.ini_options]\naddopts="-q"\n[project]\ndependencies=["a==1"]\n', '[tool.pytest.ini_options]\naddopts="-q"\n[project]\ndependencies=["a==2"]\n'),
            ('setup.cfg', '[tool:pytest]\naddopts=-q\n[metadata]\nversion=1\n', '[tool:pytest]\naddopts=-q\n[metadata]\nversion=2\n'),
        ]:
            with self.subTest(path=path):
                f=self.fixture(); f.commit(f.repo,path,before); lane=f.add_lane('candidate'); f.commit(lane,path,after)
                guard=self.guard(f,lane)
                self.assertEqual((guard.returncode,guard.stdout),(0,''),guard.stderr)

    def test_custom_inputs_come_from_target_not_candidate(self):
        f=self.fixture(); f.commit(f.repo,'.mq-policy','tools/accept.sh\nchecks/\n')
        f.commit(f.repo,'tools/accept.sh','exit 1\n'); lane=f.add_lane('candidate')
        f.commit(lane,'tools/accept.sh','exit 0\n'); f.commit(lane,'.mq-policy','# removed by candidate\n')
        guard=self.guard(f,lane)
        self.assertEqual(guard.returncode,0,guard.stderr)
        self.assertIn('tools/accept.sh',guard.stdout)
        self.assertIn('.mq-policy',guard.stdout)

    def test_parse_error_does_not_echo_configuration_values(self):
        f=self.fixture(); lane=f.add_lane('candidate')
        f.commit(lane,'setup.cfg','fixture-private-token = malformed\n')
        guard=self.guard(f,lane)
        self.assertEqual(guard.returncode,2)
        self.assertIn('cannot parse setup.cfg',guard.stderr)
        self.assertNotIn('fixture-private-token',guard.stderr)
