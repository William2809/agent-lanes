"""Focused state-machine checks for the rolling host upgrade controller."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
KIT = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT/'remote'))
import resources
import upgrade_host


class UpgradeHost(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, HOME=str(self.home))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ci = self.home/'ci'
        self.stage = self.home/'candidate'
        self.stage.mkdir()
        (self.stage/'install-kit.sh').write_text('#!/bin/bash\n')
        (self.stage/'install-kit.sh').chmod(0o755)
        (self.stage/'resources.py').write_text('candidate = True\n')

    def prepared(self):
        return upgrade_host.prepare('upgrade-1', self.stage, 'abc123', '2', 'def456')

    def test_prepare_is_immutable_and_rejects_existing_freeze(self):
        state = self.prepared()
        saved = Path(state['stage'])
        self.assertFalse(saved.stat().st_mode & 0o200)
        (self.ci/'freeze').write_text('manual\n')
        with self.assertRaisesRegex(ValueError, 'another upgrade'):
            upgrade_host.prepare('upgrade-2', self.stage, 'abc123', '2', 'def456')

    def test_prepare_retry_returns_same_operation_and_abandons_partial_stage(self):
        state = self.prepared()
        saved = Path(state['stage'])
        before_mtime = saved.stat().st_mtime_ns
        before_stages = sorted((self.ci/'upgrades/upgrade-1').glob('stage-*'))
        self.assertEqual(self.prepared(), state)
        self.assertEqual(saved.stat().st_mtime_ns, before_mtime)
        self.assertEqual(sorted((self.ci/'upgrades/upgrade-1').glob('stage-*')), before_stages)
        upgrade_host.journal_path().unlink()
        original = upgrade_host.immutable_copy
        def interrupted(source, target):
            target.mkdir(parents=True)
            (target/'partial').write_text('partial')
            raise OSError('disconnect')
        with patch.object(upgrade_host, 'immutable_copy', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'disconnect'):
                self.prepared()
        with patch.object(upgrade_host, 'immutable_copy', side_effect=original):
            recovered = self.prepared()
        self.assertNotEqual(Path(recovered['stage']).name, 'stage')
        self.assertGreaterEqual(len(list((self.ci/'upgrades/upgrade-1').glob('stage-*'))), 2)

    def test_legacy_75_becomes_drain_mode_and_repeated_advance_waits(self):
        with resources.Queue().transaction() as data:
            data['jobs']['legacy'] = dict(version=2, state='queued', project='old')
        self.prepared()
        self.assertEqual(upgrade_host.advance('upgrade-1')['phase'], 'pausing')
        state = upgrade_host.advance('upgrade-1')
        self.assertEqual((state['phase'], state['pause_mode'], state['incompatible']),
                         ('draining', 'drain', 1))
        with patch.object(upgrade_host, 'install_count', return_value=1):
            again = upgrade_host.advance('upgrade-1')
        self.assertEqual(again['phase'], 'draining')
        self.assertEqual(upgrade_host.load()['phase'], 'draining')

    def test_changed_freeze_is_preserved_before_resume(self):
        self.prepared()
        upgrade_host.advance('upgrade-1')
        with patch.object(upgrade_host.scheduler_pause, 'request_pause', wraps=upgrade_host.scheduler_pause.request_pause):
            upgrade_host.advance('upgrade-1')
        (self.ci/'freeze').write_text('operator freeze\n')
        state = upgrade_host.load()
        state['phase'] = 'installed'
        upgrade_host.save(state)
        with patch.object(upgrade_host, 'verify_installed'):
            resuming = upgrade_host.advance('upgrade-1')
            ready = upgrade_host.advance('upgrade-1')
        self.assertEqual(resuming['phase'], 'resuming')
        self.assertEqual(ready['phase'], 'ready')
        self.assertTrue(ready['manual_freeze_preserved'])
        self.assertEqual((self.ci/'freeze').read_text(), 'operator freeze\n')
        self.assertFalse(json.loads((self.ci/'logs/pause.json.last').read_text())['owned_freeze'])

    def test_identity_change_stops_installed_resume(self):
        queue = resources.Queue()
        with queue.transaction() as data:
            data['jobs']['token'] = dict(version=2, protocol=2, park_version=1,
                state='queued', project='fixture', run_id='run', sha='a'*40,
                tree='b'*64, sequence=1, submitted_at=1, deadline=9999999999,
                generation=1, command_digest='c'*64)
        self.prepared()
        upgrade_host.advance('upgrade-1')
        upgrade_host.advance('upgrade-1')
        state = upgrade_host.load()
        state['phase'] = 'installed'
        upgrade_host.save(state)
        with queue.transaction() as data:
            data['jobs']['token']['sha'] = 'f'*40
        with patch.object(upgrade_host, 'verify_installed'), \
             patch.object(upgrade_host.scheduler_pause, 'resume') as resume:
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                upgrade_host.advance('upgrade-1')
        resume.assert_not_called()
        self.assertTrue((self.ci/'freeze').exists())

    def test_missing_legacy_row_requires_durable_completion_receipt(self):
        expected = {'legacy': dict(version=1, run_id='legacy-run',
                                   _legacy_may_retire=True)}
        with self.assertRaisesRegex(ValueError, 'no unique completion receipt'):
            upgrade_host.audit_snapshot(resources.Queue(), expected)

    def test_missing_legacy_row_accepts_unique_numeric_completion_receipt(self):
        expected = {'legacy': dict(version=1, run_id='legacy-run',
                                   _legacy_may_retire=True)}
        receipt = self.ci/'projects/fixture/results/legacy-run.exit'
        receipt.parent.mkdir(parents=True)
        receipt.write_text('0\n')
        upgrade_host.audit_snapshot(resources.Queue(), expected)

    def test_pause_side_effect_before_journal_is_reconciled(self):
        self.prepared()
        state = upgrade_host.advance('upgrade-1')
        queue = resources.Queue()
        upgrade_host.scheduler_pause.request_pause(
            queue, self.ci, state['pause_reason'], {})
        recovered = upgrade_host.advance('upgrade-1')
        self.assertEqual(recovered['phase'], 'draining')
        self.assertTrue(upgrade_host.freeze_owned(recovered))

    def test_changed_freeze_blocks_install_until_restored(self):
        self.prepared()
        upgrade_host.advance('upgrade-1')
        state = upgrade_host.advance('upgrade-1')
        expected = (self.ci/'freeze').read_text()
        (self.ci/'freeze').write_text('operator maintenance\n')
        with self.assertRaisesRegex(ValueError, 'freeze changed'):
            upgrade_host.advance('upgrade-1')
        (self.ci/'freeze').write_text(expected)
        with patch.object(upgrade_host, 'install_count', return_value=1):
            self.assertEqual(upgrade_host.advance('upgrade-1')['phase'], 'draining')

    def test_resume_side_effect_before_journal_is_reconciled(self):
        self.prepared()
        upgrade_host.advance('upgrade-1')
        upgrade_host.advance('upgrade-1')
        state = upgrade_host.load()
        state['phase'] = 'installed'
        upgrade_host.save(state)
        with patch.object(upgrade_host, 'verify_installed'):
            self.assertEqual(upgrade_host.advance('upgrade-1')['phase'], 'resuming')
        upgrade_host.scheduler_pause.resume(resources.Queue(), self.ci, {})
        with patch.object(upgrade_host, 'verify_installed'):
            ready = upgrade_host.advance('upgrade-1')
        self.assertEqual(ready['phase'], 'ready')
        self.assertFalse((self.ci/'freeze').exists())

    def test_abort_refuses_candidate_selected_while_journal_draining(self):
        self.prepared()
        upgrade_host.advance('upgrade-1')
        state = upgrade_host.advance('upgrade-1')
        target = self.ci/'kits'/('%s-20261017120000' % state['revision'])
        target.parent.mkdir(parents=True)
        target.mkdir()
        for source in Path(state['stage']).iterdir():
            if source.is_file():
                (target/source.name).write_bytes(source.read_bytes())
        for name, value in (('VERSION', state['revision']), ('PROTOCOL', state['protocol']),
                            ('SOURCE', state['source'])):
            (target/name).write_text(value+'\n')
        (self.ci/'kit').symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'cannot be aborted'):
            upgrade_host.abort('upgrade-1')

    def test_abort_is_idempotent_only_before_install(self):
        self.prepared()
        first = upgrade_host.abort('upgrade-1')
        second = upgrade_host.abort('upgrade-1')
        self.assertEqual((first['phase'], second['phase']), ('aborted', 'aborted'))
        state = self.prepared_with_terminal_reuse()
        state['phase'] = 'installed'
        upgrade_host.save(state)
        with self.assertRaisesRegex(ValueError, 'cannot be aborted'):
            upgrade_host.abort('upgrade-2')

    def prepared_with_terminal_reuse(self):
        return upgrade_host.prepare('upgrade-2', self.stage, 'abc123', '2', 'def456')


if __name__ == '__main__':
    unittest.main()
