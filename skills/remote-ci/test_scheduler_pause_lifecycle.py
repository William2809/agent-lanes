"""Actual pooled admission, maintenance install, and same-run resumption.

Only host headroom is simulated; Git, runner, controller, process identities,
installer audit and candidate execution are real. No application services.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

KIT = Path(__file__).resolve().parent


class PauseLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ci-pause-lifecycle-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ci = self.home/'ci'
        self.project = self.ci/'projects/fixture'
        for name in ('logs', 'results'):
            (self.project/name).mkdir(parents=True)
        self.bin = self.ci/'bin'
        shutil.copytree(KIT/'remote', self.bin)
        sampler = self.bin/'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "scheduler_sample=lambda: dict(cores=8, load=0, available_mb=16000, total_mb=24000, pressure=1, swap_mb=0)\nif __name__ == '__main__':"))
        settings = self.home/'.config/agent-lanes/config'
        settings.parent.mkdir(parents=True)
        settings.write_text('AGENT_CI_SCHEDULER=v2\nAGENT_CI_MAX_JOBS=1\n'
                            'AGENT_CI_MAX_WORKERS=1\nAGENT_CI_MAX_JOB_MB=8192\n'
                            'AGENT_CI_FAST_LANE=1\n')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('AGENT_CI_', 'REMOTE_CI_', 'REMOTE_CI_', 'SCHEDULER_'))}
        self.env.update(HOME=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        self.procs = []
        self.addCleanup(self.stop)
        self.source = self.home/'source'
        self.git('init', '-q', self.source)
        (self.source/'fixture').write_text('fixture')
        self.git('-C', self.source, 'add', 'fixture')
        self.git('-C', self.source, '-c', 'user.name=Fixture', '-c',
                 'user.email=user@example.invalid', 'commit', '-qm', 'fixture')
        self.sha = self.git('-C', self.source, 'rev-parse', 'HEAD')
        self.git('init', '--bare', '-q', self.project/'repo.git')

    def git(self, *args):
        return subprocess.check_output(['git', *map(str, args)], env=self.env,
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def stop(self):
        (self.home/'release').touch()
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)

    def rows(self):
        path = self.home/'.local/state/agent-ci/queue.json'
        return json.loads(path.read_text())['jobs'] if path.exists() else {}

    def evidence(self):
        return '\n'.join(str(p.relative_to(self.home))+': '+p.read_text()
                         for pattern in ('ci/projects/*/logs/*.log',
                                         'ci/projects/*/results/*.exit',
                                         '.local/state/agent-ci/queue.json')
                         for p in self.home.glob(pattern))

    def wait(self, predicate, seconds=40):
        until = time.monotonic()+seconds
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.1)
        self.fail('lifecycle event timed out\n'+self.evidence())

    def launch(self, run, command):
        directory = self.project/'runs'/run
        shutil.copytree(self.bin, directory/'bin')
        directory.chmod(0o700)
        (directory/'conf').write_text("REMOTE_CI_CHECK='false'\nREMOTE_CI_WAIT_TIMEOUT=240\n"
                                      'REMOTE_CI_TIMEOUT=120\nREMOTE_CI_WORKERS=1\nREMOTE_CI_SLOTS=1\n')
        (directory/'submission.json').write_text(json.dumps(dict(
            schema='agent-ci.submission/v1', protocol=2, park_version=1,
            lane=run, generation=1, landing=True)))
        self.git('-C', self.project/'repo.git', 'fetch', '-q', self.source,
                 'HEAD:refs/heads/run/'+run)
        tree = hashlib.sha256(run.encode()).hexdigest()
        argv = ['fixture', self.sha, tree, run, base64.b64encode(command.encode()).decode(),
                '0', 'runner', 'assignment-'+run, str(int(time.time()))]
        proc = subprocess.Popen(['/bin/bash', str(directory/'bin/run.sh'), *argv],
                                env=self.env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(proc)
        return proc

    def control(self, *args):
        result = subprocess.run(['python3', str(self.bin/'scheduler_pause.py'), *args],
                                env=self.env, capture_output=True, text=True, timeout=50)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr+'\n'+self.evidence())
        return result.stdout

    def test_park_install_resume_original_pooled_job_once(self):
        holder = self.launch('holder', 'touch "$HOME/holding"; while [ ! -f "$HOME/release" ]; do sleep .1; done')
        self.wait(lambda: (self.home/'holding').exists())
        saved = self.launch('saved', 'printf saved >> "$HOME/calls"')
        self.wait(lambda: any(r.get('run_id') == 'saved' and r['state'] == 'queued' for r in self.rows().values()))
        token, original = next((t, r) for t, r in self.rows().items() if r.get('run_id') == 'saved')
        later = self.launch('earlier-name', 'printf later >> "$HOME/calls"')
        self.wait(lambda: any(r.get('run_id') == 'earlier-name' and r['state'] == 'queued' for r in self.rows().values()))
        self.control('pause', 'fixture upgrade')
        self.wait(lambda: saved.poll() is not None)
        self.assertEqual(saved.returncode, 0, self.evidence())
        self.assertEqual(later.wait(timeout=20), 0, self.evidence())
        self.assertFalse((self.project/'results/earlier-name.exit').exists())
        parked = self.rows()[token]
        self.assertEqual(parked['state'], 'parked', self.evidence())
        for key in ('sequence', 'deadline', 'submitted_at', 'sha', 'tree', 'run_id'):
            self.assertEqual(parked[key], original[key])
        self.assertFalse((self.project/'results/saved.exit').exists())
        self.assertFalse((self.home/'calls').exists())
        self.assertTrue(json.loads(self.control('status'))['blockers'])
        (self.home/'release').touch()
        self.assertEqual(holder.wait(timeout=40), 0, self.evidence())
        status = json.loads(self.control('status'))
        self.assertEqual((status['parked'], status['blockers']), (2, 0), self.evidence())
        stage = self.ci/'stage'
        (self.ci/'kits').mkdir()
        shutil.copytree(self.bin, stage)
        install = subprocess.run(['/bin/bash', str(self.bin/'install-kit.sh'), 'install',
                                  str(stage), 'abc123', '2', self.sha], env=self.env,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(install.returncode, 0, install.stdout+install.stderr+'\n'+self.evidence())
        before = (self.project/'logs/saved.log').read_bytes()
        self.control('resume')
        self.wait(lambda: (self.project/'results/saved.exit').exists())
        self.assertEqual((self.project/'results/saved.exit').read_text(), '0\n', self.evidence())
        self.wait(lambda: (self.project/'results/earlier-name.exit').exists())
        self.assertEqual((self.project/'results/earlier-name.exit').read_text(), '0\n', self.evidence())
        self.assertEqual((self.home/'calls').read_text(), 'savedlater', self.evidence())
        self.assertTrue((self.project/'logs/saved.log').read_bytes().startswith(before))
        for key in ('sequence', 'deadline', 'submitted_at', 'sha', 'tree', 'run_id'):
            self.assertEqual(self.rows()[token][key], original[key])
        self.assertEqual((self.project/'runs/saved/assignment.started').read_text(), 'assignment-saved\n')
        self.control('resume')
        self.assertEqual((self.home/'calls').read_text(), 'savedlater')
        self.assertFalse((self.ci/'freeze').exists())

    def test_upgrade_drains_v1_queue_before_switching_authority(self):
        """The first v1 upgrade drains saved work; only v2 work can park."""
        settings = self.home/'.config/agent-lanes/config'
        settings.write_text('AGENT_CI_SCHEDULER=v1\nAGENT_CI_MAX_JOBS=1\n'
                            'AGENT_CI_MAX_WORKERS=1\n')
        sampler = self.bin/'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "sample=lambda: dict(cores=8, load=0, available_mb=16000, pressure=1)\nif __name__ == '__main__':"))
        holder = self.launch('legacy-holder', 'touch "$HOME/holding"; while [ ! -f "$HOME/release" ]; do sleep .1; done')
        self.wait(lambda: (self.home/'holding').exists())
        queued = self.launch('legacy-queued', 'printf legacy >> "$HOME/calls"')
        log = self.project/'logs/legacy-queued.log'
        self.wait(log.exists)
        saved = self.project/'runs/legacy-queued'
        original = {str(path.relative_to(saved)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in saved.rglob('*') if path.is_file()}
        self.assertIsNone(queued.poll(), self.evidence())
        self.assertFalse((self.home/'calls').exists())
        (self.ci/'freeze').write_text('v1 to v2 upgrade\n')
        count = subprocess.run(['/bin/bash', str(self.bin/'install-kit.sh'), 'count'],
                               env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(count.returncode, 0, count.stderr)
        self.assertGreater(int(count.stdout), 0)
        self.assertIsNone(queued.poll(), self.evidence())
        # Already queued work continues unchanged while new intake is frozen.
        (self.home/'release').touch()
        self.assertEqual(holder.wait(timeout=40), 0, self.evidence())
        self.assertEqual(queued.wait(timeout=40), 0, self.evidence())
        self.assertEqual((self.home/'calls').read_text(), 'legacy')
        for name, digest in original.items():
            self.assertEqual(hashlib.sha256((saved/name).read_bytes()).hexdigest(), digest)
        self.assertEqual((self.project/'results/legacy-queued.exit').read_text(), '0\n')
        count = subprocess.run(['/bin/bash', str(self.bin/'install-kit.sh'), 'count'],
                               env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual((count.returncode, count.stdout.strip()), (0, '0'), count.stderr)
        stage = self.ci/'upgrade-stage'
        (self.ci/'kits').mkdir()
        shutil.copytree(self.bin, stage)
        install = subprocess.run(['/bin/bash', str(self.bin/'install-kit.sh'), 'install',
                                  str(stage), 'def456', '2', self.sha], env=self.env,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(install.returncode, 0, install.stdout+install.stderr)
        self.assertTrue((self.ci/'freeze').exists())
        settings.write_text('AGENT_CI_SCHEDULER=v2\nAGENT_CI_MAX_JOBS=1\n'
                            'AGENT_CI_MAX_WORKERS=1\nAGENT_CI_FAST_LANE=1\n')
        version = subprocess.run(['python3', str(self.ci/'kit/resources.py'), 'scheduler-version'],
                                 env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual((version.returncode, version.stdout.strip()), (0, 'v2'), version.stderr)
        (self.ci/'freeze').rename(self.ci/'freeze.last')
        upgraded = self.launch('new-v2', 'printf new >> "$HOME/calls"')
        self.assertEqual(upgraded.wait(timeout=40), 0, self.evidence())
        self.assertEqual((self.home/'calls').read_text(), 'legacynew')
        row = next(row for row in self.rows().values() if row.get('run_id') == 'new-v2')
        self.assertEqual(row['version'], 2)
        self.assertEqual(row['state'], 'finished')
        self.assertFalse(row.get('workers') or row.get('process') or row.get('groups'))
