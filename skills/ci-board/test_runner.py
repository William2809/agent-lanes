"""ci-board: runner snapshot (fixture HOMEs), merge/ETA logic, mq lanes, unreachable runners, local server."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixtures  # noqa: E402

SNAPSHOT = os.path.join(HERE, 'remote', 'snapshot.py')


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


board = load('ci_board', os.path.join(HERE, 'bin', 'ci-board'))
snapmod = load('ci_snapshot', SNAPSHOT)


def snapshot(home, stdin=True):
    """Run snapshot.py the way ssh does (`python3 -` on stdin) against a fake HOME."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_CI_')}
    env.update(CI_BOARD_HOME=home)
    with open(SNAPSHOT, 'rb') as script:
        proc = subprocess.run([sys.executable, '-'], stdin=script, capture_output=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='ci-board-')
        # Tests write only inside this temp dir; the OS temp cleaner owns it afterwards.


class SnapshotTest(Tmp):
    def test_empty_home_has_no_runs_and_does_not_create_state(self):
        snap = snapshot(self.dir)
        self.assertFalse(snap['ci_present'])
        self.assertEqual((snap['active'], snap['finished'], snap['queue']['granted']), ([], [], 0))
        self.assertTrue(snap['queue']['readable'])
        self.assertEqual(os.listdir(self.dir), [], 'snapshot must not write runner state')
        self.assertLess(snap['took_ms'], 2000)

    def test_busy_runner_phases_queue_and_history(self):
        root = fixtures.build(self.dir, 'busy')
        snap = snapshot(os.path.join(root, 'runners', 'source'))
        phases = {(r['project'], r['phase'], r['step']) for r in snap['active']}
        self.assertIn(('billing-api', 'database', 'database'), phases)
        self.assertIn(('web-app', 'check', 'check'), phases)
        self.assertEqual(sum(r['phase'] == 'queued' for r in snap['active']), 3)
        self.assertEqual(snap['queue']['granted'], 4)
        self.assertEqual(len(snap['queue']['waiting']), 2)   # the third waits for the runner lock
        self.assertEqual([r['step'] for r in snap['active'] if r['phase'] == 'queued'].count('lock'), 1)
        self.assertIsNone(snap['lock'])
        self.assertEqual((snap['drain']['pid'], snap['drain']['at']), (1, int(fixtures.NOW - 300)))
        self.assertEqual(snap['policy'], dict(max_jobs=2, max_workers=4))
        self.assertEqual(snap['host'], 'runner-a')
        running = [r for r in snap['active'] if r['started']]
        self.assertTrue(all(r['live'] and r['elapsed_s'] >= 0 and r['sha'] for r in running))
        outcomes = [r['outcome'] for r in snap['finished']]
        self.assertEqual(len(outcomes), 25)   # 24 full checks + one --run
        self.assertEqual(set(outcomes), {'pass', 'fail', 'infra'})
        self.assertEqual(sum(r['cached'] for r in snap['finished']), 1)
        self.assertTrue(all(r['run_s'] for r in snap['finished'] if not r['cached']))
        self.assertTrue(all(r['branch'] is None for r in snap['active'] + snap['finished']))
        text = json.dumps(snap)
        self.assertNotIn('pnpm run check', text, 'command text must never leave the runner')

    def test_lost_and_stale_runs_are_not_shown_as_running(self):
        home = fixtures.Home(self.dir)
        lost = home.active('proj', 'ab' * 20, time.time() - 100, 5, [('checkout', 1)], live=False)
        stale = home.active('proj', 'cd' * 20, time.time() - 99999, 5, [('checkout', 1)], live=False)
        log = os.path.join(self.dir, 'ci', 'projects', 'proj', 'logs', stale + '.log')
        os.utime(log, (time.time() - 20000, time.time() - 20000))
        home.save()
        snap = snapshot(self.dir)
        runs = {r['run'] for r in snap['active']}
        self.assertNotIn(lost, runs)
        self.assertNotIn(stale, runs)

    def test_old_live_run_survives_age_and_history_bounds_without_reviving_dead_log(self):
        home = fixtures.Home(self.dir)
        old = time.time() - 5 * 3600
        live = home.active('proj', 'ab' * 20, old, 5, [('checkout', 1)], live=True,
                           idle_s=4 * 3600 + 60)
        dead = home.active('other', 'cd' * 20, old, 5, [('checkout', 1)], live=False,
                           idle_s=4 * 3600 + 60)
        for index in range(snapmod.HISTORY + 10):
            home.finished('proj', ('%08x' % index) + '0' * 32,
                          time.time() - index, 20, 0)
        # The same run id under another project is not made live by proj's ledger row.
        source = os.path.join(self.dir, 'ci', 'projects', 'proj', 'logs', live + '.log')
        wrong = os.path.join(self.dir, 'ci', 'projects', 'wrong', 'logs', live + '.log')
        with open(source) as stream:
            fixtures.write(wrong, stream.read(), old)
        home.save()
        snap = snapshot(self.dir)
        shown = [(row['project'], row['run']) for row in snap['active']]
        self.assertEqual(shown.count(('proj', live)), 1)
        self.assertNotIn(('other', dead), shown)
        self.assertNotIn(('wrong', live), shown)
        self.assertLessEqual(len(snap['finished']), snapmod.HISTORY)

    def test_only_current_v2_phase_and_job_deadlines_leave_the_runner(self):
        home = fixtures.Home(self.dir)
        current = home.active('proj', 'ab' * 20, time.time() - 60, 5, [('checkout', 1)])
        stale = home.active('proj', 'cd' * 20, time.time() - 50, 5, [('checkout', 1)])
        jobs = list(home.jobs.values())
        jobs[0].update(version=2, state='running', lease_generation=7, deadline=2400,
                       job_deadline=2200, admitted_at=2000,
                       process=dict(generation=7, deadline=2100, phase='check', group=42))
        jobs[1].update(version=2, state='running', lease_generation=8, deadline=2500,
                       job_deadline=2300, admitted_at=2001,
                       process=dict(generation=7, deadline=2050, phase='check', group=43))
        home.save()
        found = {row['run']: row for row in snapshot(self.dir)['active']}
        self.assertEqual((found[current]['phase_deadline'], found[current]['job_deadline'],
                          found[current]['run_deadline']), (2100, 2200, 2400))
        self.assertEqual((found[stale]['phase_deadline'], found[stale]['job_deadline'],
                          found[stale]['run_deadline']), (None, 2300, 2500))

    def test_exhausted_log_budget_uses_trusted_live_phase_without_reading(self):
        run = fixtures.run_id(time.time() - 300, 'ab' * 20, 1)
        pdir = os.path.join(self.dir, 'ci', 'projects', 'proj')
        fixtures.write(os.path.join(pdir, 'logs', run + '.log'), 'not read\n')
        fixtures.write(os.path.join(pdir, 'runs', run, 'conf'), 'REMOTE_CI_NAME=proj\n')
        qrow = dict(project='proj', run=run, live=True, workers=1, sequence=1,
                    phase_deadline=900, run_deadline=950, job_deadline=None,
                    ledger_phase='check', admitted_at=800)
        old_ci, old_now, old_read = snapmod.CI, snapmod.NOW, snapmod.READ[0]
        try:
            snapmod.CI, snapmod.NOW, snapmod.READ[0] = os.path.join(self.dir, 'ci'), 1000, snapmod.BUDGET
            with mock.patch.object(snapmod, 'read_part', side_effect=AssertionError('budget exceeded')):
                active, finished, _ = snapmod.projects([qrow], True, True)
        finally:
            snapmod.CI, snapmod.NOW, snapmod.READ[0] = old_ci, old_now, old_read
        self.assertEqual(finished, [])
        row = board.derive(dict(now=1000, policy=dict(max_jobs=1), active=active,
                                finished=[], queue=dict(active=[], waiting=[])))['running'][0]
        self.assertEqual((row['phase'], row['step'], row['started']), ('check', 'check', 800))
        self.assertEqual((row['overdue'], row['deadline_reason']), (True, 'phase'))

    def test_unreadable_pieces_degrade_instead_of_failing(self):
        home = fixtures.Home(self.dir)
        run = home.active('proj', 'ab' * 20, time.time() - 60, 5, [('checkout', 1)])
        hidden = home.active('proj', 'cd' * 20, time.time() - 50, 5, [('checkout', 1)])
        done = home.finished('proj', 'ef' * 20, time.time() - 30, 40, 0)
        home.save()
        queue = os.path.join(self.dir, '.local', 'state', 'agent-ci', 'queue.json')
        with open(queue, 'w') as f:
            f.write('{not json')
        pdir = os.path.join(self.dir, 'ci', 'projects', 'proj')
        os.chmod(os.path.join(pdir, 'logs', hidden + '.log'), 0)
        with open(os.path.join(pdir, 'results', done + '.exit'), 'w') as f:
            f.write('garbage')
        try:
            snap = snapshot(self.dir)
        finally:
            os.chmod(os.path.join(pdir, 'logs', hidden + '.log'), 0o600)
        self.assertFalse(snap['queue']['readable'])
        self.assertEqual([r['run'] for r in snap['active']], [run])  # liveness unknown: still shown
        self.assertEqual(snap['finished'][0]['outcome'], 'infra')

    def test_large_log_reads_head_and_tail_only(self):
        lines = ['== resources queued', '== resources workers=2 memory_mb=4096',
                 '== p run r sha %s slot 1/1 start %s' % ('ab' * 20, fixtures.stamp(time.time() - 500)),
                 '== timing queue 3s', '== timing checkout 1s']
        lines += ['x' * 120] * 20000   # ~2.4 MB of check output
        lines += ['== timing install 40s', '== pnpm run check', 'y' * 100]
        path = os.path.join(self.dir, 'big.log')
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        parsed = snapmod.parse_log(snapmod.read_part(path), dict(db=True, prepare=False))
        self.assertEqual(parsed['phase'], 'check')
        self.assertEqual(parsed['timings'], dict(queue=3, checkout=1, install=40))
        self.assertEqual(len(parsed['check']), 8)

    def test_phase_follows_run_sh_order(self):
        base = ['== resources queued', '== resources workers=1 memory_mb=2048',
                '== p run r sha abcdef12 slot 1/2 start 2026-10-15 10:00:00', '== timing queue 2s']
        cases = [([], 'install', 'checkout'), (['== timing checkout 1s'], 'install', 'install'),
                 (['== timing checkout 1s', '== timing install 9s'], 'database', 'database'),
                 (['== timing checkout 1s', '== timing install 9s', '== timing database 3s'], 'database', 'prepare'),
                 (['== timing checkout 1s', '== timing install 9s', '== timing database 3s', '== timing prepare 4s'], 'check', 'check'),
                 (['== timing checkout 1s', '== timing install 9s', '== timing database 3s', '== timing prepare 4s',
                   '== timing check 30s', '== timing run 47s (before cleanup)'], 'cleanup', 'cleanup')]
        for extra, phase, step in cases:
            info = snapmod.parse_log(base + extra, dict(db=True, prepare=True))
            self.assertEqual((info['phase'], info['step']), (phase, step), extra)
        self.assertEqual(snapmod.parse_log(['== resources queued'], dict(db=False, prepare=False))['phase'], 'queued')
        info = snapmod.parse_log(base + ['== timing checkout 1s', '== timing install 2s', '== bash x'],
                                 dict(db=True, prepare=False))
        self.assertEqual(info['phase'], 'check', 'a printed check command means the check started')

    def test_exclusive_is_opt_in_and_only_exclusive_runs_wait_for_the_lock(self):
        ci = os.path.join(self.dir, 'ci')
        conf = os.path.join(self.dir, 'conf')
        old, snapmod.CI = snapmod.CI, ci
        try:
            for text, new, legacy in (('REMOTE_CI_SLOTS=1\n', False, True), ('REMOTE_CI_EXCLUSIVE=1\n', True, True),
                                      ('REMOTE_CI_SLOTS=2\n', False, False), ('', False, True)):
                fixtures.write(conf, text)
                fixtures.write(os.path.join(ci, 'bin', 'lib.sh'), ': "${REMOTE_CI_EXCLUSIVE:=0}"\n')
                snapmod._OLD_RUNNER.clear()
                self.assertEqual(snapmod.conf_flags(conf)['exclusive'], new, text)
                fixtures.write(os.path.join(ci, 'bin', 'lib.sh'), ': "${REMOTE_CI_SLOTS:=1}"\n')
                snapmod._OLD_RUNNER.clear()
                self.assertEqual(snapmod.conf_flags(conf)['exclusive'], legacy, 'older runner: ' + text)
        finally:
            snapmod.CI = old
            snapmod._OLD_RUNNER.clear()
        # Waiting before host admission: an exclusive run waits for the lock, any other for its slot.
        self.assertEqual(snapmod.parse_log([], dict(db=False, prepare=False, exclusive=True))['step'], 'lock')
        self.assertEqual(snapmod.parse_log([], dict(db=False, prepare=False, exclusive=False))['step'], 'slot')
        info = snapmod.parse_log(['== resources workers=1 memory_mb=2048',
                                  '== p run r sha abcdef12 slot 1/exclusive start 2026-10-15 10:00:00'],
                                 dict(db=False, prepare=False))
        self.assertEqual((info['slot'], info['exclusive'], info['phase']), (1, True, 'install'))

    def test_parallel_slot_runs_have_no_drain_or_lock(self):
        root = fixtures.build(self.dir, 'parallel')
        snap = board.derive(snapshot(os.path.join(root, 'runners', 'source')))
        self.assertEqual((snap['drain'], snap['lock']), (None, None))
        self.assertEqual([r['project'] for r in snap['running']], ['billing-api', 'web-app', 'tools', 'search'])
        self.assertFalse(any(r['exclusive'] for r in snap['running']))
        self.assertEqual(sum(r['workers'] for r in snap['running']), snap['queue']['granted'])
        self.assertEqual([(w['step'], w.get('behind_drain')) for w in snap['waiting']], [('resources', None)])

    def test_policy_reads_only_numeric_limits_from_runner_config(self):
        fixtures.write(os.path.join(self.dir, '.config', 'agent-lanes', 'config'),
                       'SECRET=hunter2\nAGENT_CI_MAX_JOBS=3\nAGENT_CI_MAX_WORKERS=bad\n')
        old, snapmod.HOME = snapmod.HOME, self.dir
        try:
            self.assertEqual(snapmod.policy(), dict(max_jobs=3, max_workers=2))
        finally:
            snapmod.HOME = old

    def test_ps_start_matches_in_either_locale_order(self):
        self.assertEqual(snapmod.start_key('Thu Oct 15 20:28:10 2026    '), snapmod.start_key('Thu 15 Oct 20:28:10 2026'))


