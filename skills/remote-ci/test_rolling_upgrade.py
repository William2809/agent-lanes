"""Coordinator ordering and recovery; real host lifecycle is tested separately."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).parent/'bin'
sys.path.insert(0, str(BIN))
import rolling_upgrade as upgrade


class Controller(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = patch.object(upgrade, 'STATE', self.root)
        self.state.start()
        self.addCleanup(self.state.stop)
        self.directory = self.root/'operation'
        self.directory.mkdir()
        self.path = self.directory/'operation.json'
        self.op = dict(id='fixture', rev='abcdef12', source='a'*40, phase='staging',
                       timeout=120, hosts=[dict(name='beta', route='beta'), dict(name='alpha', route='alpha')])
        upgrade.save(self.path, self.op)
        self.events = []

    def stage(self, op, host, directory):
        self.events.append(('stage', host['name']))
        return dict(phase='prepared')

    def advance(self, op, host, action):
        self.events.append(('advance', host['name']))
        return dict(phase='ready')

    def canary(self, op, host, directory):
        self.events.append(('canary', host['name']))

    def drive(self, **overrides):
        with patch.object(upgrade, 'stage', overrides.get('stage', self.stage)), \
             patch.object(upgrade, 'host_call', overrides.get('host_call', self.advance)), \
             patch.object(upgrade, 'run_canary', overrides.get('run_canary', self.canary)), \
             patch.object(upgrade, 'peer_ready', overrides.get('peer_ready', lambda host: None)):
            upgrade.drive(self.directory)

    def test_stage_every_device_then_verify_each_before_next(self):
        self.drive()
        self.assertEqual(self.events, [('stage', 'beta'), ('stage', 'alpha'), ('advance', 'beta'),
                                      ('canary', 'beta'), ('advance', 'alpha'), ('canary', 'alpha')])
        self.assertEqual(upgrade.read(self.path)['phase'], 'complete')
        self.events.clear()
        self.drive()
        self.assertEqual(self.events, [])

    def test_failed_canary_never_pauses_second_host_and_resume_reconciles_first(self):
        def failure(*args):
            raise RuntimeError('canary failed')
        with self.assertRaisesRegex(RuntimeError, 'canary failed'):
            self.drive(run_canary=failure)
        self.assertNotIn(('advance', 'alpha'), self.events)
        self.assertEqual(upgrade.read(self.path)['phase'], 'blocked')
        self.events.clear()
        self.drive()
        self.assertEqual(self.events[:2], [('stage', 'beta'), ('stage', 'alpha')])
        self.assertEqual(self.events[2], ('advance', 'beta'))
        self.assertEqual(upgrade.read(self.path)['phase'], 'complete')

    def test_transport_failure_after_remote_mutation_reconciles_instead_of_restage(self):
        def lost(*args):
            raise RuntimeError('ssh disconnected after host installed')
        with self.assertRaisesRegex(RuntimeError, 'disconnected'):
            self.drive(host_call=lost)
        self.events.clear()
        self.drive()
        self.assertEqual(self.events[2], ('advance', 'beta'))

    def test_second_device_unavailable_before_any_pause(self):
        def peer(host):
            if host['name'] == 'alpha':
                raise RuntimeError('offline')
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            self.drive(peer_ready=peer)
        self.assertFalse(any(event[0] == 'advance' for event in self.events))

    def test_finished_host_is_not_installed_or_tested_twice_on_recovery(self):
        self.op['hosts'][0].update(prepared=True, passed=1)
        self.op['hosts'][1].update(prepared=True)
        upgrade.save(self.path, self.op)
        self.drive()
        self.assertEqual(self.events, [('stage', 'alpha'), ('advance', 'alpha'), ('canary', 'alpha')])

    def test_canary_uses_verified_remote_route_without_reentering_pool_selection(self):
        from types import SimpleNamespace
        self.op.update(root=str(self.root), registry='beta=user@worker:linux')
        host = dict(name='beta', route='user@worker')
        def execute(argv, **kwargs):
            env = kwargs['env']
            self.assertNotIn('CI_RUNNERS', env)
            self.assertNotIn('REMOTE_CI_RUNNER', env)
            self.assertEqual(env['REMOTE_CI_HOST'], 'user@worker')
            self.assertEqual(env['REMOTE_CI_DIRECT_HOST'], 'worker')
            self.assertEqual(env['REMOTE_CI_USER'], 'user')
            self.assertIn('check', argv)
            kwargs['stdout'].write('rolling-upgrade-canary-ok\n')
            return SimpleNamespace(returncode=0)
        with patch.object(upgrade.subprocess, 'run', side_effect=execute):
            upgrade.run_canary(self.op, host, self.directory)
        self.assertTrue(Path(host['canary_log']).exists())

    def test_hash_matches_existing_git_blob_protocol(self):
        directory = self.root/'kit'
        directory.mkdir()
        (directory/'a.sh').write_text('one')
        (directory/'b.py').write_text('two')
        import subprocess
        def blob(data):
            return subprocess.check_output(['git', 'hash-object', '--stdin'], input=data).decode().strip()
        manifest = 'a.sh\n'+blob(b'one')+'\nb.py\n'+blob(b'two')+'\n'
        self.assertEqual(upgrade.kit_hash(directory), blob(manifest.encode())[:8])


if __name__ == '__main__':
    unittest.main()
