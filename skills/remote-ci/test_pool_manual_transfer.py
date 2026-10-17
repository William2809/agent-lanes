"""Manual queue transfer stays bound to the live pool client that owns resubmission."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'bin'))
import manual_transfer as transfer  # noqa: E402


class ManualTransfer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.birth = mock.patch.object(transfer, 'process_birth', return_value='owner-birth')
        self.birth.start()

    def tearDown(self):
        self.birth.stop()
        self.temp.cleanup()

    def test_live_owner_accepts_one_idempotent_configured_request(self):
        owner = transfer.Owner('proj', 'run-1', 'source', ['pc'], self.root, pid=42, birth='owner-birth')
        self.assertEqual(transfer.status('run-1', 'source', 'pc', self.root),
                         {'available': True, 'destination': 'pc'})
        expected = {'available': False, 'state': 'pending', 'reason': 'move requested'}
        self.assertEqual(transfer.request('run-1', 'source', 'pc', {'source', 'pc'}, self.root), expected)
        self.assertEqual(transfer.request('run-1', 'source', 'pc', {'source', 'pc'}, self.root), expected)
        self.assertEqual(owner.request(), 'pc')
        owner.result('moved', destination='pc')
        self.assertEqual(transfer.status('run-1', 'source', 'pc', self.root)['state'], 'moved')

    def test_stale_pid_and_arbitrary_destination_are_never_eligible(self):
        transfer.Owner('proj', 'run-2', 'source', ['pc'], self.root, pid=42, birth='old-birth')
        self.assertIn('ended', transfer.status('run-2', 'source', 'pc', self.root)['reason'])
        with self.assertRaisesRegex(ValueError, 'invalid request'):
            transfer.request('run-2', 'source', 'evil;command', {'source', 'pc'}, self.root)

    def test_request_is_bound_to_the_owner_birth_that_registered_it(self):
        old = transfer.Owner('proj', 'run-bound', 'source', ['pc'], self.root,
                             pid=42, birth='first-birth')
        with mock.patch.object(transfer, 'process_birth', return_value='first-birth'):
            transfer.request('run-bound', 'source', 'pc', {'source', 'pc'}, self.root)
        replacement = transfer.Owner('proj', 'run-bound', 'source', ['pc'], self.root,
                                     pid=42, birth='second-birth')
        self.assertIsNone(old.request())
        self.assertFalse(old.result('moved', destination='pc'))
        self.assertIsNone(replacement.request())

    def test_oversized_state_is_ignored(self):
        path = self.root / 'large.json'
        path.write_text('{"padding":"' + ('x' * transfer.MAX_JSON) + '"}')
        self.assertIsNone(transfer.load(path))

    def test_incompatible_and_unregistered_clients_explain_why(self):
        transfer.Owner('proj', 'run-3', 'source', [], self.root, pid=42, birth='owner-birth')
        self.assertIn('incompatible', transfer.status('run-3', 'source', 'pc', self.root)['reason'])
        self.assertIn('old or pinned', transfer.status('old-run', 'source', 'pc', self.root)['reason'])

    def test_registration_contains_no_route_token_or_environment(self):
        transfer.Owner('proj', 'run-4', 'source', ['pc'], self.root, pid=42, birth='owner-birth')
        saved = json.loads((self.root / 'run-4/owner.json').read_text())
        self.assertEqual(set(saved), {'version', 'project', 'run', 'source', 'destinations',
                                      'pid', 'birth', 'state', 'updated'})


if __name__ == '__main__':
    unittest.main()