def snap_with(active=(), finished=(), max_jobs=1, queue=None):
    return dict(now=1000, policy=dict(max_jobs=max_jobs, max_workers=2), active=list(active),
                finished=list(finished), queue=queue or dict(active=[], waiting=[]))


def done(project, run_s, outcome='pass', ended=0, cached=False, check='c1'):
    return dict(project=project, run_s=run_s, outcome=outcome, ended=ended, cached=cached, check=check)


class DeriveTest(unittest.TestCase):
    def test_median_uses_last_ten_real_runs_of_the_same_command(self):
        finished = [done('a', 1000, ended=i) for i in range(5)] + [done('a', 100, ended=10 + i) for i in range(10)]
        finished += [done('a', 5, ended=30, cached=True), done('a', 9999, 'infra', ended=31),
                     done('a', 7, ended=32, check='other')]
        running = dict(project='a', run='r', started=900, elapsed_s=150, phase='check', check='c1')
        snap = board.derive(snap_with([running], finished))
        row = snap['running'][0]
        self.assertEqual(row['median_s'], 100)
        self.assertTrue(row['over_median'])
        self.assertEqual(row['eta_s'], 0)
        self.assertEqual(row['step_index'], 3)
        self.assertEqual(len(snap['sparks']['a']), 10)

    def test_eta_and_estimated_start_respect_job_slots(self):
        finished = [done('a', 100, ended=i) for i in range(3)] + [done('b', 40, ended=5)]
        running = dict(project='a', run='r1', started=960, elapsed_s=40, phase='check')
        waiting = [dict(project='b', run='w%d' % i, started=None, phase='queued', sequence=i, submitted=0)
                   for i in (2, 1)]
        one = board.derive(snap_with([running] + waiting, finished))
        self.assertEqual(one['running'][0]['eta_s'], 60)
        self.assertEqual([(w['run'], w['est_start_s'], w['est_known']) for w in one['waiting']],
                         [('w1', 60, True), ('w2', 100, True)])
        two = board.derive(snap_with([running] + waiting, finished, max_jobs=2))
        self.assertEqual([(w['est_start_s'], w['est_basis']) for w in two['waiting']], [(0, 'free'), (40, 'eta')])

    def test_queue_estimates_never_decrease_and_unknown_stays_unknown(self):
        finished = [done('a', 100, ended=1)]
        running = [dict(project='a', run='r1', started=0, elapsed_s=300, phase='check'),
                   dict(project='a', run='r2', started=0, elapsed_s=20, phase='check')]
        waiting = [dict(project=p, run='w%d' % i, started=None, phase='queued', sequence=i)
                   for i, p in enumerate(['a', 'new', 'a', 'a'])]
        snap = board.derive(snap_with(running + waiting, finished, max_jobs=2))
        starts = [w['est_start_s'] for w in snap['waiting']]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(snap['waiting'][0]['est_basis'], 'overdue')
        self.assertEqual([w['est_known'] for w in snap['waiting']], [True, True, True, False])

    def test_fifo_admission_keeps_later_estimates_unknown_after_an_unknown_one(self):
        running = dict(project='x', run='r', started=0, elapsed_s=20, phase='check')
        waiting = [dict(project='a', run='w%d' % i, started=None, phase='queued', sequence=i) for i in range(3)]
        snap = board.derive(snap_with([running] + waiting, [done('a', 100, ended=1)], max_jobs=2))
        self.assertEqual([(w['est_start_s'], w['est_known']) for w in snap['waiting']],
                         [(0, True), (80, False), (100, False)])

    def test_drain_owner_leads_and_later_runs_wait_behind_it(self):
        rows = [dict(project='s', run='slot-new', started=None, phase='queued', step='resources', live=True,
                     workers=0, sequence=1, submitted=500),
                dict(project='x', run='excl', started=None, phase='queued', step='lock', submitted=100),
                dict(project='s', run='slot-old', started=None, phase='queued', step='lock', submitted=50)]
        snap = snap_with(rows)
        snap['drain'] = dict(pid=9, run='excl', project='x', kind='check', at=100)
        out = board.derive(snap)
        self.assertEqual([w['run'] for w in out['waiting']], ['excl', 'slot-old', 'slot-new'])
        self.assertEqual([bool(w.get('behind_drain')) for w in out['waiting']], [False, False, True])
        # A second exclusive run waits behind the owner even when submitted before it.
        rows.append(dict(project='y', run='excl-2', started=None, phase='queued', step='lock', submitted=60, exclusive=True))
        out = board.derive(snap_with(rows) | dict(drain=snap['drain']))
        self.assertEqual([w['run'] for w in out['waiting']], ['excl', 'slot-old', 'slot-new', 'excl-2'])

    def test_unknown_repo_falls_back_and_safe_check_jobs_count(self):
        queue = dict(active=[dict(kind='local', workers=1, sequence=1, since=900)],
                     waiting=[dict(kind='local', workers=0, sequence=3, since=990)])
        snap = board.derive(snap_with([dict(project='new', run='r', started=None, phase='queued', sequence=2)],
                                      queue=queue))
        self.assertEqual(snap['fallback_s'], board.DEFAULT_RUN_S)
        self.assertEqual([r['project'] for r in snap['running']], ['safe-check'])
        self.assertEqual(snap['running'][0]['elapsed_s'], 100)
        # The running safe-check job has no history: its slot frees at an unknown time ("after #N").
        self.assertEqual([(r['project'], r['est_start_s'], r['est_known']) for r in snap['waiting']],
                         [('new', 500, False), ('safe-check', 1100, False)])


