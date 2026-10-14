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
