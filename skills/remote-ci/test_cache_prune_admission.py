"""V2 cache pruning requires an admitted maintenance phase."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parent
REMOTE = KIT / 'remote'
spec = importlib.util.spec_from_file_location('cache_prune_tested', REMOTE / 'cache.py')
cache_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache_module)


class PruneAdmission(unittest.TestCase):
    def real_home(self, root):
        home = Path(root).resolve()
        config = home / '.config/agent-lanes/config'
        config.parent.mkdir(parents=True)
        config.write_text('AGENT_CI_SCHEDULER=v2\nAGENT_CI_DISK_FLOOR_GB=1\n')
        return home

    def real_cli(self, home, *args, env=None):
        variables = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
        variables.pop('AGENT_CI_TOKEN', None)
        variables.pop('AGENT_CI_LEASE_GENERATION', None)
        variables.update(env or {})
        return subprocess.run([sys.executable, str(REMOTE / 'cache.py'), *args],
                              env=variables, capture_output=True, text=True, timeout=30)

    def test_real_v2_no_token_defers_before_suspect_registry_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            home = self.real_home(temp)
            ci = (home / 'ci').resolve()
            registry = ci / 'cache/registry.json'
            registry.parent.mkdir(parents=True)
            registry.write_text('{suspect sentinel')
            call = self.real_cli(home, 'prune', '--ci', str(ci))
            self.assertEqual(call.returncode, 0, call.stdout + call.stderr)
            self.assertIn('cache prune deferred scheduler=v2', call.stdout)
            self.assertEqual(registry.read_text(), '{suspect sentinel')

    def test_real_v2_stale_and_check_phase_tokens_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            home = self.real_home(temp)
            ci = (home / 'ci').resolve()
            state = home / '.local/state/agent-ci'
            state.mkdir(parents=True)
            row = dict(version=2, protocol=2, state='running', phase='check',
                       lease_generation=7, owner=99999999, start='missing', groups={})
            (state / 'queue.json').write_text(json.dumps(
                dict(sequence=1, authority='v2', jobs={'check-token': row})))
            for token in ('missing-token', 'check-token'):
                with self.subTest(token=token):
                    call = self.real_cli(home, 'prune', '--ci', str(ci), env={
                        'AGENT_CI_TOKEN': token, 'AGENT_CI_LEASE_GENERATION': '7'})
                    self.assertEqual(call.returncode, 74, call.stdout + call.stderr)
                    self.assertIn('cache unavailable; entry not trusted', call.stderr)

    def test_real_live_maintenance_phase_can_prune(self):
        helper = r'''import os,sys,time
import resources,scheduler_process
queue=resources.Queue()
procs=scheduler_process.snapshot(); owner=procs[os.getpid()]
token='maintenance-live'; generation=7
row=dict(version=2,protocol=2,project='maintenance',lane='',sha='',tree='fixture',mode='run',
 command_digest='a'*64,generation=1,owner=os.getpid(),start=owner['start'],owner_birth=owner['birth'],
 sequence=1,lease_generation=generation,phase='admitted',submitted_at=time.time(),updated_at=time.time(),
 deadline=time.time()+20,state='running',landing=True,blocker=None,cleanup_required=True,
 requested=dict(slots=1,workers=1,memory_mb=4096,leases=['exclusive-host','prune']),
 workers=1,memory_mb=4096,slot=1,groups={})
with queue.transaction() as data:
 data['authority']='v2'; data['sequence']=1; data['jobs'][token]=row
rc=scheduler_process.run(queue,token,'maintenance',10,None,
 [sys.executable,sys.argv[1],'prune','--ci',sys.argv[2]])
raise SystemExit(rc)
'''
        with tempfile.TemporaryDirectory() as temp:
            home = self.real_home(temp)
            ci = (home / 'ci').resolve()
            variables = dict(os.environ, HOME=str(home), PYTHONPATH=str(REMOTE),
                             PYTHONDONTWRITEBYTECODE='1')
            call = subprocess.run([sys.executable, '-c', helper,
                                   str(REMOTE / 'cache.py'), str(ci)], env=variables,
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(call.returncode, 0, call.stdout + call.stderr)
            self.assertIn('cache prune entries=0 bounded=true', call.stdout)

    def invoke(self, ci, coordinator, env=None):
        stdout = io.StringIO()
        variables = {'HOME': str(Path(ci).parent)}
        variables.update(env or {})
        with mock.patch.dict(os.environ, variables, clear=True), \
                mock.patch.object(cache_module, 'coordinator', side_effect=coordinator), \
                mock.patch.object(sys, 'argv', ['cache.py', 'prune', '--ci', str(ci)]), \
                contextlib.redirect_stdout(stdout):
            result = cache_module.main()
        return result, stdout.getvalue()

    def test_v2_without_token_defers_without_scan_or_delete(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(cache_module.Cache, 'evict') as evict:
            result, output = self.invoke(Path(temp) / 'ci', ['v2'])
        self.assertEqual(result, 0)
        self.assertIn('cache prune deferred scheduler=v2', output)
        evict.assert_not_called()

    def test_v2_check_or_stale_token_is_refused(self):
        for label in ('check-phase', 'stale-token'):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp, \
                    mock.patch.object(cache_module.Cache, 'evict') as evict:
                def coordinator(*args):
                    if args == ('scheduler-version',):
                        return 'v2'
                    self.assertEqual(args[-2:], ('--phase', 'maintenance'))
                    raise ValueError(label)
                with self.assertRaises(ValueError):
                    self.invoke(Path(temp) / 'ci', coordinator,
                                {'AGENT_CI_TOKEN': label,
                                 'AGENT_CI_LEASE_GENERATION': '7'})
                evict.assert_not_called()

    def test_unknown_authority_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(cache_module.Cache, 'evict') as evict:
            with self.assertRaises(ValueError):
                self.invoke(Path(temp) / 'ci', ['unknown'])
            evict.assert_not_called()

    def test_admitted_maintenance_evicts_idle_but_honors_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            ci = (Path(temp) / 'ci').resolve()
            pinned = ci / 'cache' / 'pinned'
            idle = ci / 'cache' / 'idle'
            pinned.mkdir(parents=True)
            idle.mkdir()
            ledger = cache_module.Cache(ci, delete=lambda path: shutil.rmtree(path))
            ledger.register('pinned', pinned, 'run', token='active', size=10, used=2)
            ledger.register('idle', idle, 'run', size=10, used=1)

            def coordinator(*args):
                if args == ('scheduler-version',):
                    return 'v2'
                self.assertIn('--token', args)
                self.assertIn('maintenance-token', args)
                self.assertEqual(args[-2:], ('--phase', 'maintenance'))
                return ''

            with mock.patch.object(cache_module, 'host_cap', return_value=10), \
                    mock.patch.object(cache_module, 'disk_floor', return_value=0), \
                    mock.patch.object(cache_module.Cache, 'prune_delete',
                                      new=lambda self, path: shutil.rmtree(path)):
                result, output = self.invoke(ci, coordinator,
                    {'AGENT_CI_TOKEN': 'maintenance-token',
                     'AGENT_CI_LEASE_GENERATION': '7'})
            self.assertEqual(result, 0)
            self.assertIn('cache prune entries=1 bounded=true', output)
            self.assertTrue(pinned.exists())
            self.assertFalse(idle.exists())

    def test_v1_prune_behavior_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(cache_module.Cache, 'evict', return_value=([], True)) as evict:
            result, output = self.invoke(Path(temp) / 'ci', ['v1'])
        self.assertEqual(result, 0)
        self.assertIn('cache prune entries=0 bounded=true', output)
        evict.assert_called_once()

    def test_cached_client_registers_run_without_opportunistic_prune(self):
        source = (KIT / 'bin/remote-ci').read_text()
        start = source.index('if [ "$force" = 0 ] && [ -z "$command" ] && remote')
        end = source.index('echo "remote-ci: this exact code already passed', start)
        cached = source[start:end]
        self.assertIn('cache.py register --key run-', cached)
        self.assertNotIn('cache.py prune', cached)


if __name__ == '__main__':
    unittest.main()