class LanesTest(Tmp):
    def test_landing_queued_and_recent_landed(self):
        now = time.time()
        os.makedirs(os.path.join(self.dir, 'repo'))
        os.makedirs(os.path.join(self.dir, 'claims'))
        with open(os.path.join(self.dir, 'repo', 'queue'), 'w') as f:
            f.write('l1\tready\t0\t\nl2\tready\t0\t\nl3\tbounced\t1\tfix\n')
        with open(os.path.join(self.dir, 'repo', 'events'), 'w') as f:
            f.write('%d\tl1\tlanding\tx\n%d\tl4\tlanded\tx\n%d\tl5\tlanded\tx\nbad line\n' % (now - 5, now - 60, now - 7200))
        lanes = board.mq_lanes(self.dir, now)
        self.assertEqual([(l['lane'], l['state']) for l in lanes],
                         [('l1', 'landing'), ('l2', 'queued'), ('l3', 'bounced'), ('l4', 'landed')])


class RunnerTest(Tmp):
    def test_config_routes(self):
        specs = board.runners(dict(CI_RUNNERS='source=box-a:unit pc=box-b:wol:db:browser here=local'))
        self.assertEqual([(s['name'], s['routes'], s['wakeable'], s['roles']) for s in specs],
                         [('source', [['box-a']], False, ['unit tests']),
                          ('pc', [['box-b']], True, ['database', 'browser']), ('here', ['local'], False, [])])
        self.assertEqual(board.runners(dict(REMOTE_CI_HOST='local'))[0]['routes'], ['local'])
        spec = board.runners(dict(REMOTE_CI_HOST='tail', REMOTE_CI_DIRECT_HOST='192.0.2.2', REMOTE_CI_HOSTKEY_ALIAS='tb',
                                  REMOTE_CI_USER='ci'))[0]
        self.assertEqual(spec['routes'], [['-o', 'HostKeyAlias=tb', 'ci@192.0.2.2'], ['ci@tail']])

    def test_fake_unreachable_runner_over_ssh(self):
        runner = board.Runner(dict(name='gone', routes=[['-o', 'ConnectTimeout=1', 'nonexistent.invalid']],
                                   wakeable=False))
        runner.poll(timeout=15)
        view = runner.view()
        self.assertEqual(view['state'], 'unreachable')
        self.assertIsNotNone(view['down_since'])
        self.assertIsNone(view['snapshot'])

    def test_asleep_then_recovers_keeping_down_since_until_ok(self):
        root = fixtures.build(self.dir, 'unreachable')
        specs = {s['name']: s for s in board.fixture_runners(root)}
        pc = board.Runner(specs['pc'])
        pc.poll()
        first = pc.view()['down_since']
        pc.poll()
        self.assertEqual((pc.view()['state'], pc.view()['down_since']), ('asleep', first))
        self.assertEqual(board.Runner(specs['source']).view()['state'], 'connecting')
        fixtures.build(os.path.join(self.dir, 'up'), 'calm')
        pc.spec = dict(pc.spec, routes=[['home:' + os.path.join(self.dir, 'up', 'runners', 'source')]])
        pc.poll()
        view = pc.view()
        self.assertEqual((view['state'], view['down_since']), ('ok', None))
        self.assertTrue(view['snapshot']['history'])

    def test_json_output_merges_runners_and_lanes(self):
        root = fixtures.build(self.dir, 'parallel')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            board.main(['--fixtures', root, '--json'])
        merged = json.loads(out.getvalue())
        self.assertEqual([(r['name'], r['state']) for r in merged['runners']], [('source', 'ok'), ('pc', 'asleep')])
        snap = merged['runners'][0]['snapshot']
        self.assertEqual([(r['project'], r['who']) for r in snap['running']],
                         [('billing-api', 'payfix'), ('web-app', 'mq landing cards'), ('tools', 'docs-ui'), ('search', None)])
        self.assertEqual([r['who'] for r in snap['waiting']], ['search-ui'])
        tools = snap['running'][2]
        self.assertEqual((tools['rss_mb'], tools['reserved_mb'], tools['peak_mb'], tools['phase_peaks']),
                         (2630, 2048, 2630, {'install': 2630}))
        self.assertEqual(snap['services'], dict(postgres=372, vm=834, vm_size_mb=4096))
        self.assertEqual(merged['lanes'][0]['state'], 'landing')


