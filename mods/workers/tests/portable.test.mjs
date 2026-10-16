import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { join } from 'node:path';
const require = createRequire(import.meta.url);
const { events, remember, identity, unclaimed, readReports, commands } = require(join(process.env.WORKERS_BUILD, 'hooks/parse.js'));
const { weather, sparkline } = require(join(process.env.WORKERS_BUILD, 'hooks/weather.js'));
const run = (id, extra = {}) => ({ name: 'review', repo: 'demo', runId: id, log: '/tmp/demo/review.log', dir: '/tmp/demo', started: 100, status: 'running', session: 'lead', ...extra });

test('completion is attempt-specific and disappearance is not success', () => {
  const old = run('old', { rc: 0 });
  const history = remember([], [old], 0);
  assert.deepEqual(events([old], history, 'lead'), []);
  assert.deepEqual(events([], history, 'lead'), []);
  const next = run('next', { started: 200, rc: 0 });
  assert.deepEqual(events([next], history, 'lead'), [next]);
  assert.notEqual(identity(old), identity(next));
});
test('resumed runs suppress stale completion without suppressing another repository', () => {
  const old = run('old', { rc: 0 });
  const current = run('current', { started: 200 });
  assert.deepEqual(events([old, current], [], 'lead'), []);
  const other = run('old', { log: '/tmp/other/review.log', rc: 0 });
  assert.deepEqual(events([other, current], [], 'lead'), [other]);
});
test('unclaimed reports exclude paused, delivered and queue-owned runs', () => {
  const lost = run('lost', { session: 'gone', rc: 0, ended: 150 });
  assert.deepEqual(unclaimed([lost], 'lead', 1000), [lost]);
  for (const extra of [{ delivered: true }, { read: true }, { owner: 'mq' }, { status: 'paused' }]) {
    assert.deepEqual(unclaimed([{ ...lost, ...extra }], 'lead', 1000), []);
  }
});
test('only an actual report for the exact log suppresses a notice', () => {
  const item = run('one');
  const text = `=================== ${item.log}\nChanged: work\n`;
  assert.deepEqual(readReports('batches report review', text, [item]), [item]);
  assert.deepEqual(readReports('batches report review; true', text, [item]), []);
  assert.deepEqual(readReports('batches report review', 'NO REPORT', [item]), []);
  assert.ok(commands(item).resume.includes('one'));
});
test('context helpers retain visible thresholds and bounded sparkline', () => {
  assert.equal(weather(10).label, 'Clear');
  assert.equal(weather(92).label, 'Storm');
  assert.equal(sparkline([-10, 0, 100, 200, 300], 200), '▁▁▅██');
});
