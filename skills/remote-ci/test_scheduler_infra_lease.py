"""Host capacity survives independently tracked infrastructure cleanup."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'remote'))
import resources as r
from process_tree import Tree, birth


class InfraLease(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.q = r.Queue(self.root)
        self.pid = os.getpid()
        self.procs = {self.pid: dict(start='self', birth=birth(self.pid),
            parent=os.getppid(), group=os.getpgrp(), rss=64 * 1024)}
        self.marker = self.root / 'infra.tree'
        Tree(self.pid).save(self.marker)
        with self.q.transaction() as data:
            data['jobs']['job'] = dict(owner=99999999, start='dead', workers=2, memory_mb=2048)
        sampled = patch.object(r, 'processes', return_value=self.procs)
        sampled.start()
        self.addCleanup(sampled.stop)

    def row(self):
        with self.q.transaction() as data:
            return dict(data['jobs']['job'])

    def test_live_infra_retains_capacity_after_owner_exits(self):
        self.q.register_infra_tree('job', str(self.marker))
        self.assertFalse(self.q.release('job'))
        with self.q.transaction() as data:
            r.sweep(data, self.procs, 0)
        row = self.row()
        self.assertTrue(row['cleanup_pending'])
        self.assertEqual((row['workers'], row['memory_mb']), (2, 2048))
        self.assertEqual(r.unused_reservation(row, self.procs), 2048)
        limits = dict(MAX_JOBS=1, MAX_WORKERS=4, CPU_RESERVE=1, MEM_RESERVE_MB=512, WORKER_MB=512)
        metrics = dict(pressure=1, cores=8, load=0, available_mb=16000)
        self.assertEqual(r.capacity([row], metrics, limits, 1, self.procs), 0)
        self.q.clear_infra_tree('job', str(self.marker))
        self.assertTrue(self.q.release('job'))
        self.assertEqual(r.capacity([], metrics, limits, 1, self.procs), 1)

    def test_empty_candidate_tree_cannot_hide_live_infrastructure(self):
        empty = self.root / 'candidate.tree'
        tree = Tree(self.pid)
        tree.known = {}
        tree.save(empty)
        self.q.update('job', tree_marker=str(empty))
        self.q.register_infra_tree('job', str(self.marker))
        self.assertFalse(self.q.release('job'))
        self.assertTrue(r.cleanup_live(self.row(), self.procs, 0))
        self.q.clear_infra_tree('job', str(self.marker))
        self.assertTrue(self.q.release('job'))

    def test_stale_clear_and_second_marker_cannot_replace_ownership(self):
        self.q.register_infra_tree('job', str(self.marker))
        with self.assertRaises(ValueError):
            self.q.clear_infra_tree('job', str(self.root / 'other'))
        with self.assertRaises(ValueError):
            self.q.register_infra_tree('job', str(self.root / 'other'))
        self.assertEqual(self.row()['infra_tree_marker'], str(self.marker))

    def test_unknown_grant_is_refused_except_explicit_legacy_recovery(self):
        with self.assertRaises(ValueError):
            self.q.register_infra_tree('missing', str(self.marker))
        self.assertFalse(self.q.register_infra_tree('missing', str(self.marker), allow_missing=True))
        self.q.update('job', version=2)
        with self.assertRaises(ValueError):
            self.q.register_infra_tree('job', str(self.marker), allow_missing=True)


if __name__ == '__main__':
    unittest.main()