class HostActivityTest(Tmp):
    def test_parallel_jobs_report_workers_and_usage(self):
        root = fixtures.build(self.dir, 'busy')
        snap = snapshot(os.path.join(root, 'runners', 'source'))
        admitted = snap['queue']['active']
        self.assertEqual((len(admitted), snap['policy']['max_jobs']), (2, 2))
        self.assertTrue(all(r['workers'] == 2 and r['rss_mb'] > 0 and r['cpu_pct'] > 0 for r in admitted))
        running = [r for r in snap['active'] if r['started']]
        self.assertTrue(all(r['rss_mb'] > 0 for r in running))

    def test_group_usage_sums_a_real_process_group(self):
        child = subprocess.Popen(['sleep', '30'], start_new_session=True)
        try:
            rss, cpu = snapmod.group_usage(dict(owner=-1, group=child.pid), snapmod.processes())
        finally:
            child.kill()
            child.wait()
        self.assertGreaterEqual(rss, 0)
        self.assertIsNotNone(cpu)
        self.assertEqual(snapmod.group_usage(dict(owner=-1, group=child.pid), None), (None, None))

    def test_containers_are_listed_and_matched_to_running_checks(self):
        root = fixtures.build(self.dir, 'busy')
        c = snapshot(os.path.join(root, 'runners', 'source'))['containers']
        self.assertEqual(c['state'], 'ok')
        by = {x['name']: x for x in c['items']}
        self.assertEqual(by['billing-api-ci-s1-postgres']['owner'], 'billing-api')
        self.assertEqual((by['billing-api-ci-s1-postgres']['mem_mb'], by['billing-api-ci-s1-postgres']['cpu_pct']), (412.5, 14.2))
        self.assertEqual((by['tc-postgres-8f2a']['owner'], by['tc-postgres-8f2a']['test']), (None, True))
        self.assertEqual((by['redis-cache']['owner'], by['redis-cache']['test']), (None, False))
        self.assertEqual(c['items'][0]['name'], 'billing-api-ci-s1-postgres', 'check-owned containers first')

    def test_docker_stopped_or_absent_is_quiet(self):
        calm = fixtures.build(os.path.join(self.dir, 'calm'), 'calm')
        self.assertEqual(snapshot(os.path.join(calm, 'runners', 'source'))['containers'], dict(state='stopped', items=[]))
        empty = fixtures.build(os.path.join(self.dir, 'empty'), 'empty')
        self.assertEqual(snapshot(os.path.join(empty, 'runners', 'source'))['containers'], dict(state='absent', items=[]))

    def test_one_off_runs_are_told_apart_and_kept_out_of_repo_medians(self):
        root = fixtures.build(self.dir, 'busy')
        snap = board.derive(snapshot(os.path.join(root, 'runners', 'source')))
        kinds = {(r['project'], r['run_s']): r['kind'] for r in snap['finished']}
        self.assertEqual(kinds[('tools', 300)], 'run')
        self.assertEqual({k for (p, _), k in kinds.items() if p != 'tools'}, {'check'})
        self.assertNotIn(300, snap['sparks']['tools'])
        self.assertLess(snap['medians']['tools'], 100)

    def test_run_sh_processes_reveal_run_kind_and_lock_holder(self):
        script = os.path.join(self.dir, 'run.sh')
        with open(script, 'w') as f:
            f.write('sleep 30\n')
        full = subprocess.Popen(['bash', script, 'proj', 'abc', 'tree', 'RUN-FULL', '', '0', 'runner'])
        once = subprocess.Popen(['bash', script, 'proj', 'abc', 'tree', 'RUN-ONCE', 'Zm9v', '0', 'runner'])
        try:
            time.sleep(0.3)
            snapmod.processes()
            self.assertEqual((snapmod.RUNSH['RUN-FULL']['kind'], snapmod.RUNSH['RUN-ONCE']['kind']), ('check', 'run'))
            fixtures.write(os.path.join(self.dir, 'ci', 'lock'), '%d\n' % once.pid)
            old, snapmod.CI = snapmod.CI, os.path.join(self.dir, 'ci')
            try:
                self.assertEqual(snapmod.lock_holder(), dict(pid=once.pid, run='RUN-ONCE', project='proj', kind='run',
                                                             exclusive=False))
            finally:
                snapmod.CI = old
        finally:
            for p in (full, once):
                p.kill()
                p.wait()

    def test_docker_sizes(self):
        self.assertEqual([snapmod.size_mb(x) for x in ('412.5MiB ', '1.5GiB', '980kB', 'n/a')], [412.5, 1536.0, 1.0, None])

    def test_admitted_job_waiting_for_a_slot_counts_as_active(self):
        rows = [dict(project='a', run='r1', started=None, phase='queued', step='slot', live=True, workers=2, sequence=1),
                dict(project='b', run='r2', started=None, phase='queued', step='resources', live=True, workers=0, sequence=2)]
        snap = board.derive(snap_with(rows, [done('a', 100, ended=1)]))
        self.assertEqual([(r['run'], r['admitted']) for r in snap['running']], [('r1', True)])
        self.assertEqual([(w['run'], w['est_start_s'], w['est_known']) for w in snap['waiting']], [('r2', 100, True)])


