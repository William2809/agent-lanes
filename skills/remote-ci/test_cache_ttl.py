"""Brief steps 1–4. Runner-only fixtures; clocks, sizes and disk are stubs.

Each test names its requirement and boundary. These fixtures do not prove live
filesystem performance, physical disk recovery, or mixed-kit process races.
"""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_cache import cache


class CacheTTL(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ci = Path(self.temp.name).resolve()
        self.manager = cache.Cache(self.ci, delete=lambda path: None)
        self.now = 10000
        self.elapsed = 0
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(cache.time, 'time', side_effect=lambda: self.now).start()
        patch.object(cache.time, 'monotonic', side_effect=lambda: self.elapsed).start()
        patch.object(cache, 'size_bytes', side_effect=self.scan).start()
        self.disk = cache.shutil._ntuple_diskusage(1000, 0, 1000)

    def scan(self, path):
        self.calls.append(Path(path).name)
        return 10

    def entry(self, key, size=10, measured=10000, kind='turbo', token=''):
        path = self.ci / 'cache' / key
        path.mkdir(exist_ok=True)
        with self.manager.ledger() as entries:
            entries[key] = dict(path=str(path), kind=kind, pins=[token] if token else [],
                                bytes=size, used=1, metadata={})
            if measured is not None:
                entries[key]['measured'] = measured
        return path

    def records(self):
        return json.loads((self.ci / 'cache/registry.json').read_text())['entries']

    def evict(self, cap=1000):
        return self.manager.evict(cap=cap, floor=0, disk=self.disk)

    def test_ttl_boundary_and_missing_timestamp(self):
        """Step 1: skip at 3599; scan at 3600 and absent timestamp; no real walks."""
        self.entry('recent', measured=6401)
        self.entry('expired', measured=6400)
        self.entry('legacy', measured=None)
        self.evict()
        self.assertEqual(self.calls, ['legacy', 'expired'])

    def test_budget_completes_oldest_then_advances(self):
        """Step 1: six-second scan finishes; next known size waits; clocks stubbed."""
        self.entry('oldest', measured=1)
        self.entry('later', measured=2)
        def slow(path):
            self.calls.append(Path(path).name)
            self.now += 6
            self.elapsed += 6
            return 10
        cache.size_bytes.side_effect = slow
        self.evict()
        self.assertEqual(self.calls, ['oldest'])
        self.assertEqual(self.records()['oldest']['measured'], 10006)
        self.evict()
        self.assertEqual(self.calls, ['oldest', 'later'])

    def test_zero_refresh_after_budget_changes_eviction(self):
        """Step 1: recent zero grows after budget; cap uses new bytes; deletion stubbed."""
        self.entry('old', measured=1)
        self.entry('zero', size=0)
        def slow(path):
            self.calls.append(Path(path).name)
            self.elapsed += 6
            return 10 if Path(path).name == 'old' else 50
        cache.size_bytes.side_effect = slow
        self.assertEqual(self.evict(cap=20), (['old', 'zero'], True))
        self.assertEqual(self.calls, ['old', 'zero'])

    def test_each_eviction_scan_keeps_its_completion_timestamp(self):
        """Step 1: a later slow mandatory scan cannot refresh an earlier result."""
        self.entry('first', size=0, measured=0)
        self.entry('second', size=0, measured=0)
        def scan(path):
            self.now += 2 if Path(path).name == 'first' else 20
            return 10
        cache.size_bytes.side_effect = scan
        self.evict()
        records = self.records()
        self.assertEqual(records['first']['measured'], 10002)
        self.assertEqual(records['second']['measured'], 10022)

    def test_register_reuses_recent_same_path(self):
        """Step 2: repeated store registration at 3599 skips scan; no process race."""
        path = self.entry('store', measured=6401, kind='npm', token='first')
        self.manager.register('store', path, 'npm', 'second')
        self.assertEqual(self.calls, [])
        record = self.records()['store']
        self.assertEqual(record['measured'], 6401)
        self.assertEqual(record['pins'], ['first', 'second'])

    def test_register_scan_is_unlocked_and_completion_timed(self):
        """Step 2: new and expired registrations scan without registry lock; no threads."""
        for measured in (None, 6400):
            with self.subTest(measured=measured):
                path = self.ci / 'cache/new' if measured is None else self.entry('new', measured=measured)
                def scan(path):
                    with (self.manager.root / 'registry.lock').open('a') as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.now += 6
                    return 12
                cache.size_bytes.side_effect = scan
                self.manager.register('new', path, 'npm', 'job')
                self.assertEqual(self.records()['new']['measured'], self.now)

    def test_register_retains_known_bytes_during_scan(self):
        """Goal/step 2: unlocked refresh keeps known bytes and a pin; no process race."""
        path = self.entry('store', size=40, measured=1, kind='npm')
        def scan(path):
            record = self.records()['store']
            self.assertEqual(record['bytes'], 40)
            self.assertIn('job', record['pins'])
            self.assertTrue(any(pin.startswith('operation:job:measure-')
                                for pin in record['pins']))
            # A release during the scan must keep the result due for refresh.
            self.manager.release('job')
            return 50
        cache.size_bytes.side_effect = scan
        self.manager.register('store', path, 'npm', 'job')
        record = self.records()['store']
        self.assertEqual(record['bytes'], 40)
        self.assertEqual(record['measured'], 0)
        self.assertEqual(record['pins'], [])

    def test_owner_release_cancels_registration_measurement_claim(self):
        """Step 2: teardown removes its scan fence and rejects the late result."""
        path = self.entry('output', size=40, measured=1, kind='turbo')
        def scan(path):
            self.manager.release('job')
            record = self.records()['output']
            self.assertEqual(record['pins'], [])
            self.assertEqual(record['bytes'], 40)
            self.assertEqual(record['measured'], 0)
            return 50
        cache.size_bytes.side_effect = scan
        self.manager.register('output', path, 'turbo', 'job')
        record = self.records()['output']
        self.assertEqual(record['bytes'], 40)
        self.assertEqual(record['measured'], 0)
        self.assertEqual(record['pins'], [])

    def test_register_scan_preserves_concurrent_owner_and_marks_result_stale(self):
        """Step 2: a concurrent pin survives unlocked measurement reconciliation."""
        path = self.entry('store', size=40, measured=1, kind='npm', token='first')
        def scan(path):
            self.manager.register('store', path, 'npm', 'second',
                                  pin_existing=True, used=2)
            return 50
        cache.size_bytes.side_effect = scan
        self.manager.register('store', path, 'npm', 'first', used=1)
        record = self.records()['store']
        self.assertEqual(record['bytes'], 40)
        self.assertEqual(record['measured'], 0)
        self.assertEqual(record['pins'], ['first', 'second'])

    def test_malformed_timestamps_are_repaired(self):
        """Step 2: strings, bools and nonfinite numbers cannot sort or skip refresh."""
        for index, value in enumerate(('bad', True, float('nan'), float('inf'))):
            self.entry(str(index), measured=value)
        self.entry('valid', measured=1)
        self.evict()
        self.assertEqual(set(self.calls), {'0', '1', '2', '3', 'valid'})
        self.assertTrue(all(e['measured'] == 10000 for e in self.records().values()))

    def test_release_invalidates_owned_mutable_outputs(self):
        """Step 3: final release includes failed partial deps; immutable images excluded."""
        for kind in ('turbo', 'pnpm', 'npm', 'pip', 'venv', 'deps'):
            self.entry(kind, kind=kind, token='job')
        image_key = 'a' * 64
        self.entry(image_key, kind='deps', token='job')
        with self.manager.ledger() as entries:
            entries[image_key]['path'] = str(self.manager.root / 'deps' / ('a' * 64))
        self.entry('unrelated', token='other')
        self.manager.release('job')
        records = self.records()
        for kind in ('turbo', 'venv', 'deps'):
            self.assertEqual(records[kind]['measured'], 0)
        for kind in ('pnpm', 'npm', 'pip'):
            self.assertEqual(records[kind]['measured'], 10000)
        self.assertEqual(records[image_key]['measured'], 10000)
        self.assertEqual(records['unrelated']['measured'], 10000)

    def test_idle_reap_dirties_only_owned_mutable_outputs(self):
        """Step 3: abandoned mutable pins refresh; shared stores remain TTL-managed."""
        for kind in ('turbo', 'venv', 'deps', 'pnpm', 'npm', 'pip'):
            self.entry(kind, kind=kind, token='abandoned')
        (self.ci / 'lock').write_text('123\n')
        with patch.object(cache.os, 'getppid', return_value=123):
            self.manager.reap_idle()
        records = self.records()
        for kind in ('turbo', 'venv', 'deps'):
            self.assertEqual(records[kind]['measured'], 0)
        for kind in ('pnpm', 'npm', 'pip'):
            self.assertEqual(records[kind]['measured'], 10000)
        self.assertTrue(all(not entry['pins'] for entry in records.values()))

    def test_live_disk_resampled_after_walk(self):
        """Step 4: disk drops during refresh; eviction cannot return success; disk stubbed."""
        self.entry('pinned', measured=1, token='job')
        low = cache.shutil._ntuple_diskusage(1000, 999, 1)
        with patch.object(cache.shutil, 'disk_usage', side_effect=[self.disk, low, low]):
            self.assertEqual(self.manager.evict(cap=1000, floor=20), ([], False))

    def test_preflight_resamples_after_walk(self):
        """Step 4: copy is blocked if disk falls during refresh; no real allocation."""
        self.entry('pinned', measured=1, token='job')
        low = cache.shutil._ntuple_diskusage(1000, 999, 1)
        with patch.object(cache, 'host_cap', return_value=1000), \
                patch.object(cache, 'disk_floor', return_value=20), \
                patch.object(cache.shutil, 'disk_usage', side_effect=[self.disk, low, low]):
            with self.assertRaises(cache.CacheSpaceError):
                self.manager.preflight()
