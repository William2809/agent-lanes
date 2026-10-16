# Resource scheduling evaluation — 2026-10-15

## Evidence and diagnosis

The attached screenshot was a lead, not an instruction or current-state proof.
Inspection of both `remote/run.sh` implementations confirmed a five-minute slot
cutoff, per-project slot limits without a host budget, and pre-push templates
that ran the full check locally when the runner returned unreachable (3).
These are independently reproducible oversubscription and outage-fallback risks;
they do not establish the cause of a historical client crash.

Read-only host samples found a 10-core, 64 GiB development device and a 10-core,
16 GiB runner. The development device had roughly 6.4 GiB of swap in use, then
99% aggregate CPU usage and a one-minute load above 34. Browser renderers,
Finder and WindowServer were prominent consumers, not only test processes.
The runner was reachable and initially lightly loaded (load about 1.6).
Swap in use is historical; these samples do not prove sustained swap thrashing.

`loop-stats --repo shiftread --day 2026-10-15 --days 7` reported 10 completed
worker runs, median landing 3.42 minutes, baseline 136 seconds (11 samples),
and check 63 seconds (10 samples). The baseline includes release/commit work
and hooks; its duration alone does not identify the expensive subcommand.
`loop-stats --repo agent-lanes --day 2026-10-15 --days 3` reported 9 runs (8
complete), 6 bounced lanes, median landing 12.68 minutes, and check 753 seconds
(5 samples). A five-minute slot wait cannot reliably wait behind such checks.
These are cumulative recorded durations and estimates, not CPU measurements.

The current ShiftRead policy already pauses local tests; `check` is lint-only
and `check:full` is the remote suite. Its Vitest config's `CI` condition is a
convention, not resource isolation. Its installed Vitest source recognizes
`VITEST_MAX_WORKERS`; Playwright needs its own worker setting. Keep those tests
and the local pause. Do not bypass it by setting `CI=1` locally.

## Initial operating point

Permit many editing sessions but only one managed heavy job per execution
host. Grant at most two runner workers or one local worker, reserve CPU and
memory headroom, and increase concurrency only after representative peak-RSS
measurements. A smaller runner's RAM can be the limit before its CPU count.
Queueing trades elapsed validation latency for responsiveness; it cannot create
capacity. Coalesce identical snapshots and batch full checks at landing.

The same scheduler, fixtures, supervision and outage behavior ship in agent-lanes
and agent-lanes. Native Vitest/Make/CMake/Cargo and numerical-thread settings are
bounded. Jest, Playwright and package orchestrators need the explicit settings
listed in the skill. All such commands can use `safe-check --run`.

## Verification

On the separate macOS runner, isolated source copies of each toolkit passed the
same 37 checks: twelve resource/routing tests, nine runner/client regressions,
and sixteen tool-bundle/worker-brief checks. The checks
cover twelve concurrent submissions in FIFO order with no heavy-job overlap,
CPU/memory/pressure admission, stale/reused ownership, lower worker limits,
timeout and descendant termination, unrelated-process preservation, unreachable
runner behavior, actual worker export clamping, infrastructure classification
for memory overrun, identical-tree sharing, queued pass reuse, killed-owner child
protection, candidate exit normalization, forced/custom checks, and snapshot/hash
consistency. These use tiny sleeping processes, not real CPU/memory stress loads.
Host measurements are simulated where required; process locks, groups and
signals are real. The core checks passed after a Darwin zombie-group EPERM cleanup fix;
the additional route, immutable-bundle and worker-brief checks also passed. A real live runner check also caused the evaluation to queue;
an intentionally short 120-second evaluation wait expired with 75 and launched
no local fallback. The final evaluations used the normal longer wait budget.

Changed entrypoints pass shell/Python syntax checks and whitespace validation.
Both skills pass the skill-creator validator on the runner using an isolated
PyYAML environment.
The two toolkits' scheduler and resource fixtures are byte-identical; runner
differences are naming/configuration namespaces. No application test was removed.

