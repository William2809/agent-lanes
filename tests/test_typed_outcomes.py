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

    def fresh_infra_submission(self):
        # Each classification case is independent of the three-outcome history.
        for lane in self.f.lanes:
            self.f.mq('drop', lane)
        self.f.mq('add', *self.f.lanes)

    def test_raw_preflight_three_is_candidate(self):
        """Review 3: untyped local exit 3 requests a fixer.

        Boundary: legacy land preflight line. Gap: land/fixer are stubs.
        """
        self.f.mq('add', 'a')
        self.f.shim('land', '#!/bin/sh\necho "FAILED preflight rc=3 on abcdef0 (main..abcdef0)"\nexit 1\n')
        self.f.mq('run')
        self.assertTrue((self.f.root / 'fixers').exists())
        self.assertIn('\tbounced\t', (self.f.state / 'events').read_text())

    def test_repeated_infra_parks_same_head(self):
        """Review 3: three consecutive infra results park the same lane head.

        Boundary: two queued train attempts then park both lanes on third.
        Gap: runner and fixer are stubs; no real runner outage.
        """
        self.f.mq('add', 'a', 'b')
        for attempt in range(1, 4):
            result = self.f.mq('run', check=False, env={'RUNNER_RC': '74', 'MQ_TRAIN': '2'})
            self.assertNotEqual(result.returncode, 0)
            expected = 'ready' if attempt < 3 else 'parked'
            self.assertEqual([row[1] for row in self.f.queue()], [expected, expected])
        self.assertIn('3 consecutive infra outcomes', result.stdout)
        for lane, path in self.f.lanes.items():
            head = self.f.git('rev-parse', 'HEAD', cwd=path)
            self.assertIn(f'3 consecutive infra outcomes on {head}',
                          (self.f.state / ('parked.' + lane)).read_text())
        self.assert_no_fixer()

    def test_new_head_resets_infra_count(self):
        """Review 3: infra count belongs to the lane head.

        Boundary: two failures then a new commit; third new-head result parks.
        Gap: runner stub; no actual infrastructure failure.
        """
        self.f.mq('add', 'a')
        for _ in range(2):
            self.f.mq('run', check=False, env={'RUNNER_RC': '74'})
        self.f.commit(self.f.lanes['a'], 'next.txt', 'next\n')
        for attempt in range(1, 4):
            self.f.mq('run', check=False, env={'RUNNER_RC': '74'})
            self.assertEqual(self.f.queue()[0][1], 'ready' if attempt < 3 else 'parked')
        self.assert_no_fixer()

    def test_untyped_custom_three_is_candidate(self):
        """Review 3: an untyped local check exit 3 is a candidate failure.

        Boundary: LAND_CHECK hook, real land. Gap: check/fixer are stubs.
        """
        self.f.mq('add', 'a')
        self.f.mq('run', env={'LAND_CHECK': 'exit 3'})
        self.assertTrue((self.f.root / 'fixers').exists())
        self.assertIn('check rc=3', (self.f.state / 'events').read_text())

    def test_noninfra_outcome_resets_infra_count(self):
        """Review 3: infra outcomes must be consecutive before parking.

        Boundary: interruption after two infra results resets the streak.
        Gap: land/fixer are stubs, no real interruption or runner outage.
        """
        self.f.mq('add', 'a')
        infra = '#!/bin/sh\necho "FAILED infra fixture runner"\nexit 1\n'
        self.f.shim('land', infra)
        for _ in range(2):
            self.f.mq('run', check=False)
        self.f.shim('land', '#!/bin/sh\necho "FAILED interrupted (TERM)"\nexit 1\n')
        self.f.mq('run', check=False)
        self.f.shim('land', infra)
        for attempt in range(1, 4):
            self.f.mq('run', check=False)
            self.assertEqual(self.f.queue()[0][1], 'ready' if attempt < 3 else 'parked')
        self.assert_no_fixer()

    def test_preflight_unreachable_marker_keeps_queue(self):
        """Review 3: the runner's own unreachable marker is typed infra.

        Boundary: rc 3 with no finished line, real land then legacy output.
        Gap: runner/legacy land/fixer are stubs; no real network outage.
        """
        preflight = self.f.repo / 'tools/land-preflight.sh'
        preflight.parent.mkdir(exist_ok=True)
        preflight.write_text('#!/bin/sh\necho "remote-ci: runner unreachable" >&2\nexit 3\n')
        preflight.chmod(0o755)
        self.f.git('add', '--', 'tools/land-preflight.sh')
        self.f.git('commit', '-qm', 'fixture unreachable preflight')
        self.f.mq('add', 'a')
        self.f.mq('run', check=False)
        self.assertEqual(self.f.queue()[0][1:3], ['ready', '0'])
        self.f.shim('land', '#!/bin/sh\necho "remote-ci: runner unreachable"\necho "FAILED preflight rc=3 on abcdef0 (main..abcdef0)"\nexit 1\n')
        self.f.mq('run', check=False)
        self.assertEqual(self.f.queue()[0][1:3], ['ready', '0'])
        self.assert_no_fixer()

    def test_preflight_reserved_codes_stop_without_bounce(self):
        """Brief 1: runner preflight errors keep solo/train lanes queued.

        Boundary: reserved low codes and execution-range endpoints.
        Gap: preflight returns a stub runner code; no runner outage is induced.
        """
        preflight = self.f.repo / 'tools/land-preflight.sh'
        preflight.parent.mkdir(exist_ok=True)
        preflight.write_text('#!/bin/sh\ncase "$PREFLIGHT_RC" in 74|75) ;; '
                             '*) echo "== finished rc=$PREFLIGHT_RC fixture" ;; esac\n'
                             'exit "$PREFLIGHT_RC"\n')
        preflight.chmod(0o755)
        self.f.git('add', '--', 'tools/land-preflight.sh')
        self.f.git('commit', '-qm', 'fixture runner preflight')
        self.f.mq('add', 'a', 'b')
        before = self.f.queue()
        heads = {lane: self.f.git('rev-parse', 'HEAD', cwd=path)
                 for lane, path in self.f.lanes.items()}
        for size in ('1', '2'):
            for rc in (3, 74, 75, 76, 126, 127, 128, 199, 200, 255):
                with self.subTest(size=size, rc=rc):
                    self.fresh_infra_submission()
                    result = self.f.mq('run', check=False,
                                       env={'PREFLIGHT_RC': str(rc), 'MQ_TRAIN': size})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('lanes stay queued', result.stdout)
                    self.assertEqual(self.f.queue(), before)
                    self.assert_no_fixer()
                    for lane, path in self.f.lanes.items():
                        self.assertEqual(self.f.git('rev-parse', 'HEAD', cwd=path), heads[lane])

    def test_legacy_preflight_reserved_codes_stop_without_bounce(self):
        """Brief 1: mq also handles preflight output from an older land.

        Boundary: solo/train, runner marker without land's infra prefix.
        Gap: land is a stub.
        """
        self.f.mq('add', 'a', 'b')
        before = self.f.queue()
        for size in ('1', '2'):
            for rc in (3, 74, 75, 76, 126, 255):
                with self.subTest(size=size, rc=rc):
                    self.fresh_infra_submission()
                    marker = '' if rc in (74, 75) else f'echo "== finished rc={rc} fixture"\n'
                    self.f.shim('land', f'#!/bin/sh\n{marker}echo "FAILED preflight rc={rc} on abcdef0 (main..abcdef0)"\nexit 1\n')
                    result = self.f.mq('run', check=False, env={'MQ_TRAIN': size})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('lanes stay queued', result.stdout)
                    self.assertEqual(self.f.queue(), before)
                    self.assert_no_fixer()

    def test_install_runner_failure_stops_without_bounce(self):
        """Brief 1: every runner-backed step preserves reserved infrastructure codes.

        Boundary: dependency install returns 74 before preflight; train stays queued.
        Gap: install is a runner stub, not a real dependency installation.
        """
        path = self.f.shims / 'wt-dev'
        path.write_text(path.read_text().replace("elif cmd == 'install': pass",
                                               "elif cmd == 'install': sys.exit(74)"))
        self.f.mq('add', 'a', 'b')
        before = self.f.queue()
        result = self.f.mq('run', check=False, env={'MQ_TRAIN': '2'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('lanes stay queued', result.stdout)
        self.assertEqual(self.f.queue(), before)
        self.assert_no_fixer()

    def test_preflight_candidate_failure_still_bounces(self):
        """Brief 1: ordinary preflight rc 1 stays a coding failure.

        Boundary: adjacent candidate class. Gap: land and fixer are stubs.
        """
        self.f.mq('add', 'a')
        self.f.shim('land', '#!/bin/sh\necho "FAILED preflight rc=1 on abcdef0 (main..abcdef0)"\nexit 1\n')
        self.f.mq('run')
        self.assertTrue((self.f.root / 'fixers').exists())
        self.assertIn('\tbounced\t', (self.f.state / 'events').read_text())

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
                    self.fresh_infra_submission()
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
                    self.fresh_infra_submission()
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
