"""Brief requirements 1 and 3: real runner shell, fixture command, no real suite."""
import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

KIT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True


class PortableRunner(unittest.TestCase):
    def test_custom_command_and_duplicate_delivery(self):
        """Native decode/locks/time plus one execution despite repeated delivery.
        Gap: the fixture supplies host headroom; it does not measure capacity.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / 'ci/projects/fixture'
            scripts = project / 'runs/job/bin'
            shutil.copytree(KIT / 'remote', scripts)
            for name in ('logs', 'results'):
                (project / name).mkdir()
            sampler = scripts / 'resources.py'
            sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
                "sample=lambda: dict(cores=8, load=0, available_mb=16000, pressure=1)\nif __name__ == '__main__':"))
            (scripts.parent / 'conf').write_text("REMOTE_CI_CHECK='exit 19'\nREMOTE_CI_WAIT_TIMEOUT=15\nREMOTE_CI_TIMEOUT=10\n")
            env = dict(os.environ, HOME=tmp, AGENT_CI_MAX_JOBS='1', AGENT_CI_MAX_WORKERS='1')
            def git(*args):
                return subprocess.check_output(['git', *map(str, args)], env=env,
                                               stderr=subprocess.DEVNULL, text=True).strip()
            source = home / 'source'
            git('init', '-q', source)
            (source / 'fixture').write_text('fixture')
            git('-C', source, 'add', 'fixture')
            git('-C', source, '-c', 'user.name=Fixture', '-c', 'user.email=user@example.invalid',
                'commit', '-qm', 'fixture')
            sha = git('-C', source, 'rev-parse', 'HEAD')
            git('init', '-q', '--bare', project / 'repo.git')
            git('-C', project / 'repo.git', 'fetch', '-q', source, 'HEAD:refs/heads/run/job')
            encoded = base64.b64encode(b'printf x >> "$HOME/calls"').decode()
            call = ['/bin/bash', str(scripts / 'run.sh'), 'fixture', sha, 'tree', 'job', encoded,
                    '0', 'runner', 'generation']
            first = subprocess.run(call, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(first.returncode, 0, (project / 'logs/job.log').read_text())
            before = (project / 'logs/job.log').read_bytes()
            again = subprocess.run(call, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(again.returncode, 3)
            self.assertEqual((home / 'calls').read_text(), 'x')
            self.assertEqual((project / 'logs/job.log').read_bytes(), before)
            self.assertEqual((project / 'results/job.exit').read_text().strip(), '0')


if __name__ == '__main__':
    unittest.main()
