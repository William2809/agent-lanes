"""ci-board: job liveness under WSL clock drift, stalled checks and their wait reason, asleep :wol runners."""
import json
import os
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixtures  # noqa: E402
from test_runner import Tmp, board, procs_of, snapmod  # noqa: E402


def lstart(pid, shift=0):
    """ps lstart of a pid moved by `shift` seconds, as ps prints it after WSL corrects its clock."""
    text = subprocess.run(['env', 'LC_ALL=C', 'ps', '-p', str(pid), '-o', 'lstart='],
                          capture_output=True, text=True).stdout
    when = time.mktime(time.strptime(' '.join(text.split()), '%a %b %d %H:%M:%S %Y')) + shift
    return time.strftime('%a %b %d %H:%M:%S %Y', time.localtime(when))


class DriftTest(Tmp):
    def setUp(self):
        super().setUp()
        self.script = os.path.join(self.dir, 'run.sh')
        fixtures.write(self.script, 'sleep 30\n')
        self.run_id = fixtures.run_id(time.time() - 60, 'ab' * 20, 1)
        self.procs = []
        self.old = snapmod.QUEUE
        snapmod.QUEUE = os.path.join(self.dir, 'queue.json')

    def tearDown(self):
        snapmod.QUEUE = self.old
        for p in self.procs:
            p.kill()
            p.wait()

    def start(self, args):
        proc = subprocess.Popen(args)
        self.procs.append(proc)
        return proc.pid

    def live(self, jobs):
        fixtures.write(snapmod.QUEUE, json.dumps(dict(sequence=1, jobs=jobs)))
        time.sleep(0.3)
        return {r['run'] or r['sequence']: r['live'] for r in snapmod.queue(snapmod.processes())['rows']}

    def test_run_sh_owner_stays_live_when_ps_start_moved_59_seconds(self):
        pid = self.start(['bash', self.script, 'proj', 'ab' * 20, 'tree', self.run_id, '', '0', 'runner'])
        time.sleep(0.3)
        job = dict(owner=pid, start=lstart(pid, -59), sequence=1, workers=2, memory_mb=4096)
        self.assertEqual(self.live({'proj-%s-%d' % (self.run_id, pid): job}), {self.run_id: True})

    def test_moved_start_without_a_matching_run_sh_is_not_live(self):
        other = self.start(['sleep', '30'])   # alive, but not run.sh of this run
        runsh = self.start(['bash', self.script, 'proj', 'ab' * 20, 'tree', 'OTHER-RUN', '', '0', 'runner'])
        time.sleep(0.3)
        rows = self.live({'proj-%s-%d' % (self.run_id, other): dict(owner=other, start=lstart(other, -59), sequence=1),
                          'proj-%s-%d' % (fixtures.run_id(time.time(), 'cd' * 20, 2), runsh):
                              dict(owner=runsh, start=lstart(runsh, -59), sequence=2)})
        self.assertEqual(set(rows.values()), {False}, 'an existing pid alone never makes a job live')

    def test_watcher_with_its_recorded_start_keeps_the_job(self):
        watcher = self.start(['sleep', '30'])
        time.sleep(0.3)
        job = dict(owner=999999, start='Thu Jan  1 00:00:00 2026', watcher=watcher, watcher_start=lstart(watcher),
                   sequence=1)
        self.assertEqual(self.live({'proj-%s-999999' % self.run_id: job}), {self.run_id: True})

    def test_v2_rows_finished_drop_out_and_a_process_group_keeps_a_running_one(self):
        member = self.start(['python3', '-c', 'import os, time; os.setpgid(0, 0); time.sleep(30)'])
        time.sleep(0.3)
        old = 'Thu Jan  1 00:00:00 2026'
        done = dict(version=2, state='finished', owner=999998, start=old, sequence=1, process=dict(group=member))
        running = dict(version=2, state='running', owner=999999, start=old, sequence=2, process=dict(group=member))
        other = fixtures.run_id(time.time(), 'cd' * 20, 2)
        rows = self.live({'proj-%s-999998' % other: done, 'proj-%s-999999' % self.run_id: running})
        self.assertEqual(rows, {self.run_id: True})

    def test_stale_marker_group_is_ignored_once_the_row_has_a_tree_record(self):
        member = self.start(['python3', '-c', 'import os, time; os.setpgid(0, 0); time.sleep(30)'])
        marker = os.path.join(self.dir, 'job.stop')
        fixtures.write(marker, str(member))
        time.sleep(0.3)
        job = dict(owner=999999, start='Thu Jan  1 00:00:00 2026', sequence=1, marker=marker,
                   tree_marker=os.path.join(self.dir, 'missing.tree'))
        self.assertEqual(self.live({'proj-%s-999999' % self.run_id: job}), {self.run_id: False})


