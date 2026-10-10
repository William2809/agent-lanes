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

    def test_appended_check_override_is_parked(self):
        # Review of v0.8.2, finding 1: one added line replaced the effective check command.
        self.f.commit(self.f.lanes['b'], '.remote-ci.conf', "REMOTE_CI_CHECK='exit 7'\n")
        self.f.commit(self.f.repo, 'main.txt', 'main\n')  # keep main moving
        (self.f.state / 'bounce-head.b').write_text(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['b']) + '\n')
        out = self.repair('.remote-ci.conf', "REMOTE_CI_CHECK='exit 7'\nREMOTE_CI_CHECK='true'\n").stdout
        self.assertIn('PARKED b: repair changed existing tests or check config (.remote-ci.conf)', out)

    def test_changed_package_scripts_are_parked(self):
        # land runs scripts["check:remote"], which can call any other script.
        self.f.commit(self.f.lanes['b'], 'package.json', '{"scripts": {"check:remote": "pnpm test"}}\n')
        (self.f.state / 'bounce-head.b').write_text(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['b']) + '\n')
        out = self.repair('package.json', '{"scripts": {"check:remote": "true"}}\n').stdout
        self.assertIn('package.json (scripts)', out)
        self.assertIn('PARKED b', out)

    def test_rebased_copy_of_a_pre_bounce_test_change_is_not_the_repair(self):
        # Review of v0.8.2, finding 4: the lane changed a test before it bounced, then rebased.
        self.f.commit(self.f.lanes['b'], 'tests/test_a.py', 'assert total() == 4\n', 'lane test change')
        (self.f.state / 'bounce-head.b').write_text(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['b']) + '\n')
        self.f.commit(self.f.repo, 'main.txt', 'main\n')
        self.f.git('rebase', '-q', 'main', cwd=self.f.lanes['b'])
        out = self.repair('src.txt', 'source fix\n').stdout
        self.assertIn('requeued b', out)

    def test_rebase_over_nearby_main_change_keeps_pre_bounce_commits_out(self):
        # Review of v0.8.3 branch: main edits lines near the lane's test change, so the rebased
        # commit has new context lines (and a new full patch ID).
        body = ''.join(f'assert f({i}) == {i}\n' for i in range(6))
        self.f.commit(self.f.repo, 'tests/test_near.py', body)
        self.f.git('merge', '-q', '--no-edit', 'main', cwd=self.f.lanes['b'])
        self.f.commit(self.f.lanes['b'], 'tests/test_near.py', body.replace('f(1) == 1', 'f(1) == 10'), 'lane test change')
        (self.f.state / 'bounce-head.b').write_text(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['b']) + '\n')
        self.f.commit(self.f.repo, 'tests/test_near.py', body.replace('f(3) == 3', 'f(3) == 30'))
        self.f.git('rebase', '-q', 'main', cwd=self.f.lanes['b'])
        self.assertIn('requeued b', self.repair('src.txt', 'source fix\n').stdout)

    def test_check_change_inside_a_merge_resolution_is_parked(self):
        self.f.commit(self.f.repo, 'main.txt', 'main\n')
        lane = self.f.lanes['b']
        self.f.git('merge', '-q', '--no-commit', 'main', cwd=lane)
        (lane / '.remote-ci.conf').write_text("REMOTE_CI_CHECK='true'\n")
        self.f.git('add', '.remote-ci.conf', cwd=lane)
        self.f.git('commit', '-q', '--no-edit', cwd=lane)
        out = self.repair('src.txt', 'source fix\n').stdout
        self.assertIn('.remote-ci.conf (merge resolution)', out)

    def test_merge_that_keeps_the_lane_side_of_a_check_is_parked(self):
        # Review of v083: -X ours drops main's stricter check; the merge result equals a parent.
        lane = self.f.lanes['b']
        self.f.commit(self.f.repo, '.remote-ci.conf', "REMOTE_CI_CHECK='pnpm test'\n")
        self.f.git('merge', '-q', '--no-edit', 'main', cwd=lane)
        self.f.commit(lane, '.remote-ci.conf', "REMOTE_CI_CHECK='pnpm test --fast'\n")
        (self.f.state / 'bounce-head.b').write_text(self.f.git('rev-parse', 'HEAD', cwd=lane) + '\n')
        self.f.commit(self.f.repo, '.remote-ci.conf', "REMOTE_CI_CHECK='pnpm test --strict'\n")
        self.f.git('merge', '-q', '--no-edit', '-X', 'ours', 'main', cwd=lane)
        out = self.repair('src.txt', 'source fix\n').stdout
        self.assertIn('.remote-ci.conf', out)
        self.assertIn('PARKED b', out)

    def test_unreadable_bounce_baseline_parks(self):
        # Review of v0.8.2, finding 7: a pruned baseline let a test-weakening repair through.
        (self.f.state / 'bounce-head.b').write_text('0' * 40 + '\n')
        out = self.repair('tests/test_a.py', 'assert total() == 4\n').stdout
        self.assertIn('PARKED b: repair changed existing tests or check config ((guard check failed: cannot inspect the repair', out)

    def test_bounce_baseline_is_kept_by_a_ref(self):
        # Lane a's first land fails in the fixture, so it bounces to its worker.
        self.f.mq('claim', 'a', 'a.txt')
        self.f.commit(self.f.lanes['a'], 'a.txt', 'a\n')
        self.f.worker('writer', 'a', 'danger-full-access')
        self.f.mq('add', 'a')
        self.assertIn('BOUNCED a', self.f.mq('run').stdout)
        head = (self.f.state / 'bounce-head.a').read_text().strip()
        self.assertEqual(self.f.git('rev-parse', 'refs/mq/bounce/a'), head)
        # The resumed worker keeps its name; its record says the queue owns its report (finding 6).
        self.f.wait_worker('writer')
        run = (self.f.home / '.claude/state/logs/demo/writer.log.run').read_text()
        self.assertIn('\nowner=mq\n', run)

    def test_custom_guard_paths_replace_the_default(self):
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\nrepair\n')
        self.f.commit(self.f.lanes['b'], 'tests/test_a.py', 'assert total() == 4\n')
        out = self.f.mq('run', env={'MQ_GUARD_PATHS': r'^golden/'}).stdout
        self.assertIn('requeued b', out)

    def bounce_here(self):
        lane = self.f.lanes['b']
        (self.f.state / 'bounce-head.b').write_text(self.f.git('rev-parse', 'HEAD', cwd=lane) + '\n')

    def test_same_edit_to_another_assertion_is_the_repair(self):
        # Review of v0.8.3, finding 1: identical edits at two places share a context-free patch ID.
        lane = self.f.lanes['b']
        self.f.commit(lane, 'tests/test_a.py', 'assert total() == 3\n\nx = 1\n\nassert total() == 3\n')
        self.f.commit(lane, 'tests/test_a.py', 'assert total() >= 0\n\nx = 1\n\nassert total() == 3\n')
        self.bounce_here()
        out = self.repair('tests/test_a.py', 'assert total() >= 0\n\nx = 1\n\nassert total() >= 0\n').stdout
        self.assertIn('PARKED b: repair changed existing tests or check config (tests/test_a.py)', out)

    def test_rebase_that_absorbs_part_of_a_lane_commit_keeps_its_test_change(self):
        # Review of v0.8.3, finding 4: main already has the source half of a lane commit.
        lane = self.f.lanes['b']
        (lane / 'app.py').write_text('def total(): return 4\n')
        (lane / 'tests/test_a.py').write_text('assert total() == 4\n')
        self.f.git('add', '-A', cwd=lane)
        self.f.git('commit', '-qm', 'total is 4', cwd=lane)
        self.bounce_here()
        self.f.commit(self.f.repo, 'app.py', 'def total(): return 4\n')
        self.f.git('rebase', '-q', 'main', cwd=lane)
        self.assertEqual(self.f.git('diff', (self.f.state / 'bounce-head.b').read_text().strip(), 'HEAD',
                                    '--', 'tests/test_a.py', cwd=lane), '')
        self.assertIn('requeued b', self.repair('src.txt', 'source fix\n').stdout)

    def test_custom_guard_paths_keep_posix_ere_syntax(self):
        # Review of v0.8.3, finding 2: MQ_GUARD_PATHS has always been an awk ERE.
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\nrepair\n')
        self.f.commit(self.f.lanes['b'], 'tests/test_a.py', 'assert total() >= 0\n')
        out = self.f.mq('run', env={'MQ_GUARD_PATHS': r'^tests/test_[[:alpha:]]+\.py$'}).stdout
        self.assertIn('PARKED b: repair changed existing tests or check config (tests/test_a.py)', out)

    def test_invalid_guard_pattern_parks_with_the_reason(self):
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\nrepair\n')
        out = self.f.mq('run', env={'MQ_GUARD_PATHS': '('}).stdout
        self.assertIn('PARKED b', out)
        self.assertIn('MQ_GUARD_PATHS', (self.f.state / 'guarded.b').read_text())
    def test_rebase_conflict_outside_tests_is_not_the_repair(self):
        # The replayed pre-bounce work conflicts in app.py only; the lane resolved it by hand.
        lane = self.f.lanes['b']
        self.f.commit(lane, 'app.py', 'lane\n')
        self.bounce_here()
        self.f.commit(self.f.repo, 'app.py', 'main\n')
        self.f.run('git', 'rebase', '-q', 'main', cwd=lane, check=False)
        (lane / 'app.py').write_text('main\nlane\n')
        self.f.git('add', 'app.py', cwd=lane)
        self.f.run('git', '-c', 'core.editor=true', 'rebase', '--continue', cwd=lane)
        self.assertIn('requeued b', self.repair('src.txt', 'source fix\n').stdout)
    def test_rebase_conflict_in_a_test_parks(self):
        # Git could not replay the lane's test change; the hand resolution needs a person.
        lane = self.f.lanes['b']
        self.f.commit(lane, 'tests/test_a.py', 'assert total() == 4\n')
        self.bounce_here()
        self.f.commit(self.f.repo, 'tests/test_a.py', 'assert total() == 5\n')
        step = self.f.run('git', 'rebase', '-q', 'main', cwd=lane, check=False)
        for _ in range(3):  # each replayed lane commit stops on the test file
            if not step.returncode:
                break
            (lane / 'tests/test_a.py').write_text('assert total() >= 0\n')
            self.f.git('add', 'tests/test_a.py', cwd=lane)
            step = self.f.run('git', '-c', 'core.editor=true', 'rebase', '--continue', cwd=lane, check=False)
        self.assertEqual(step.returncode, 0)
        out = self.repair('src.txt', 'source fix\n').stdout
        self.assertIn('PARKED b: repair changed existing tests or check config (tests/test_a.py)', out)
    def test_modify_delete_replay_conflict_on_check_config_parks(self):
        # Review of v0.8.3 (round 2), finding 1: no markers; the lane kept a config file main deleted.
        lane = self.f.lanes['b']
        self.f.commit(self.f.repo, 'pytest.ini', '[pytest]\n')
        self.f.git('merge', '-q', '--no-edit', 'main', cwd=lane)
        self.f.commit(lane, 'pytest.ini', '[pytest]\naddopts = -q\n')
        self.bounce_here()
        self.f.git('rm', '-q', 'pytest.ini', cwd=self.f.repo)
        self.f.git('commit', '-qm', 'config moved', cwd=self.f.repo)
        step = self.f.run('git', 'rebase', '-q', 'main', cwd=lane, check=False)
        self.assertNotEqual(step.returncode, 0)
        self.f.git('add', 'pytest.ini', cwd=lane)
        self.f.run('git', '-c', 'core.editor=true', 'rebase', '--continue', cwd=lane)
        out = self.repair('src.txt', 'source fix\n').stdout
        self.assertIn('PARKED b: repair changed existing tests or check config (pytest.ini)', out)

    def test_test_name_with_a_vertical_tab_stays_guarded(self):
        # Review of v0.8.3 (round 2), finding 4: splitlines() split the name awk matched.
        lane = self.f.lanes['b']
        self.f.commit(lane, 'tests/test_\vtotal.py', 'assert total() == 3\n')
        self.bounce_here()
        out = self.repair('tests/test_\vtotal.py', 'assert total() >= 0\n').stdout
        self.assertIn('PARKED b', out)

    def test_directory_split_conflict_over_a_test_below_an_ordinary_directory_parks(self):
        # Review of v0.8.3 round 3, finding 2: Git names only "src"; the test below it was missed.
        lane = self.f.lanes['b']
        for name in ('one', 'two', 'three', 'four'):
            self.f.commit(self.f.repo, f'src/{name}.py', f'{name} = 1\n')
        self.f.git('merge', '-q', '--no-edit', 'main', cwd=lane)
        self.f.commit(lane, 'src/test_total.py', 'assert total() == 3\n')
        self.bounce_here()
        for name, side in (('one', 'left'), ('two', 'left'), ('three', 'right'), ('four', 'right')):
            (self.f.repo / side).mkdir(exist_ok=True)
            self.f.git('mv', f'src/{name}.py', f'{side}/{name}.py', cwd=self.f.repo)
        self.f.git('commit', '-qm', 'split src', cwd=self.f.repo)
        step = self.f.run('git', 'rebase', '-q', 'main', cwd=lane, check=False)
        for _ in range(3):  # keep the test where the lane put it
            if not step.returncode:
                break
            self.f.git('add', '-A', cwd=lane)
            step = self.f.run('git', '-c', 'core.editor=true', 'rebase', '--continue', cwd=lane, check=False)
        self.assertEqual(step.returncode, 0)
        self.assertTrue((lane / 'src/test_total.py').exists())
        out = self.repair('app.py', 'def total(): return 3\n').stdout
        self.assertIn('PARKED b', out)
        self.assertIn('src/test_total.py', (self.f.state / 'guarded.b').read_text())

    def test_added_skip_in_a_test_with_a_quoted_name_parks(self):
        # Review of v0.8.3 round 3, finding 3: Git quotes "b/tests/test_\303\251.py" in patch headers.
        lane = self.f.lanes['b']
        self.f.commit(lane, 'tests/test_\u00e9.py', 'import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n')
        self.bounce_here()
        out = self.repair('tests/test_\u00e9.py', 'import unittest\nclass T(unittest.TestCase):\n'
                          '    @unittest.skip("later")\n    def test_a(self): pass\n').stdout
        self.assertIn('PARKED b', out)

    def test_added_skip_on_a_line_starting_with_plus_plus_parks(self):
        # Review of v0.8.3 round 4, finding 3: "+++skipped; t.skip(...)" read as a file header.
        lane = self.f.lanes['b']
        self.f.commit(lane, 'tests/total.test.js', 'let skipped = 0\ntest("total", t => {\n  assert(total() === 3)\n})\n')
        self.bounce_here()
        out = self.repair('tests/total.test.js', 'let skipped = 0\ntest("total", t => {\n'
                          '++skipped; t.skip("temporarily disabled"); return;\n  assert(total() === 3)\n})\n').stdout
        self.assertIn('PARKED b', out)

    def test_added_skip_after_a_form_feed_parks(self):
        lane = self.f.lanes['b']
        self.f.commit(lane, 'tests/test_ff.py', 'import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n')
        self.bounce_here()
        out = self.repair('tests/test_ff.py', 'import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n'
                          '        x = 1\x0cself.skipTest("later")\n        pass\n').stdout
        self.assertIn('PARKED b', out)


class ReplayConflictParseTests(unittest.TestCase):
    """`git merge-tree -z --name-only` output; Git documents conflicts with no conflicted-file entry."""

    def setUp(self):
        from importlib.machinery import SourceFileLoader
        from workflow_fixture import ROOT
        self.guard = SourceFileLoader('mq_guard', str(ROOT / 'bin/mq-guard.py')).load_module()

    def test_clean_replay_has_no_conflicts(self):
        self.assertEqual(self.guard.replay_conflicts('OID\0'), set())

    def test_message_only_directory_conflict_is_guarded(self):
        out = 'OID\0\0' + '1\0tests\0CONFLICT (directory rename split)\0msg\0' + '1\0app\0Auto-merging\0msg\0'
        conflicts = self.guard.replay_conflicts(out)
        self.assertNotIn('app', conflicts)
        self.assertEqual(self.guard.guarded_conflicts(conflicts), {'tests'})


if __name__ == '__main__':
    unittest.main()
