// Admission is a live host queue row tied to this process group, never a CI flag.
const { readFileSync, existsSync } = require('node:fs');
const { homedir } = require('node:os');
const { join } = require('node:path');
const { execFileSync } = require('node:child_process');
const pause = new Int32Array(new SharedArrayBuffer(4));
function ps(fields, pid) {
  return execFileSync('/bin/ps', ['-o', fields, '-p', String(pid)], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 2000,
  }).trim();
}
function localAllowed() {
  const policy = join(homedir(), '.config/agent-ci/policy.json');
  try {
    return !existsSync(policy) || JSON.parse(readFileSync(policy, 'utf8')).execution === 'local-queue';
  } catch { return false; }
}
function readGrant(token) {
  const row = JSON.parse(readFileSync(join(homedir(), '.local/state/agent-ci/queue.json'), 'utf8')).jobs[token];
  if (!row || !Number.isInteger(row.workers) || row.workers < 1) return null;
  if (ps('lstart=', row.owner) !== String(row.start).trim() || ps('stat=', row.owner).startsWith('Z')) return null;
  return row;
}
function matchesGroup(row) {
  const group = row.marker ? Number(readFileSync(row.marker, 'utf8').trim()) : row.group;
  return group > 0 && group === Number(ps('pgid=', process.pid));
}
function allocation() {
  const token = process.env.AGENT_CI_TOKEN;
  if (!token || !localAllowed()) return null;
  // The supervisor publishes the child group immediately after spawning it.
  for (let attempt = 0; attempt < 15; attempt += 1) {
    try {
      const row = readGrant(token);
      if (row && matchesGroup(row)) return row;
    } catch { /* Fail closed after the bounded publication window. */ }
    Atomics.wait(pause, 0, 0, 50);
  }
  return null;
}
function requireAllocation() {
  const row = allocation();
  if (!row) throw new Error('Heavy checks require a live queue lease. Use safe-check; CI=1 is not admission.');
  return row;
}
module.exports = { allocation, requireAllocation };
