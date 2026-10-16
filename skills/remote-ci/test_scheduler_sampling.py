"""Atomic admission: ledger rows must not outlive their process/time snapshot."""
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

KIT = Path(os.environ.get('CIQUEUE_SAMPLING_KIT', Path(__file__).resolve().parent))
sys.path.insert(0, str(KIT / 'remote'))
import resources as r
import scheduler_maintenance as maintenance
import scheduler_process as supervisor

LIMITS = dict(MAX_JOBS=1, MAX_WORKERS=2, CPU_RESERVE=2,
              MEM_RESERVE_MB=2048, WORKER_MB=2048)
METRICS = dict(cores=10, load=0, available_mb=16000, total_mb=24000,
               pressure=1, swap_mb=0)
REQUEST = dict(project='current', lane='lane', sha='a'*40, tree='current',
               mode='run', command_digest='b'*64, slots=1, workers=1,
               memory_mb=2048, leases=[], generation=1)


def running_owner():
    return dict(version=2, protocol=2, owner=2, start='late', owner_birth='late', sequence=1,
                lease_generation=1, state='running', project='late', slot=1,
                workers=1, memory_mb=2048, cleanup_required=False,
                requested=dict(slots=1, workers=1, memory_mb=2048, leases=[]))


@contextmanager
def published_while_waiting(row=None, after_lock=None):
    """Another submitter commits after an early ps read, before our lock opens.

    Use a real ledger and flock. Model that precise interleaving at the lock
    boundary, so the defect does not depend on process launch speed or load.
    """
    with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp,
                                                          AGENT_CI_SCHEDULER='v1'):
        queue = r.Queue(temp)
        transaction = queue.transaction
        published = [False]

        @contextmanager
        def enter():
            with transaction() as data:
                if not published[0]:
                    published[0] = True
                    if row:
                        data['sequence'] = 1
                        data['jobs']['late'] = row
                    if after_lock:
                        after_lock()
                yield data

        def processes():
            procs = {1: dict(start='caller', birth='caller', group=1, parent=0, rss=0)}
            if published[0] and row:
                procs[2] = dict(start='late', birth='late', group=2, parent=0, rss=0)
            return procs

        with patch.object(queue, 'transaction', side_effect=enter):
            yield queue, processes


class Sampling(unittest.TestCase):
    def test_v1_keeps_owner_registered_while_waiting_for_lock(self):
        clock = [1]
        late = dict(owner=2, start='late', sequence=1)
        with published_while_waiting(late) as (queue, processes), \
                patch.object(r, 'processes', side_effect=processes), \
                patch.object(r, 'sample', return_value=METRICS), \
                patch.object(r.time, 'time', side_effect=lambda: clock[0]), \
                patch.object(r.time, 'sleep', side_effect=lambda _: clock.__setitem__(0, 3)):
            self.assertIsNone(r.acquire(queue, 'current', 1, 2, LIMITS, 1))
            with queue.transaction() as data:
                self.assertIn('late', data['jobs'])
                self.assertNotIn('current', data['jobs'])

    def test_v2_admission_keeps_new_live_owner(self):
        clock = [1]
        with published_while_waiting(running_owner()) as (queue, processes), \
                patch.object(supervisor, 'snapshot', side_effect=processes), \
                patch.object(supervisor, 'reconcile'), patch.object(maintenance, 'start_recovery'), \
                patch.object(r, 'scheduler_sample', return_value=METRICS), \
                patch.object(r.time, 'time', side_effect=lambda: clock[0]), \
                patch.object(r.time, 'sleep', side_effect=lambda _: clock.__setitem__(0, 3)):
            result = r.acquire_v2(queue, 'current', 1, 2, LIMITS, REQUEST)
            self.assertEqual(result.get('result'), 75)
            with queue.transaction() as data:
                self.assertEqual(data['jobs']['late']['state'], 'running')
                self.assertEqual(data['jobs']['late']['workers'], 1)

    def test_v2_reconcile_does_not_cancel_new_live_owner(self):
        with published_while_waiting(running_owner()) as (queue, processes), \
                patch.object(supervisor, 'snapshot', side_effect=processes):
            supervisor.reconcile(queue)
            with queue.transaction() as data:
                self.assertEqual(data['jobs']['late']['state'], 'running')
                self.assertFalse(data['jobs']['late'].get('cancel'))

    def test_deadline_crossed_inside_lock_never_grants_either_version(self):
        for version in (1, 2):
            clock = [1]
            with self.subTest(version=version), \
                    published_while_waiting(after_lock=lambda: clock.__setitem__(0, 3)) as (queue, processes), \
                    patch.object(r, 'processes', side_effect=processes), \
                    patch.object(supervisor, 'snapshot', side_effect=processes), \
                    patch.object(supervisor, 'reconcile'), patch.object(maintenance, 'start_recovery'), \
                    patch.object(r, 'sample', return_value=METRICS), \
                    patch.object(r, 'scheduler_sample', return_value=METRICS), \
                    patch.object(r.time, 'time', side_effect=lambda: clock[0]):
                if version == 1:
                    self.assertIsNone(r.acquire(queue, 'current', 1, 2, LIMITS, 1))
                else:
                    result = r.acquire_v2(queue, 'current', 1, 2, LIMITS, REQUEST)
                    self.assertEqual(result.get('result'), 75)
                with queue.transaction() as data:
                    self.assertFalse(any(row.get('workers') for row in data['jobs'].values()))


if __name__ == '__main__':
    unittest.main()