class ServerTest(Tmp):
    def test_serves_page_and_json_on_loopback_only(self):
        root = fixtures.build(self.dir, 'calm')
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        b = board.Board(board.fixture_runners(root), os.path.join(root, 'mq'))
        out = io.StringIO()

        def run():
            with contextlib.redirect_stdout(out):
                board.serve(b, port, 0.2, False)
        threading.Thread(target=run, daemon=True).start()
        url = 'http://127.0.0.1:%d' % port
        for _ in range(50):
            try:
                page = urllib.request.urlopen(url + '/', timeout=15).read()
                break
            except OSError:
                time.sleep(0.1)
        self.assertIn(b'<title>CI board</title>', page)
        nonce = re.search(br"const BOARD_NONCE = '([^']+)'", page)[1].decode()
        self.assertNotEqual(nonce, '__CI_BOARD_NONCE__')
        self.assertIn(b'<title>CI board</title>', urllib.request.urlopen(url + '/', timeout=15).read())
        for _ in range(50):
            data = json.load(urllib.request.urlopen(url + '/api/snapshot', timeout=15))
            if data['runners'][0]['state'] == 'ok':
                break
            time.sleep(0.1)
        self.assertEqual(data['runners'][0]['state'], 'ok')
        request = urllib.request.Request(url + '/api/snapshot', headers={'Host': 'evil.example'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=15)
        self.assertEqual(caught.exception.code, 403)
        body = json.dumps({'run': 'run', 'source': 'source', 'destination': 'pc'}).encode()
        bad = urllib.request.Request(url + '/api/transfer', data=body, method='POST', headers={
            'Content-Type': 'application/json', 'Origin': url, 'X-CI-Board-Nonce': 'wrong'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(bad, timeout=15)
        self.assertEqual(caught.exception.code, 403)
        authenticated = urllib.request.Request(url + '/api/transfer', data=body, method='POST', headers={
            'Content-Type': 'application/json', 'Origin': url, 'X-CI-Board-Nonce': nonce})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(authenticated, timeout=15)
        self.assertEqual(caught.exception.code, 409)  # authenticated, but this run is not waiting
        self.assertIn('http://127.0.0.1:%d/' % port, out.getvalue())


if __name__ == '__main__':
    unittest.main()


def procs_of(rows):
    """pid -> (pgid, start, rss_kb, cpu, ppid) from (pid, ppid, pgid, rss_mb) rows."""
    return {pid: (pgid, '', rss * 1024, 1.0, ppid) for pid, ppid, pgid, rss in rows}


class CheckMemoryTest(unittest.TestCase):
    def test_tree_counts_every_descendant_whatever_its_group(self):
        procs = procs_of([(10, 1, 10, 4),          # run.sh
                          (11, 10, 11, 1),         # nice -> its own group
                          (12, 11, 12, 1),         # /usr/bin/time -l
                          (13, 12, 13, 900),       # bash -c <check>
                          (14, 13, 13, 700),       # node worker
                          (20, 1, 10, 5000),       # reused pgid, not a descendant
                          (30, 1, 30, 300)])       # orphan: its parent died, launchd adopted it
        self.assertEqual(snapmod.tree_usage(10, procs), (1606, 5.0))
        self.assertEqual(snapmod.tree_usage(99, procs), (None, None), 'dead run.sh: no number')
        self.assertEqual(snapmod.tree_usage(1, procs), (None, None), 'never sum launchd')
        procs[10] = (10, '', 4096, 1.0, 14)       # a ppid cycle must not hang
        self.assertEqual(snapmod.tree_usage(10, procs)[0], 1606)

    def test_peaks_per_run_and_phase_survive_polls_and_drop_with_the_run(self):
        runner = board.Runner(dict(name='m', routes=[], wakeable=False))
        poll = lambda rss, phase: runner.track_peaks(dict(active=[dict(run='r1', rss_mb=rss, phase=phase)]))
        poll(500, 'install')
        poll(1800, 'check')
        poll(1200, 'check')
        self.assertEqual(runner.peaks['r1'], {'all': 1800, 'install': 500, 'check': 1800})
        out = board.with_peaks(dict(running=[dict(run='r1')]), runner.peaks)['running'][0]
        self.assertEqual((out['peak_mb'], out['phase_peaks']), (1800, {'install': 500, 'check': 1800}))
        runner.track_peaks(dict(active=[]))
        self.assertEqual(runner.peaks, {})


class WhoTest(Tmp):
    RUN = '20261016-004500-abcdef12-%d-0123abcd'

    def who(self, table, cwd=lambda pid: 'ciports'):
        logs = os.path.join(self.dir, 'logs')
        fixtures.write(os.path.join(logs, 'agent-lanes', 'ciports2.log.run'), 'dir=/x\nworker_pid=200\nsupervisor_pid=199\n')
        fixtures.write(os.path.join(logs, 'agent-lanes', 'ciports2.1016-004023.48396.log.run'), 'worker_pid=300\n')
        calls = []
        return board.Who(logs=logs, table=lambda: calls.append(1) or table, cwd=cwd), calls

    def label(self, who, pid, landing=None):
        rows = [dict(run=self.RUN % pid, project='agent-lanes')]
        who.annotate(rows, landing or {})
        return rows[0]['who']

    def test_worker_match_mq_landing_worktree_and_dead_client(self):
        table = {500: (400, 'bash /u/bin/remote-ci check'), 400: (200, 'node agent'), 200: (199, 'codex'),
                 199: (1, 'supervisor'),
                 600: (550, 'bash remote-ci check'), 550: (540, 'sh /tmp/mq.x/bin/mq run'), 540: (200, 'zsh'),
                 700: (300, 'bash remote-ci check'), 300: (1, 'archived worker'),
                 800: (1, 'vim notes.txt')}
        who, calls = self.who(table)
        self.assertEqual(self.label(who, 500), 'ciports2')
        self.assertEqual(self.label(who, 600, {'agent-lanes': 'kopb'}), 'mq landing kopb')
        self.assertEqual(self.label(who, 700), 'ciports', 'archived metadata is skipped; cwd folder instead')
        self.assertIsNone(self.label(who, 900), 'client gone: no label')
        self.assertIsNone(self.label(who, 800), 'a reused pid that is not remote-ci is ignored')

    def test_label_is_looked_up_once_per_run(self):
        who, calls = self.who({500: (200, 'bash remote-ci check')})
        rows = [dict(run=self.RUN % 500, project='agent-lanes')]
        who.annotate(rows, {})
        who.annotate(rows, {})
        self.assertEqual((rows[0]['who'], len(calls)), ('ciports2', 1))

    def test_mq_landing_lane_comes_from_the_last_landing_line(self):
        log = os.path.join(self.dir, 'run.log')
        fixtures.write(log, 'landing kopb\nBOUNCED kopb to kopb: preflight rc=1\nlanding fe044\n')
        self.assertEqual(board.last_landing(log), 'fe044')
        self.assertIsNone(board.last_landing(os.path.join(self.dir, 'missing.log')))
