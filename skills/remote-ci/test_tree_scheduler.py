"""Shared Tree requirements from the scheduler integration brief."""
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get('CI_TREE_REMOTE', str(Path(__file__).resolve().parent/'remote')))
import resources as r
import scheduler_process as sp

class SharedTree(unittest.TestCase):
    def test_v2_rejects_child_older_than_parent(self):
        record = dict(group=10, generation=1, members={'10': '100'})
        rows = {10: dict(group=10, parent=1, birth='100', start='same', rss=0),
                20: dict(group=20, parent=10, birth='99', start='same', rss=0)}
        self.assertEqual(set(sp.members(record, rows)), {10})

    def test_v1_retains_reparented_memory_member(self):
        row = dict(group=10, memory_mb=2048)
        rows = {10: dict(group=10, parent=1, birth='100', start='same', rss=100*1024),
                20: dict(group=20, parent=10, birth='101', start='same', rss=900*1024)}
        self.assertEqual(r.unused_reservation(row, rows), 1048)
        rows.pop(10)
        rows[20]['parent'] = 1
        self.assertEqual(r.unused_reservation(row, rows), 1148)
        rows[20]['birth'] = '102'
        self.assertEqual(r.unused_reservation(row, rows), 2048)

    def test_v2_tree_record_exists_before_gate_opens(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(sig, handler) for sig, handler in previous.items()])
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, HOME=temp):
            queue = r.Queue(temp)
            marker = str(Path(temp)/'stop')
            with queue.transaction() as data:
                data['jobs']['job'] = dict(version=2, state='running', owner=os.getpid(),
                    owner_birth=sp.birth(os.getpid()), lease_generation=7, workers=1, memory_mb=2048)
            command = [sys.executable, '-c',
                'import json,os,sys; row=json.load(open(sys.argv[1])); '
                'assert str(os.getppid()) in row["known"]', marker+'.tree']
            self.assertEqual(sp.run(queue, 'job', 'check', 10, marker, command), 0)
            with queue.transaction() as data:
                self.assertEqual(data['jobs']['job']['lease_generation'], 7)
                self.assertFalse(data['jobs']['job']['groups'])
            self.assertEqual(json.loads(Path(marker+'.tree').read_text())['known'], {})

if __name__ == '__main__':
    unittest.main()
