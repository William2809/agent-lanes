"""Accounting caps preserve identities and allow host-bounded working sets."""
import copy
import json
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).parent/'remote'))
import resources
import scheduler_reservations as cap
import test_scheduler_runner as fixture

class ReservationPolicy(unittest.TestCase):
    def test_existing_rows_capped_without_identity_or_order_changes(self):
        row = dict(version=2, state='running', owner=11, owner_birth='birth',
                   lease_generation=3, run_id='original', sha='a'*40, tree='b'*64,
                   submitted_at=10, deadline=900, sequence=5, slot=2,
                   process={'group':42}, groups={'42':{}}, memory_mb=6144,
                   initial_memory_mb=3072, memory_ceiling_mb=6144,
                   requested=dict(memory_mb=6144, leases=['postgres:18']),
                   granted=dict(memory_mb=6144, workers=1),
                   memory_wait=dict(at=20,target_mb=6144))
        data={'jobs':{'token':row}}; before=copy.deepcopy(row)
        cap.apply(data,dict(RESERVATION_CAP_MB=3072,MAX_JOB_MB=10240))
        for k in ('memory_mb','initial_memory_mb','memory_ceiling_mb'):
            self.assertEqual(row[k],3072)
        for k in ('owner','owner_birth','lease_generation','run_id','sha','tree','submitted_at','deadline','sequence','slot','process','groups','state'):
            self.assertEqual(row[k],before[k])
        self.assertEqual(row['requested']['memory_mb'],3072)
        self.assertEqual(row['granted']['memory_mb'],3072)
        self.assertEqual(row['memory_wait'],dict(at=20,target_mb=3072))
        again=copy.deepcopy(row);cap.apply(data,dict(RESERVATION_CAP_MB=3072,MAX_JOB_MB=10240));self.assertEqual(row,again)
    def test_small_requests_stay_small_and_off_preserves_contract(self):
        request=dict(memory_mb=1536,initial_memory_mb=1536,memory_ceiling_mb=1536)
        self.assertEqual(cap.cap_request(request,{}),request)
        self.assertEqual(cap.cap_request(request,dict(RESERVATION_CAP_MB=3072,MAX_JOB_MB=8192))['memory_mb'],1536)
    def test_elastic_accounting_keeps_absolute_and_percentage_limits(self):
        row=dict(initial_memory_mb=3072,memory_mb=3072,reservation_policy_version=1)
        percent,maximum=cap.runtime_limits(row,300,8192)
        self.assertFalse(resources.budget_exceeded(5000,3072,percent,maximum))
        self.assertTrue(resources.budget_exceeded(8200,3072,percent,maximum))
        row.pop('reservation_policy_version')
        self.assertEqual(cap.runtime_limits(row,300,8192),(100,3072))

class CappedRunner(unittest.TestCase):
    setUp=fixture.SchedulerRunner.setUp
    git=fixture.SchedulerRunner.git
    configure=fixture.SchedulerRunner.configure
    add_project=fixture.SchedulerRunner.add_project
    stop_runs=fixture.SchedulerRunner.stop_runs
    start=fixture.SchedulerRunner.start
    wait_for=fixture.SchedulerRunner.wait_for
    log=fixture.SchedulerRunner.log
    count=fixture.SchedulerRunner.count
    assert_result=fixture.SchedulerRunner.assert_result
    rows=fixture.SchedulerRunner.rows
    def test_real_working_set_can_exceed_accounting_reservation(self):
        with (self.home/'.config/agent-lanes/config').open('a') as f:
            f.write('AGENT_CI_RESERVATION_CAP_MB=64\nAGENT_CI_INITIAL_MEMORY_MB=64\nAGENT_CI_KILL_PCT=300\n')
        command="python3 -c 'import time; payload=bytearray(96*1024*1024); time.sleep(5)'"
        proc=self.start('capped-burst',command=command)
        self.assert_result(proc,'capped-burst',0)
        row=next(r for r in self.rows().values() if r.get('run_id')=='capped-burst')
        self.assertEqual(row['requested']['memory_mb'],64)
        self.assertEqual(row['memory_ceiling_mb'],64)
        self.assertGreater(row['peak_rss_mb'],64)
        self.assertFalse(row.get('workers'))

    def stopped_event(self, run):
        events = self.home / '.local/state/agent-ci/telemetry/events.jsonl'
        return next(row for row in map(json.loads, events.read_text().splitlines())
                    if row.get('event') == 'stopped' and row.get('run_id') == run)

    def test_budget_stop_records_triggering_memory_and_reaps(self):
        with (self.home/'.config/agent-lanes/config').open('a') as f:
            f.write('AGENT_CI_RESERVATION_CAP_MB=64\nAGENT_CI_INITIAL_MEMORY_MB=64\nAGENT_CI_KILL_PCT=150\n')
        proc = self.start('budget-evidence', command="python3 -c 'import time; payload=bytearray(192*1024*1024); time.sleep(20)'")
        self.assert_result(proc, 'budget-evidence', 75)
        event = self.stopped_event('budget-evidence')
        self.assertEqual(event['reason'], 'memory-budget')
        self.assertGreater(event['rss_mb'], event['limit_mb'])
        self.assertEqual(event['pressure'], 1)
        self.assertIn('reason=memory-budget rc=75', self.log('budget-evidence'))
        self.assertFalse(any(r.get('groups') or r.get('workers') for r in self.rows().values()))

    def test_pressure_stop_is_distinguished_from_small_working_set(self):
        sampler = self.bin/'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "sample=lambda: dict(cores=10, load=0, available_mb=20000, pressure=4)\nif __name__ == '__main__':"))
        proc = self.start('pressure-evidence', command="python3 -c 'import time; time.sleep(20)'")
        self.assert_result(proc, 'pressure-evidence', 75)
        event = self.stopped_event('pressure-evidence')
        self.assertEqual(event['reason'], 'pressure')
        self.assertEqual(event['pressure'], 4)
        self.assertEqual(event['available_mb'], 20000)
        self.assertLess(event['rss_mb'], event['limit_mb'])
        self.assertIn('reason=pressure rc=75', self.log('pressure-evidence'))
        self.assertFalse(any(r.get('groups') or r.get('workers') for r in self.rows().values()))

if __name__=='__main__': unittest.main()
