"""Brief: provide a working cheap land preflight and reject red checks."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LandPreflightTests(unittest.TestCase):
    def setUp(self):
        # Keep isolated fixture evidence; no deletion commands.
        self.repo = Path(tempfile.mkdtemp(prefix='land-preflight-test.'))
        self.git('init', '-q', '-b', 'master')
        self.git('config', 'user.name', 'Preflight fixture')
        self.git('config', 'user.email', 'user@example.invalid')
        (self.repo / 'base.txt').write_text('base\n')
        self.git('add', '--', 'base.txt')
        self.git('commit', '-qm', 'fixture base')
        (self.repo / 'tools').mkdir()
        source = ROOT / 'tools/land-preflight.sh'
        if source.exists():
            shutil.copy2(source, self.repo / 'tools/land-preflight.sh')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True,
                              capture_output=True, text=True)

    def run_preflight(self):
        return subprocess.run(['sh', 'tools/land-preflight.sh', 'master'],
                              cwd=self.repo, capture_output=True, text=True)

    def test_clean_sources_pass_without_executing_them(self):
        # Brief: requested command must work; syntax checking must not run tools.
        (self.repo / 'valid.sh').write_text('#!/bin/sh\ntouch executed\n')
        (self.repo / 'valid.py').write_text('raise RuntimeError("must not execute")\n')
        result = self.run_preflight()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.repo / 'executed').exists())

    def test_whitespace_error_is_red(self):
        # Brief: fix a red preflight cause; preserve the whitespace gate.
        (self.repo / 'base.txt').write_text('trailing space \n')
        result = self.run_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('trailing whitespace', result.stdout + result.stderr)

    def test_invalid_sources_are_red(self):
        # Brief: syntax errors must fail, including files not yet staged.
        cases = (('broken.sh', '#!/bin/sh\nif then\n'),
                 ('broken.bash', '#!/usr/bin/env bash\nif then\n'),
                 ('broken.py', 'def broken(\n'),
                 ('broken.mjs', '#!/usr/bin/env node\nconst = ;\n'))
        for name, body in cases:
            with self.subTest(name=name):
                path = self.repo / name
                path.write_text(body)
                result = self.run_preflight()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stdout + result.stderr)
                # Keep each failed source as evidence outside the repo.
                path.rename(self.repo.parent / (self.repo.name + '.' + name))
