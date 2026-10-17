"""Paused work remains visible without inventing process liveness or an ETA."""
import json
import time
import unittest
from test_runner import Tmp, board, fixtures, snapshot


class MaintenanceVisibility(Tmp):
    def test_upgrade_status_exposes_only_bounded_display_fields(self):
        home = fixtures.Home(self.dir)
        home.save()
        from pathlib import Path
        target = Path(self.dir)/'ci/upgrade.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(dict(phase='draining', updated_at=123,
                                         last_error='private details', accepted={'token': 'secret'})))
        value = snapshot(self.dir)['upgrade']
        self.assertEqual(value, dict(phase='draining', updated_at=123, blocked=True))

    def test_ownerless_parked_and_starting_jobs_remain_visible_as_waiting(self):
        home = fixtures.Home(self.dir)
        expected = {}
        for state in ('parked', 'starting'):
            run = home.active('proj', 'ab' * 20, time.time() - 100, None, [], workers=0)
            row = next(row for token, row in home.jobs.items() if run in token)
            row.clear()
            row.update(version=2, state=state, sequence=len(expected) + 1)
            expected[run] = state
        home.save()
        snap = snapshot(self.dir)
        self.assertEqual({r['run']: r['maintenance_state'] for r in snap['active']}, expected)
        self.assertTrue(all(r['live'] is False and r['workers'] == 0 for r in snap['active']))
        view = board.derive(snap)
        self.assertEqual(view['running'], [])
        self.assertEqual(len(view['waiting']), 2)
        self.assertTrue(all(r['est_start_s'] is None and not r['est_known'] for r in view['waiting']))
        self.assertEqual(snap['queue']['granted'], 0)


if __name__ == '__main__':
    unittest.main()
