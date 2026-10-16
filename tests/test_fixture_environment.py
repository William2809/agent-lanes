"""Regression: standalone fixture records must not inherit the host launch lock."""
import os
import unittest
from unittest.mock import patch
from workflow_fixture import ROOT, WorkflowFixture


class FixtureEnvironmentTests(unittest.TestCase):
    def test_standalone_record_ignores_host_launch_lock(self):
        # Reproduced: three run-record assertions failed before reaching their
        # behavior checks because the host's launch_lock reached this fixture.
        with patch.dict(os.environ, {'launch_lock': '/nonexistent/fixture-host-launch.lock'}):
            fixture = WorkflowFixture(())
        self.addCleanup(fixture.close)
        scripts = ROOT / 'skills/agent-workers/scripts'
        log = fixture.home / 'record.log'
        result = fixture.run('sh', '-c',
                             '. "$1/state.sh"; . "$1/run-record.sh"; tool_rev=fixture; '
                             'ops_run_record "$2" build codex codex gpt-6.1-sol medium workspace-write "$3" ""',
                             'sh', str(scripts), str(log), str(fixture.repo), check=False)
        # Do not attach env or captured output to an assertion; it can contain host values.
        self.assertEqual(result.returncode, 0)
        self.assertTrue(fixture.home.joinpath('record.log.run').exists())

    def test_resume_ignores_host_lane_constraint(self):
        # Reproduced: host WK_RESUME_DIR rejected valid isolated fixture resumes.
        with patch.dict(os.environ, {'WK_RESUME_DIR': '/nonexistent/host-lane'}):
            fixture = WorkflowFixture()
        self.addCleanup(fixture.close)
        fixture.worker('resume', 'a', 'danger-full-access')
        result = fixture.wk('resume', '-r', 'continue', check=False)
        self.assertEqual(result.returncode, 0)
        fixture.wait_worker('resume')
        self.assertEqual(len(fixture.calls()), 1)

    def test_resume_reads_its_message_from_a_file(self):
        # Reproduced: `wk NAME -r -f FILE` passed -f to `codex exec resume`, which refused it.
        fixture = WorkflowFixture()
        self.addCleanup(fixture.close)
        fixture.worker('resume', 'a', 'danger-full-access')
        brief = fixture.root / 'brief.md'
        brief.write_text('fix the review findings\n')
        result = fixture.wk('resume', '-r', '-f', str(brief), check=False)
        self.assertEqual(result.returncode, 0)
        fixture.wait_worker('resume')
        self.assertEqual(fixture.calls()[-1]['prompt'], 'fix the review findings')
        missing = fixture.wk('resume', '-r', '-f', str(fixture.root / 'missing.md'), check=False)
        self.assertEqual(missing.returncode, 2)
