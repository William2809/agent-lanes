"""Typed outcomes: real Git/mq/land, with local runner and fixer substitutes."""
import unittest
from pathlib import Path
from workflow_fixture import ROOT, WorkflowFixture


class TypedOutcomes(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(('a', 'b'))
        self.addCleanup(self.f.close)
        for lane in self.f.lanes:
            self.f.commit(self.f.lanes[lane], lane + '.txt', lane + '\n')
        self.f.env.pop('LAND_CHECK', None)
        self.f.shim('land', f'#!/bin/sh\nexec sh "{ROOT / "skills/wt-dev/bin/land"}" "$@" --keep\n')
        self.f.shim('migcheck', '#!/bin/sh\nexit 0\n')
        self.f.shim('remote-ci', '#!/bin/sh\n[ "$1" = check ] || exit 1\necho "== finished rc=$RUNNER_RC fixture"\nexit "$RUNNER_RC"\n')
        self.f.shim('wk', '#!/bin/sh\necho "$*" >>"$FIXTURE_ROOT/fixers"\ncat >/dev/null\nmkdir -p "$HOME/.claude/state/logs/demo"\nprintf "tokens used\\n1\\n" >"$HOME/.claude/state/logs/demo/$1.log"\n')

    def assert_no_fixer(self):
        self.assertFalse((self.f.root / 'fixers').exists())
        self.assertNotIn('\tbounced\t', (self.f.state / 'events').read_text())

    def test_runner_infra_codes_never_become_check_failures(self):
        """Requirement 2: reserved and unknown errors restore the lane and name infra."""
        before = self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['a'])
        for rc in (2, 3, 74, 76, 127, 143, 255, 42):
            with self.subTest(rc=rc):
                result = self.f.run('land', 'a', check=False, env={'RUNNER_RC': str(rc)})
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stdout.splitlines()[-1],
                                 r'^FAILED infra .* on [0-9a-f]+ \(main\.\.[0-9a-f]+\)$')
                self.assertEqual(self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['a']), before)

    def test_runner_error_without_a_verdict_is_infra(self):
        """Requirement 2: a legacy client can exit 1 before it runs any checks."""
        self.f.shim('remote-ci', '#!/bin/sh\nexit 1\n')
        result = self.f.run('land', 'a', check=False)
        self.assertIn('FAILED infra runner failed without a verdict', result.stdout)

    def test_infra_keeps_solo_and_train_queued_without_bounces(self):
        """Requirement 3: no split, bounce, or fixer for infrastructure outcomes."""
        self.f.mq('add', 'a', 'b')
        before = self.f.queue()
        for size in ('1', '2'):
            for rc in ('3', '74', '76'):
                with self.subTest(size=size, rc=rc):
                    result = self.f.mq('run', check=False, env={'RUNNER_RC': rc, 'MQ_TRAIN': size})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('lanes stay queued', result.stdout)
                    self.assertEqual(self.f.queue(), before)
                    self.assert_no_fixer()

    def test_legacy_infra_and_busy_lines_keep_queue(self):
        """Compatibility: old FAILED check codes and exhausted busy still stop safely."""
        self.f.mq('add', 'a', 'b')
        before = self.f.queue()
        for size in ('1', '2'):
            for line in ('FAILED check rc=3 on abcdef0 (main..abcdef0)',
                         'FAILED check rc=2 on abcdef0 (main..abcdef0)',
                         'FAILED check rc=74 on abcdef0 (main..abcdef0)',
                         'FAILED check rc=76 on abcdef0 (main..abcdef0)',
                         'FAILED runner busy for an hour', 'FAILED interrupted (TERM)',
                         'FAILED target moved (main checkout left main during the check)'):
                with self.subTest(size=size, line=line):
                    self.f.shim('land', '#!/bin/sh\necho "' + line + '"\nexit 1\n')
                    result = self.f.mq('run', check=False, env={'MQ_TRAIN': size})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.f.queue(), before)
                    self.assert_no_fixer()

    def test_unknown_failure_parks_without_a_fixer(self):
        """Requirement 3: an unknown result is neither a pass nor a coding request."""
        self.f.mq('add', 'a', 'b')
        self.f.shim('land', '#!/bin/sh\necho "FAILED unknown outcome"\nexit 1\n')
        self.f.mq('run', env={'MQ_TRAIN': '2'})
        self.assertEqual([row[1:3] for row in self.f.queue()], [['parked', '0'], ['parked', '0']])
        self.assert_no_fixer()

    def test_malformed_check_and_contradictory_success_park(self):
        """Requirement 3: unknown output cannot become a verdict, even with LANDED text."""
        for line, rc in (('FAILED check rc=invalid', 1), ('LANDED fixture', 1),
                         ('unexpected result', 0)):
            with self.subTest(line=line, rc=rc):
                self.f.mq('add', 'a')
                self.f.shim('land', f'#!/bin/sh\necho "{line}"\nexit {rc}\n')
                self.f.mq('run')
                self.assertEqual(self.f.queue()[0][1:3], ['parked', '0'])
                self.assert_no_fixer()

    def test_review_summary_retains_verdict_after_sixty_one_failures(self):
        """Review 1: summary limits must not hide a candidate verdict from land."""
        logs = self.f.root / 'summary'
        (logs / 'logs').mkdir(parents=True)
        (logs / 'results').mkdir()
        (logs / 'logs/red.log').write_text(''.join(f'FAIL case-{i}\n' for i in range(61)) +
                                          '== check-result rc=2\n== finished rc=1 fixture\n')
        (logs / 'results/red.exit').write_text('1\n')
        client = (ROOT / 'skills/remote-ci/bin/remote-ci').read_text()
        body = client.split('if [ "$summary" = 1 ]; then\n', 1)[1].split('\nfi\nwait_remote ', 1)[0]
        script = ('#!/bin/bash\np="$FIXTURE_ROOT/summary"; run=red\n'
                  'await_file() { [ -f "$1" ]; }; export -f await_file\n'
                  'wait_remote() { /bin/bash -c "$1"; }\n') + body
        self.f.shim('remote-ci', script)
        result = self.f.run('remote-ci', 'check', check=False)
        self.assertEqual(result.stdout.splitlines()[-2:],
                         ['== check-result rc=2', '== finished rc=1 fixture'])
        self.assertEqual(sum(line.startswith('FAIL ') for line in result.stdout.splitlines()), 60)
        result = self.f.run('land', 'a', check=False)
        self.assertIn('FAILED check rc=1 on ', result.stdout)
        (logs / 'logs/red.log').write_text(''.join(f'FAIL case-{i}\n' for i in range(61)) +
                                          'runner cleanup pending: red\n== check-result rc=0\n== finished rc=76 fixture\n')
        (logs / 'results/red.exit').write_text('76\n')
        self.f.mq('add', 'a')
        self.f.mq('run', check=False)
        status = self.f.run(str(ROOT / 'bin/lanes'), check=False)
        self.assertIn('runner cleanup pending: red', status.stdout)

    def test_review_install_tool_failure_is_infra(self):
        """Review 5: missing installer must not launch a fixer; bad dependencies still do."""
        original = (self.f.shims / 'wt-dev').read_text()
        self.f.shim('wt-dev', original.replace("elif cmd == 'install': pass",
            "elif cmd == 'install': sys.exit(int(os.environ['INSTALL_RC']))"))
        for rc in ('127', '126', '1'):
            with self.subTest(rc=rc):
                result = self.f.run('land', 'a', check=False, env={'INSTALL_RC': rc})
                expected = 'FAILED install on ' if rc == '1' else 'FAILED infra install tool'
                self.assertIn(expected, result.stdout)
        self.f.mq('add', 'a')
        self.f.mq('run', check=False, env={'INSTALL_RC': '127'})
        self.assertEqual(self.f.queue()[0][1:3], ['ready', '0'])
        self.assert_no_fixer()

    def test_review_infra_stop_returns_nonzero(self):
        """Review 7: automation must see an unlanded infra stop as unsuccessful."""
        self.f.mq('add', 'a', 'b')
        for size in ('1', '2'):
            result = self.f.mq('run', check=False, env={'RUNNER_RC': '74', 'MQ_TRAIN': size})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual([row[1:3] for row in self.f.queue()], [['ready', '0'], ['ready', '0']])

    def test_review_lanes_names_pending_cleanup(self):
        """Review 8: the attention view must name the cleanup that stopped the queue."""
        self.f.shim('remote-ci', '#!/bin/sh\necho "runner cleanup pending: cleanup-run"\nexit 76\n')
        self.f.mq('add', 'a')
        self.f.mq('run', check=False)
        result = self.f.run(str(ROOT / 'bin/lanes'), check=False)
        self.assertIn('runner cleanup pending: cleanup-run', result.stdout)
        self.assert_no_fixer()

    def test_real_red_still_requests_a_fixer(self):
        """Requirement 3: rc 1 remains a candidate failure and uses the bounce path."""
        self.f.mq('add', 'a')
        self.f.mq('run', env={'RUNNER_RC': '1'})
        self.assertTrue((self.f.root / 'fixers').exists())
        self.assertEqual(self.f.queue()[0][2], '1')
        self.assertIn('check rc=1', (self.f.state / 'events').read_text())
        self.assertIn('\tbounced\t', (self.f.state / 'events').read_text())


    def test_custom_check_verdict_codes_request_a_fixer(self):
        """Release review: land marks a custom check's rc 2 or 127 as the candidate's; mq bounces it."""
        for lane, rc in (('a', '2'), ('b', '127')):
            with self.subTest(rc=rc):
                self.f.mq('add', lane)
                self.f.mq('run', env={'LAND_CHECK': 'exit ' + rc, 'MQ_TRAIN': '1'})
                events = (self.f.state / 'events').read_text()
                self.assertIn(f'check rc={rc} ', events)
                self.assertIn(f'{lane}\tbounced\t', events)
        self.assertEqual(len((self.f.root / 'fixers').read_text().splitlines()), 2)

    def test_push_result_lines_reach_the_run_log(self):
        """Release review: a refused LAND_PUSH_BRANCHES push is shown, solo and in a train."""
        self.f.shim('land', '#!/bin/sh\necho "push FAILED (origin/main, land kept): rejected"\n'
                            'echo "LANDED abc1234 on main"\n')
        self.f.mq('add', 'a')
        result = self.f.mq('run', env={'MQ_TRAIN': '1'})
        self.assertIn('a: push FAILED (origin/main, land kept): rejected', result.stdout)
        self.f.commit(self.f.lanes['b'], 'b2.txt', 'b2\n')
        self.f.commit(self.f.lanes['a'], 'a2.txt', 'a2\n')
        self.f.mq('add', 'a', 'b')
        result = self.f.mq('run', env={'MQ_TRAIN': '2'})
        self.assertIn('train: push FAILED (origin/main, land kept): rejected', result.stdout)


if __name__ == '__main__':
    unittest.main()
