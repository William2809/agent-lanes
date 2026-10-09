import unittest
from workflow_fixture import WorkflowFixture


class RepairGuardTests(unittest.TestCase):
    """A bounce repair may add tests; changing existing test lines or adding a skip waits for review."""

    def setUp(self):
        self.f = WorkflowFixture()
        self.f.mq('claim', 'b', 'b.txt', 'tests/')
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.commit(self.f.lanes['b'], 'tests/test_a.py', 'assert total() == 3\n')
        self.f.mq('add', 'b')
        # Lane b (the fixture fails lane a's first land). It bounced; its fixer finished with work committed after this head.
        (self.f.state / 'bounce-head.b').write_text(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['b']) + '\n')
        (self.f.state / 'queue').write_text('b\tparked\t1\twriter\n')
        (self.f.state / 'parked.b').write_text('uncommitted\n')

    def tearDown(self):
        self.f.close()

    def repair(self, path, text):
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\nrepair\n')
        self.f.commit(self.f.lanes['b'], path, text)
        return self.f.mq('run')

    def test_changed_existing_assertion_is_parked_until_manual_add(self):
        out = self.repair('tests/test_a.py', 'assert total() == 4\n').stdout
        self.assertIn('PARKED b: repair changed existing tests or check config (tests/test_a.py)', out)
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertEqual((self.f.state / 'parked.b').read_text(), 'repair changed tests: tests/test_a.py\n')
        self.assertNotIn('LANDED', out)
        # A second pass leaves it parked: only a person's `mq add` lands it.
        self.assertNotIn('requeued', self.f.mq('run').stdout)
        self.f.mq('add', 'b')
        self.assertIn('LANDED', self.f.mq('run').stdout)

    def test_added_skip_in_a_new_test_is_parked(self):
        out = self.repair('tests/test_b.py', 'import pytest\npytestmark = pytest.mark.skip\ndef test_b(): pass\n').stdout
        self.assertIn('PARKED b: repair changed existing tests', out)

    def test_added_tests_only_are_requeued(self):
        out = self.repair('tests/test_a.py', 'assert total() == 3\nassert total([]) == 0\n').stdout
        self.assertIn('requeued b', out)
        self.assertIn('LANDED', out)

    def test_renaming_a_test_out_of_the_test_folder_is_parked(self):
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\nrepair\n')
        self.f.git('mv', 'tests/test_a.py', 'src_a.py', cwd=self.f.lanes['b'])
        self.f.git('commit', '-qm', 'move', cwd=self.f.lanes['b'])
        self.assertIn('PARKED b: repair changed existing tests or check config (tests/test_a.py)', self.f.mq('run').stdout)

    def test_changing_a_test_file_mode_is_parked(self):
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\nrepair\n')
        (self.f.lanes['b'] / 'tests/test_a.py').chmod(0o755)
        self.f.git('add', 'tests/test_a.py', cwd=self.f.lanes['b'])
        self.f.git('commit', '-qm', 'mode', cwd=self.f.lanes['b'])
        self.assertIn('PARKED b: repair changed existing tests', self.f.mq('run').stdout)

    def test_test_edits_merged_from_main_are_not_the_repair(self):
        # Someone else's reviewed test change on main reaches the lane by a merge.
        self.f.commit(self.f.repo, 'tests/test_main.py', 'assert 1\n')
        self.f.git('merge', '-q', '--no-edit', 'main', cwd=self.f.lanes['b'])
        (self.f.state / 'bounce-head.b').write_text(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['b']) + '\n')
        self.f.commit(self.f.repo, 'tests/test_main.py', 'assert 2\n')
        self.f.git('merge', '-q', '--no-edit', 'main', cwd=self.f.lanes['b'])
        out = self.repair('tests/test_new.py', 'assert new() == 1\n').stdout
        self.assertIn('requeued b', out)

    def test_custom_guard_paths_replace_the_default(self):
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\nrepair\n')
        self.f.commit(self.f.lanes['b'], 'tests/test_a.py', 'assert total() == 4\n')
        out = self.f.mq('run', env={'MQ_GUARD_PATHS': r'^golden/'}).stdout
        self.assertIn('requeued b', out)


if __name__ == '__main__':
    unittest.main()