This is not a throughput benchmark or proof that ten full agent clients can
never crash. Existing direct commands, automatic project hooks, lane setup/dev
servers, first-time toolchain provisioning, detached sessions and external
Docker/VM services can consume resources outside the managed check process
group. A skill is cooperative, not a global shell interceptor or OS sandbox.
Old queued/submitted runs retain their original tool snapshots. CPU admission
uses load average, not a hard CPU quota; memory monitoring samples roughly every
two seconds with bounded probes, so allocations can overshoot between samples.
Persistent service limits and adoption by existing sessions remain operational
requirements. No unrelated processes were terminated and no historical crash
log was available to attribute the reported client failures.

## Primary sources checked

- [Vitest maxWorkers](https://vitest.dev/config/maxworkers): worker pools can use
  available parallelism by default. A percentage applies separately to each
  invocation, so independent agents can still oversubscribe a host.
- [Playwright CI](https://playwright.dev/docs/ci): recommends one worker for
  stability and a global timeout. Browser and web-server processes also count.
- [Jest CLI](https://jestjs.io/docs/cli#--maxworkersnumstring): its worker pool
  has a separate `--maxWorkers` limit; `maxConcurrency` is a different control.
- [Turbo run](https://turborepo.dev/docs/reference/run): task concurrency defaults
  to ten; `--parallel` ignores the concurrency limit. Strict environment mode
  requires explicit worker-variable passthrough.
- [Node CLI](https://nodejs.org/api/cli.html#--max-old-space-sizesize-in-mib) and
  [worker limits](https://nodejs.org/api/worker_threads.html): a V8 heap limit is
  not a machine-wide limit on native memory, subprocesses or browsers.


## 2026-10-15 automatic guard follow-up

User-scope Claude Code and Codex PreToolUse hooks now deny recognized heavy
commands and direct package execution, including common shell/interpreter
wrappers, command substitution and CI/admission-flag attempts. The installer
preserves existing settings, saves private backups, and records host execution
policy. A remote-required development host cannot admit a local queue job;
configured projects also reject safe-check --local. No outage fallback exists.

Project entry points can verify a lease using resource-lease.cjs: a current
queue row, live owner/start time and matching process group, with a bounded
publication window. A macOS integration check exposed padded ps start times;
the verifier normalizes padding, with a regression probe. ShiftRead package
checks, Stop/pre-commit paths, Vitest, Playwright, ESLint and production Next
builds use the guard. Hosted CI and Docker acquire the vendored queue explicitly.

Activation evidence: Codex 0.160.0 hooks/list reports the installed hook trusted
and enabled after the supported configuration API write. A harmless marker
probe through the already-open desktop session still executed, so existing
Codex sessions require reload/resume. Claude's documented settings watcher
reloads changed hooks; no existing session was interrupted or restarted.

Limits: hooks do not intercept arbitrary interpreted computation, stdin sent to
an already-open PTY, every specialized tool, or an agent modifying its own
policy. This is not OS isolation or a hard host-wide CPU quota. No already-running
process is retroactively stopped. Docker services outside supervised process
groups still require engine/container resource limits. The full Docker image
build and a 10-agent application stress test were not run.

Primary references: [Codex hooks](https://learn.chatgpt.com/docs/hooks), including
trust and tool coverage; [Claude hooks](https://code.claude.com/docs/en/hooks),
including settings watcher reload. Installation is not evidence that an old
session has loaded the guard; use a harmless negative execution probe.

Final verification on the separate runner: 67 checks per toolkit passed (58
resource/runner/client regressions and nine enforcement/installation/lease tests).
The final lease implementation was rechecked for both toolkits alongside
ShiftRead's targeted ESLint with zero warnings, full TypeScript check and three
routing/outage/hosted-queue fixtures. The final ShiftRead check took 17 seconds;
its total including the shared queue was 158 seconds. Application unit/E2E suites
were not run for this tooling change. Source comparison confirms both toolkits'
shared files and ShiftRead's split vendored scheduler match.

Python inline and heredoc commands are inspected as syntax: writing test source
as data remains allowed, while actual test subprocess calls are denied. The nine
final guard tests passed in 3.4 seconds per toolkit through the remote queue.
ShiftRead’s normal pre-commit lint gate also passed remotely before commit.