class StallTest(Tmp):
    def view(self, scenario='stalls'):
        root = fixtures.build(self.dir, scenario)
        views = {}
        for spec in board.fixture_runners(root):
            runner = board.Runner(spec)
            runner.poll()
            views[spec['name']] = runner.view()
        return views

    def test_quiet_run_with_no_cpu_is_suspected_with_its_wait_and_idle_time(self):
        running = {r['project']: r for r in self.view()['source']['snapshot']['running']}
        stalled = running['billing-api']
        self.assertEqual((stalled['suspected_stall'], stalled['overdue'], stalled['wait'], stalled['phase']),
                         (True, False, 'cache', 'database'))
        self.assertGreaterEqual(stalled['log_idle_s'], 715)

    def test_run_with_a_growing_log_is_not_stalled(self):
        progressing = {r['project']: r for r in self.view()['source']['snapshot']['running']}['web-app']
        self.assertFalse(progressing['suspected_stall'])
        self.assertFalse(progressing['overdue'])
        self.assertLess(progressing['log_idle_s'], 60)

    def test_busy_cpu_or_unknown_cpu_is_never_stalled(self):
        base = dict(project='a', run='r1', started=900, elapsed_s=100, phase='check', step='check', live=True,
                    log_idle_s=1800)
        rows = [dict(base, run='quiet', cpu_pct=0.4), dict(base, run='busy', cpu_pct=95.0),
                dict(base, run='unknown', cpu_pct=None), dict(base, run='fresh', cpu_pct=0.0, log_idle_s=30)]
        snap = board.derive(dict(now=1000, policy=dict(max_jobs=4), active=rows, finished=[],
                                 queue=dict(active=[], waiting=[])))
        self.assertEqual({r['run']: r['suspected_stall'] for r in snap['running']},
                         dict(quiet=True, busy=False, unknown=False, fresh=False))

    def test_deadline_overdue_is_distinct_from_quiet_suspicion_and_ignores_cpu(self):
        base = dict(project='a', started=900, elapsed_s=100, phase='check', step='check', live=True,
                    log_idle_s=1800, cpu_pct=95.0)
        rows = [dict(base, run='within', phase_deadline=1100),
                dict(base, run='over', phase_deadline=990),
                dict(base, run='job', phase_deadline=1200, job_deadline=980)]
        snap = board.derive(dict(now=1000, policy=dict(max_jobs=4), active=rows, finished=[],
                                 queue=dict(active=[], waiting=[])))
        found = {r['run']: r for r in snap['running']}
        self.assertEqual((found['within']['overdue'], found['within']['suspected_stall']), (False, False))
        self.assertEqual((found['over']['overdue'], found['over']['deadline_reason']), (True, 'phase'))
        self.assertEqual((found['job']['overdue'], found['job']['deadline_reason']), (True, 'fast'))

    def test_normal_overall_deadline_and_cleanup_grace(self):
        base = dict(project='a', started=800, elapsed_s=200, phase='check', step='check', live=True,
                    log_idle_s=0, cpu_pct=90.0, phase_deadline=1100, run_deadline=990)
        rows = [dict(base, run='normal'),
                dict(base, run='cleanup-grace', phase='cleanup', step='cleanup'),
                dict(base, run='cleanup-phase-over', phase='cleanup', step='cleanup', phase_deadline=980)]
        snap = board.derive(dict(now=1000, policy=dict(max_jobs=4), active=rows, finished=[],
                                 queue=dict(active=[], waiting=[])))
        found = {r['run']: r for r in snap['running']}
        self.assertEqual((found['normal']['overdue'], found['normal']['deadline_reason']),
                         (True, 'overall'))
        self.assertFalse(found['cleanup-grace']['overdue'])
        self.assertEqual((found['cleanup-phase-over']['overdue'],
                          found['cleanup-phase-over']['deadline_reason']), (True, 'phase'))

    def test_queued_following_and_admitted_waits_never_get_execution_stall_signals(self):
        rows = [dict(project='a', run='queued', started=None, phase='queued', step='resources', live=True,
                     workers=0, sequence=1, log_idle_s=9999, cpu_pct=0, phase_deadline=1),
                dict(project='a', run='following', started=None, phase='following', step='tree', live=True,
                     workers=0, sequence=2, log_idle_s=9999, cpu_pct=0, phase_deadline=1),
                dict(project='a', run='admitted', started=None, phase='queued', step='slot', live=True,
                     workers=1, sequence=3, log_idle_s=9999, cpu_pct=0, phase_deadline=1)]
        snap = board.derive(dict(now=1000, policy=dict(max_jobs=4), active=rows, finished=[],
                                 queue=dict(active=[], waiting=[])))
        admitted = next(r for r in snap['running'] if r['run'] == 'admitted')
        self.assertEqual((admitted['overdue'], admitted['suspected_stall']), (False, False))
        self.assertEqual({r['run'] for r in snap['waiting']}, {'queued', 'following'})

    def test_wait_reason_comes_from_the_run_sh_tree_as_a_fixed_category(self):
        procs = procs_of([(10, 1, 10, 4), (11, 10, 11, 1), (12, 11, 11, 1), (20, 1, 20, 1)])
        cases = [({12: 'cache', 20: 'database'}, 'install', 'cache'),   # pid 20 is outside the tree
                 ({12: 'database'}, 'database', 'database'),
                 ({11: 'sleep'}, 'database', 'database'),             # run.sh's template-lock loop
                 ({11: 'sleep'}, 'queued', 'slot'),
                 ({12: 'sleep'}, 'check', None),                      # a sleep inside the check is not a wait
                 ({}, 'check', None)]
        for waits, phase, want in cases:
            snapmod.WAITS.clear()
            snapmod.WAITS.update(waits)
            self.assertEqual(snapmod.wait_reason(10, procs, phase), want, (waits, phase))
        snapmod.WAITS.clear()

    def test_ps_names_map_to_categories_and_no_command_text_leaves(self):
        script = os.path.join(self.dir, 'cache.py')
        fixtures.write(script, 'import time; time.sleep(30)\n')
        proc = subprocess.Popen([sys.executable, script, 'register', '--path', '/secret/place'])
        try:
            time.sleep(0.3)
            snapmod.processes()
            self.assertEqual(snapmod.WAITS.get(proc.pid), 'cache')
            self.assertNotIn('/secret/place', json.dumps(snapmod.WAITS))
        finally:
            proc.kill()
            proc.wait()

    def test_renderer_labels_suspicion_and_deadline_overdue_separately(self):
        with open(os.path.join(HERE, 'web', 'index.html')) as stream:
            page = stream.read()
        self.assertIn("'suspected stall'", page)
        self.assertIn("'overdue'", page)
        self.assertIn("overall: 'overall', fast: 'fast job', phase: 'phase'", page)


class AsleepTest(Tmp):
    def test_wol_runner_that_does_not_answer_is_asleep_with_its_roles(self):
        root = fixtures.build(self.dir, 'stalls')
        pc = board.Runner({s['name']: s for s in board.fixture_runners(root)}['pc'])
        pc.poll()
        view = pc.view()
        self.assertEqual((view['state'], view['roles'], view['snapshot']), ('asleep', ['database', 'browser', 'PDF'], None))

    def test_wol_runner_whose_snapshot_fails_is_not_called_asleep(self):
        pc = board.Runner(dict(name='pc', routes=[['json:' + os.path.join(self.dir, 'missing.json')]], wakeable=True))
        pc.poll()
        self.assertEqual(pc.view()['state'], 'unreachable')


if __name__ == '__main__':
    unittest.main()
