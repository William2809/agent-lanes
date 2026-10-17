"""Real processes, queue parking and kit switch, inside an isolated runner HOME."""
import hashlib
import json
import shutil
import subprocess
import time
import unittest
import test_scheduler_pause_lifecycle as fixtures


class RollingLifecycle(unittest.TestCase):
    setUp = fixtures.PauseLifecycle.setUp
    git = fixtures.PauseLifecycle.git
    stop = fixtures.PauseLifecycle.stop
    rows = fixtures.PauseLifecycle.rows
    evidence = fixtures.PauseLifecycle.evidence
    wait = fixtures.PauseLifecycle.wait
    launch = fixtures.PauseLifecycle.launch

    def upgrade(self, *args):
        result = subprocess.run(['python3', str(self.controller/'upgrade_host.py'), *map(str, args)],
                                env=self.env, capture_output=True, text=True, timeout=65)
        self.assertEqual(result.returncode, 0, result.stderr+'\n'+self.evidence())
        return json.loads(result.stdout)

    def test_running_job_finishes_and_parked_jobs_execute_original_snapshot_once(self):
        self.controller = self.home/'control'
        shutil.copytree(self.bin, self.controller)
        (self.ci/'kits').mkdir()
        (self.ci/'kit').symlink_to(self.bin)
        for name, value in [('VERSION', '00000000'), ('PROTOCOL', '2'), ('SOURCE', self.sha)]:
            (self.bin/name).write_text(value+'\n')
        stage = self.home/'candidate'
        shutil.copytree(self.bin, stage)
        # New kit has a marker; queued jobs must retain their old executable bytes.
        with (stage/'run.sh').open('a') as stream:
            stream.write('\n# rolling-upgrade candidate\n')
        holder = self.launch('holding', 'touch "$HOME/holding"; while [ ! -f "$HOME/release" ]; do sleep .1; done; echo done > "$HOME/holder-finished"')
        self.wait(lambda: (self.home/'holding').exists())
        first = self.launch('first', 'printf first >> "$HOME/calls"')
        self.wait(lambda: any(r.get('run_id') == 'first' and r['state'] == 'queued' for r in self.rows().values()))
        second = self.launch('second', 'printf second >> "$HOME/calls"')
        self.wait(lambda: any(r.get('run_id') == 'second' and r['state'] == 'queued' for r in self.rows().values()))
        expected = {t: {k: row.get(k) for k in ('sha', 'tree', 'run_id', 'sequence', 'submitted_at', 'deadline', 'command_digest')}
                    for t, row in self.rows().items()}
        original = hashlib.sha256((self.project/'runs/first/bin/run.sh').read_bytes()).hexdigest()
        state = self.upgrade('prepare', 'lifecycle', stage, 'abcdef12', '2', self.sha)
        self.assertEqual(self.upgrade('prepare', 'lifecycle', stage, 'abcdef12', '2', self.sha)['id'], 'lifecycle')
        for _ in range(3):
            state = self.upgrade('advance', 'lifecycle')
            if state['phase'] == 'draining':
                break
        self.wait(lambda: first.poll() is not None and second.poll() is not None)
        self.assertEqual(state['phase'], 'draining')
        self.assertIsNone(holder.poll(), self.evidence())
        self.assertFalse((self.home/'calls').exists())
        self.assertEqual((self.ci/'kit/VERSION').read_text().strip(), '00000000')
        self.assertEqual(self.upgrade('advance', 'lifecycle')['phase'], 'draining')
        (self.home/'release').touch()
        self.assertEqual(holder.wait(timeout=40), 0, self.evidence())
        self.assertTrue((self.home/'holder-finished').exists())
        for _ in range(12):
            state = self.upgrade('advance', 'lifecycle')
            if state['phase'] == 'ready':
                break
            time.sleep(.2)
        self.assertEqual(state['phase'], 'ready', state)
        self.wait(lambda: (self.project/'results/second.exit').exists())
        for run in ('first', 'second'):
            self.assertEqual((self.project/'results'/str(run+'.exit')).read_text(), '0\n', self.evidence())
        self.assertEqual((self.home/'calls').read_text(), 'firstsecond')
        self.assertEqual(original, hashlib.sha256((self.project/'runs/first/bin/run.sh').read_bytes()).hexdigest())
        for token, identity in expected.items():
            self.assertEqual({k: self.rows()[token].get(k) for k in identity}, identity)
        self.assertFalse((self.ci/'freeze').exists())
        self.assertEqual(self.upgrade('advance', 'lifecycle')['phase'], 'ready')
        self.assertEqual((self.home/'calls').read_text(), 'firstsecond')
