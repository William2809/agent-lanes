"""Policy boundary: real Git/mq/land and config loading; heavy checks use local substitutes."""
import json
from pathlib import Path
import shutil
import unittest
from workflow_fixture import ROOT, WorkflowFixture


class PolicyBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(())
        self.addCleanup(self.f.close)
        for key in ('LAND_CHECK', 'REMOTE_CI_CONF', 'MQ_GUARD_CONFIG', 'MQ_GUARD_PATHS'):
            self.f.env.pop(key, None)
        node = shutil.which('node')
        if node:
            self.f.env['PATH'] += ':' + str(Path(node).parent)
        self.f.shim('land', f'#!/bin/sh\nexec sh "{ROOT / "skills/wt-dev/bin/land"}" "$@" --keep\n')
        self.f.shim('migcheck', '#!/bin/sh\nexit 0\n')
        self.f.shim('remote-ci', '#!/bin/sh\necho remote-ci >>"$FIXTURE_ROOT/check.calls"\nexit 0\n')
        self.f.shim('pnpm', '#!/bin/sh\necho pnpm >>"$FIXTURE_ROOT/check.calls"\nexit 0\n')

    def lane(self):
        lane = self.f.add_lane('candidate')
        self.f.commit(lane, 'feature.txt', 'feature\n')
        return lane

    def no_op(self):
        self.f.commit(self.f.repo, 'package.json', '{"scripts":{"check:remote":"pnpm test"}}\n')
        lane = self.lane()
        self.f.commit(lane, 'package.json', '{"scripts":{"check:remote":"true"}}\n')
        return lane

    def run_queue(self):
        self.f.mq('add', 'candidate')
        return self.f.mq('run').stdout

    def assert_parked(self, before, out):
        self.assertIn('PARKED candidate: check policy changed: review', out)
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertEqual(self.f.git('rev-parse', 'main'), before)
        self.assertNotIn('LANDED', out)
        self.assertFalse((self.f.root / 'check.calls').exists())

    def test_first_no_op_check_script_is_parked(self):
        """R2: first scripts change parks before checks or landing; no remote runner covered."""
        self.no_op()
        self.assert_parked(self.f.git('rev-parse', 'main'), self.run_queue())
        self.assertNotIn('LANDED', self.f.mq('run').stdout)

    def test_manual_add_after_policy_review_lands(self):
        """R2: a second manual add releases the reviewed head; human review is assumed."""
        lane = self.no_op()
        self.assert_parked(self.f.git('rev-parse', 'main'), self.run_queue())
        self.f.mq('add', 'candidate')
        self.assertIn('LANDED', self.f.mq('run').stdout)
        self.assertEqual(self.f.git('rev-parse', 'main'), self.f.git('rev-parse', 'HEAD', cwd=lane))
        self.assertEqual((self.f.root / 'check.calls').read_text(), 'pnpm\n')

    def test_new_commit_after_manual_review_needs_review_again(self):
        """R2: review covers one head, not later policy edits; concurrency is not covered."""
        lane = self.no_op()
        self.assert_parked(self.f.git('rev-parse', 'main'), self.run_queue())
        self.f.mq('add', 'candidate')
        self.f.commit(lane, 'package.json', '{"scripts":{"check:remote":"exit 0"}}\n')
        self.assert_parked(self.f.git('rev-parse', 'main'), self.f.mq('run').stdout)

    def test_first_submission_can_add_and_change_tests(self):
        """R2: feature tests may be added or changed on first submission; test quality is not checked."""
        self.f.commit(self.f.repo, 'tests/test_old.py', 'assert 1 == 1\n')
        lane = self.lane()
        self.f.commit(lane, 'tests/test_old.py', 'assert 2 == 2\n')
        self.f.commit(lane, 'tests/test_new.py', 'assert 3 == 3\n')
        self.assertIn('LANDED', self.run_queue())
        self.assertEqual(self.f.git('show', 'main:tests/test_new.py'), 'assert 3 == 3')

    def test_first_submission_policy_files_are_parked(self):
        """R2: added check/CI configs and preflight park; path list is representative."""
        for path in ('.remote-ci.conf', '.remote-ci.conf', '.wt-dev.conf', 'tools/land-preflight.sh',
                     '.github/workflows/check.yml', '.gitlab-ci.yml', 'vitest.config.ts', 'pytest.ini',
                     '.eslintrc.json', 'biome.json'):
            with self.subTest(path=path):
                f = WorkflowFixture(())
                self.addCleanup(f.close)
                lane = f.add_lane('candidate')
                f.commit(lane, path, 'candidate policy\n')
                before = f.git('rev-parse', 'main')
                f.mq('add', 'candidate')
                out = f.mq('run').stdout
                self.assertIn('check policy changed: review', out)
                self.assertEqual(f.queue()[0][1], 'parked')
                self.assertEqual(f.git('rev-parse', 'main'), before)
                self.assertNotIn('LANDED', out)

    def test_baseline_changes_do_not_trigger_first_policy_guard(self):
        """R2: baseline files remain allowed on first submission; baseline generation is not run."""
        lane = self.lane()
        self.f.commit(lane, 'tools/file-size-baseline.json', '{}\n')
        self.assertIn('LANDED', self.run_queue())

    def test_python_dependency_files_are_not_check_policy(self):
        """R2: dependency-only pyproject.toml/setup.cfg edits land on first submission (review P2)."""
        lane = self.lane()
        self.f.commit(lane, 'pyproject.toml', '[project]\ndependencies = ["requests"]\n')
        self.f.commit(lane, 'setup.cfg', '[metadata]\nversion = 2\n')
        self.assertIn('LANDED', self.run_queue())

    def test_package_metadata_is_not_check_policy(self):
        """R2: unchanged scripts with new metadata land; dependency execution is stubbed."""
        self.f.commit(self.f.repo, 'package.json', '{"scripts":{"test":"check"},"version":"1"}\n')
        lane = self.lane()
        self.f.commit(lane, 'package.json', '{"scripts":{"test":"check"},"version":"2"}\n')
        self.assertIn('LANDED', self.run_queue())

    def test_target_only_policy_change_does_not_park_lane(self):
        """R2: target policy updates are not candidate changes; conflicting updates are not covered."""
        self.lane()
        self.f.commit(self.f.repo, 'pytest.ini', '[pytest]\n')
        self.assertIn('LANDED', self.run_queue())

    def real_remote_ci(self):
        self.f.shim('remote-ci', f'#!/bin/sh\nexec bash "{ROOT / "skills/remote-ci/bin/remote-ci"}" "$@"\n')

    def test_land_ignores_candidate_remote_ci_conf(self):
        """R1: real config loading selects main and rejects its red check; runner is not contacted."""
        self.f.commit(self.f.repo, '.remote-ci.conf', 'echo "FAIL main policy"\nexit 19\n')
        lane = self.lane()
        self.f.commit(lane, '.remote-ci.conf', 'echo candidate >"$FIXTURE_ROOT/candidate-conf"\nexit 0\n')
        self.real_remote_ci()
        before = self.f.git('rev-parse', 'main')
        out = self.f.run('land', 'candidate', check=False)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn('FAIL main policy', out.stdout)
        # main's config ran (and failed while loading: a config/infra outcome, not a verdict).
        self.assertIn('FAIL main policy', out.stdout)
        self.assertIn('FAILED infra unknown runner outcome rc=19', out.stdout)
        self.assertFalse((self.f.root / 'candidate-conf').exists())
        self.assertEqual(self.f.git('rev-parse', 'main'), before)

    def test_missing_main_conf_does_not_fall_back_to_candidate(self):
        """R1: absent main config fails closed; normal runner startup is not covered."""
        lane = self.lane()
        self.f.commit(lane, '.remote-ci.conf', 'echo candidate >"$FIXTURE_ROOT/candidate-conf"\nexit 0\n')
        self.real_remote_ci()
        out = self.f.run('land', 'candidate', check=False)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        # A missing main config is a configuration (infra) failure, never the candidate's config.
        self.assertIn('FAILED infra configuration rc=2', out.stdout)
        self.assertFalse((self.f.root / 'candidate-conf').exists())

    def test_normal_remote_ci_still_loads_repo_conf(self):
        """R1: direct remote-ci keeps its repo config; commands after loading are not covered."""
        lane = self.lane()
        self.f.commit(lane, '.remote-ci.conf', 'echo "repo config loaded"\nexit 23\n')
        self.real_remote_ci()
        out = self.f.run('remote-ci', 'info', cwd=lane, check=False)
        self.assertEqual(out.returncode, 23)
        self.assertIn('repo config loaded', out.stdout)

    def test_land_selects_check_from_main_package(self):
        """R1: candidate script presence cannot select the check; script bodies are stubbed."""
        for main_scripts, candidate_scripts, expected in (
                ({}, {'check:remote': 'true'}, 'remote-ci\n'),
                ({'check:remote': 'pnpm test'}, {}, 'pnpm\n')):
            with self.subTest(expected=expected):
                self.f.commit(self.f.repo, 'package.json', json.dumps({'scripts': main_scripts}) + '\n')
                name = 'with-script' if not main_scripts else 'without-script'
                lane = self.f.add_lane(name)
                self.f.commit(lane, 'package.json', json.dumps({'scripts': candidate_scripts}) + '\n')
                out = self.f.run('land', name)
                self.assertIn('LANDED', out.stdout)
                self.assertEqual((self.f.root / 'check.calls').read_text().splitlines()[-1] + '\n', expected)


    def test_custom_check_remote_failure_is_a_verdict(self):
        """R: a check:remote that is not remote-ci has no runner contract, so its failures stay the
        candidate's (bounce to a fixer); only reserved runner codes mean infrastructure."""
        self.f.commit(self.f.repo, 'package.json', json.dumps({'scripts': {'check:remote': 'vitest run'}}) + '\n')
        for code, expected in (('1', 'FAILED check rc=1 '), ('2', 'FAILED check rc=2 '),
                               ('74', 'FAILED infra runner failure rc=74 ')):
            with self.subTest(code=code):
                self.f.shim('pnpm', '#!/bin/sh\necho " FAIL  src/a.test.ts"\nexit %s\n' % code)
                name = 'custom-' + code
                self.f.commit(self.f.add_lane(name), name + '.txt', code + '\n')
                out = self.f.run('land', name, check=False)
                self.assertEqual(out.returncode, 1, out.stdout)
                self.assertIn(expected, out.stdout.splitlines()[-1] + ' ')

    def test_check_kind_comes_from_main_script_not_output(self):
        """R: output cannot change the classification (v0.8.4 review): a remote-ci check that fails
        before printing anything is infra; a custom script printing runner-like lines stays a verdict."""
        cases = (('remote-ci check', '', '1', 'FAILED infra runner failed without a verdict rc=1 '),
                 ('vitest run', 'echo "== finished rc=2 fixture"\n', '2', 'FAILED check rc=2 '))
        for script, output, code, expected in cases:
            with self.subTest(script=script):
                self.f.commit(self.f.repo, 'package.json', json.dumps({'scripts': {'check:remote': script}}) + '\n')
                self.f.shim('pnpm', '#!/bin/sh\n%sexit %s\n' % (output, code))
                name = 'kind-' + code
                self.f.commit(self.f.add_lane(name), name + '.txt', code + '\n')
                out = self.f.run('land', name, check=False)
                self.assertIn(expected, out.stdout.splitlines()[-1] + ' ')

if __name__ == '__main__':
    unittest.main()
