import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent / 'remote'))
import ci_telemetry
import scheduler_events


class SchedulerEvents(unittest.TestCase):
    def row(self, **changes):
        row = dict(state='queued', project='fixture', run_id='run', submitted_at=90,
                   blocker=None, requested=dict(workers=1, memory_mb=2048),
                   predicted_s=12.5, samples=7)
        row.update(changes)
        return row

    def test_queued_blocked_and_admitted_are_transition_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = scheduler_events.emit_transition(root, None, self.row(), now=100)
            blocked = self.row(blocker=dict(reason='project-reserve', holder='private-token'))
            previous = scheduler_events.emit_transition(root, previous, blocked, now=101)
            previous = scheduler_events.emit_transition(root, previous, blocked, now=102)
            running = self.row(state='running', lane_class='fast', workers=1, memory_mb=2048)
            scheduler_events.emit_transition(root, previous, running,
                metrics=dict(load=2.5, available_mb=8192), now=103)
            events = list(ci_telemetry.read(root / 'telemetry'))
        self.assertEqual([row['event'] for row in events], ['queued', 'blocked', 'admitted'])
        self.assertEqual(events[1]['reason'], 'project-reserve')
        self.assertEqual(events[2]['lane_class'], 'fast')
        self.assertEqual(events[2]['wait_s'], 13)
        self.assertEqual(events[2]['samples'], 7)

    def test_blocker_mapping_hides_lease_name_and_holder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = self.row(blocker=dict(reason='lease:secret-project', holder='private-token'),
                           command='private command', profile_context='private context')
            scheduler_events.emit_transition(root, None, row, now=100)
            encoded = (root / 'telemetry/events.jsonl').read_text()
            events = [json.loads(line) for line in encoded.splitlines()]
        self.assertEqual(events[-1]['reason'], 'lease')
        self.assertNotIn('secret-project', encoded)
        self.assertNotIn('private-token', encoded)
        self.assertNotIn('private command', encoded)
        self.assertNotIn('private context', encoded)

    def test_invalid_and_private_schema_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for fields in (dict(command='secret'), dict(profile_context='secret'),
                           dict(lane_class='private'), dict(samples=-1),
                           dict(samples=1.5),
                           dict(predicted_s=float('inf'))):
                self.assertFalse(ci_telemetry.emit(temp, 'admitted', run_id='run', **fields))
            self.assertEqual(list(ci_telemetry.read(temp)), [])

    def test_initial_admission_emits_queue_then_admitted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = self.row(state='running', lane_class='normal', workers=1, memory_mb=2048)
            scheduler_events.emit_transition(root, None, row,
                                             metrics=dict(load='invalid', available_mb=-1), now=95)
            events = list(ci_telemetry.read(root / 'telemetry'))
        self.assertEqual([event['event'] for event in events], ['queued', 'admitted'])
        self.assertNotIn('host_load', events[-1])
        self.assertNotIn('available_mb', events[-1])


if __name__ == '__main__':
    unittest.main()
