"""Requirement: pin each land attempt; refresh helpers between attempts."""
# Allow 60 s for fixture scheduling at host load 40-140; product deadlines stay separate.
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest

from workflow_fixture import ROOT, WorkflowFixture


class ToolPinningTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(('b',))
        self.addCleanup(self.f.close)
        self.tools = self.f.root / 'tools'
        for directory in ('bin', 'skills/agent-workers/scripts', 'skills/wt-dev/bin',
                          'skills/remote-ci/bin', 'skills/remote-ci/remote'):
            shutil.copytree(ROOT / directory, self.tools / directory)
        # Run the same regression against the old mq without changing this checkout.
        if os.environ.get('TOOL_PINNING_MQ_SOURCE'):
            shutil.copyfile(os.environ['TOOL_PINNING_MQ_SOURCE'], self.tools / 'bin/mq')
        self.tmp = self.f.root / 'tmp'
        self.tmp.mkdir()
        self.f.env.update(TMPDIR=str(self.tmp))
        self.mq = str(self.tools / 'bin/mq')
        self.f.env['PATH'] = self.f.env['PATH'].replace(str(ROOT / 'bin'), str(self.tools / 'bin'))

    def start_gated(self):
        source = (self.f.shims / 'wt-dev').read_text()
        source = source.replace('import os, pathlib, subprocess, sys',
                                'import os, pathlib, subprocess, sys, time')
        source = source.replace("cmd = sys.argv[1]", """cmd = sys.argv[1]
if os.environ.get('PIN_GATE') == '1' and not (root / 'gate.entered').exists():
    (root / 'gate.entered').write_text(os.environ['MQ_TOOL_ROOT'])
    while not (root / 'gate.release').exists(): time.sleep(0.02)
""")
        self.f.shim('wt-dev', source)
        p = subprocess.Popen([self.mq, 'run'], cwd=self.f.repo,
                             env={**self.f.env, 'PIN_GATE': '1'},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, p)
        until = time.monotonic() + 60
        while time.monotonic() < until:
            if (self.f.root / 'gate.entered').exists():
                return p, Path((self.f.root / 'gate.entered').read_text())
            if p.poll() is not None:
                self.fail('runner exited before the gate: ' + ''.join(p.communicate()))
            time.sleep(0.02)
        self.fail('runner did not reach the gate')

    def stop(self, p):
        (self.f.root / 'gate.release').touch()
        if p.poll() is None:
            p.terminate()
        p.communicate(timeout=60)

    def prepare_repair(self):
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.commit(self.f.lanes['b'], 'tests/test_a.py', 'assert total() == 3\n')
        self.f.run(self.mq, 'add', 'b')
        (self.f.state / 'bounce-head.b').write_text(
            self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['b']) + '\n')
        (self.f.state / 'queue').write_text('b\tparked\t1\twriter\n')
        (self.f.state / 'parked.b').write_text('uncommitted\n')
        self.f.commit(self.f.lanes['b'], 'tests/test_a.py', 'assert total() == 4\n')

    def test_next_land_attempt_uses_updated_kit(self):
        """Brief 2: refresh tools between attempts, pin them within an attempt.

        Boundary: source land/client are replaced during the first solo train.
        Gap: marker helpers stand in for land and remote-ci; no real runner runs.
        """
        self.f.run('wt-dev', 'new', 'c')
        c = self.f.home / 'worktrees/demo/c'
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.commit(c, 'c.txt', 'c\n')
        land = self.tools / 'skills/wt-dev/bin/land'
        client = self.tools / 'skills/remote-ci/bin/remote-ci'
        land.write_text('''#!/bin/sh
echo OLD_LAND >>"$FIXTURE_ROOT/pinned.calls"
python3 - <<'UPGRADE'
import os, pathlib, shutil
root = pathlib.Path(os.environ['FIXTURE_ROOT']) / 'tools'
for name in ('skills/wt-dev/bin/land', 'skills/remote-ci/bin/remote-ci'):
    path = root / name
    replacement = path.with_name(path.name + '.new')
    shutil.copy2(path, replacement)
    if name.endswith('/land'):
        replacement.write_text('#!/bin/sh\\necho NEW_LAND >>"$FIXTURE_ROOT/pinned.calls"\\nremote-ci check\\necho "LANDED fixture"\\n')
    else:
        replacement.write_text(path.read_text().replace('OLD_CLIENT', 'NEW_CLIENT'))
    replacement.replace(path)
UPGRADE
remote-ci check
echo "LANDED fixture"
''')
        client.write_text('#!/bin/sh\necho OLD_CLIENT >>"$FIXTURE_ROOT/pinned.calls"\n')
        for name, path in (('land', land), ('remote-ci', client)):
            shim = self.f.shims / name
            if shim.exists():
                shim.replace(shim.with_name(name + '.held'))
            shim.symlink_to(path)
        self.f.run(self.mq, 'add', 'b', 'c')
        result = self.f.run(self.mq, 'run', env={'MQ_TRAIN': '1'})
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.f.root / 'pinned.calls').read_text().splitlines(),
                         ['OLD_LAND', 'OLD_CLIENT', 'NEW_LAND', 'NEW_CLIENT'])
        self.assertEqual(self.f.queue(), [])
        self.assertEqual(list(self.tmp.glob('mq.*')), [])

    def test_next_land_attempt_bundles_kit_file_added_mid_run(self):
        # Regression 2026-10-17: a runner kept its start-time file list, so route_policy.py,
        # committed while it ran, was missing from later attempts (rc 74). Boundary: the
        # file appears during attempt one; attempt two must carry it.
        self.f.run('wt-dev', 'new', 'c')
        c = self.f.home / 'worktrees/demo/c'
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.commit(c, 'c.txt', 'c\n')
        land = self.tools / 'skills/wt-dev/bin/land'
        client = self.tools / 'skills/remote-ci/bin/remote-ci'
        land.write_text('''#!/bin/sh
remote-ci check
added="$FIXTURE_ROOT/tools/skills/remote-ci/bin/added_mid_run.py"
[ -f "$added" ] || printf 'print(1)\\n' >"$added"
echo "LANDED fixture"
''')
        client.write_text('#!/bin/sh\nif [ -f "$MQ_TOOL_ROOT/skills/remote-ci/bin/added_mid_run.py" ]; '
                          'then echo HAS; else echo MISSING; fi >>"$FIXTURE_ROOT/pinned.calls"\n')
        for name, path in (('land', land), ('remote-ci', client)):
            shim = self.f.shims / name
            if shim.exists():
                shim.replace(shim.with_name(name + '.held'))
            shim.symlink_to(path)
        self.f.run(self.mq, 'add', 'b', 'c')
        result = self.f.run(self.mq, 'run', env={'MQ_TRAIN': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.f.root / 'pinned.calls').read_text().splitlines(), ['MISSING', 'HAS'])

    def test_live_guard_update_cannot_change_running_queue(self):
        # Requirement 1; boundary: update after startup, before the first guard call.
        self.prepare_repair()
        p, bundle = self.start_gated()
        replacement = self.tools / 'bin/mq-guard.py.new'
        replacement.write_text('print("")\n')
        replacement.replace(self.tools / 'bin/mq-guard.py')
        (self.f.root / 'gate.release').touch()
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn('PARKED b: repair changed existing tests', out)
        self.assertEqual(self.f.queue()[0][1], 'parked')
        self.assertNotEqual(bundle, self.tools)
        self.assertFalse(bundle.exists(), 'requirement 2: remove the bundle on exit')
        self.assertEqual(list(self.tmp.glob('mq.*')), [])

    def test_bundle_carries_every_kit_file(self):
        # Regression 2026-10-16: remote-ci ran remote/scheduler_submit.py, absent from the
        # hand-kept list, so every land preflight exited 74. Boundary: a kit file that no
        # list names, beside an editor backup and bytecode that must stay out.
        remote = self.tools / 'skills/remote-ci/remote'
        (remote / 'new_kit_helper.py').write_text('print("kit")\n')
        (remote / 'run.sh.bak').write_text('old\n')
        (remote / '__pycache__').mkdir(exist_ok=True)
        (remote / '__pycache__/x.cpython-312.pyc').write_bytes(b'\0')
        for name in ('scheduler_submit.py', 'cache.py'):
            self.assertTrue((remote / name).exists(), name)
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.run(self.mq, 'add', 'b')
        p, bundle = self.start_gated()
        kit = bundle / 'skills/remote-ci/remote'
        for name in ('scheduler_submit.py', 'cache.py', 'new_kit_helper.py'):
            self.assertTrue((kit / name).is_file(), name)
        self.assertTrue((bundle / 'skills/remote-ci/bin/pool.py').is_file())
        self.assertFalse((kit / 'run.sh.bak').exists())
        self.assertFalse((kit / '__pycache__').exists())

    def test_path_helpers_use_startup_bundle(self):
        # Requirement 1; boundary: land and its remote-ci client change before landing.
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.run(self.mq, 'add', 'b')
        land = self.tools / 'skills/wt-dev/bin/land'
        client = self.tools / 'skills/remote-ci/bin/remote-ci'
        land.write_text('#!/bin/sh\necho PINNED_LAND >>"$FIXTURE_ROOT/pinned.calls"\nremote-ci check\necho "LANDED fixture"\n')
        client.write_text('#!/bin/sh\necho PINNED_CLIENT >>"$FIXTURE_ROOT/pinned.calls"\n')
        for name, path in (('land', land), ('remote-ci', client)):
            shim = self.f.shims / name
            if shim.exists():
                shim.replace(shim.with_name(name + '.held'))
            shim.symlink_to(path)
        p, bundle = self.start_gated()
        for path in (land, client):
            replacement = path.with_name(path.name + '.new')
            replacement.write_text('#!/bin/sh\necho LIVE_CHANGED\nexit 1\n')
            replacement.chmod(0o755)
            replacement.replace(path)
        (self.f.root / 'gate.release').touch()
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 0, err)
        self.assertTrue((self.f.root / 'pinned.calls').exists())
        calls = (self.f.root / 'pinned.calls').read_text()
        self.assertIn('PINNED_LAND', calls)
        self.assertIn('PINNED_CLIENT', calls)
        self.assertNotIn('LIVE_CHANGED', out)
        self.assertIn('mq done: landed 1;', out)
        self.assertFalse(bundle.exists())

    def test_fixer_does_not_inherit_runner_bundle(self):
        # Requirement 1's fixer exception; boundary: wk outlives the queue snapshot.
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.run(self.mq, 'add', 'b')
        # A known candidate failure: unknown FAILED lines park instead of starting a fixer.
        self.f.shim('land', '#!/bin/sh\necho "FAILED preflight"\nexit 1\n')
        self.f.shim('wk', '''#!/bin/sh
cat >/dev/null
printf '%s|%s|%s\\n' "$MQ_COPY" "$MQ_TOOL_ROOT" "${MQ_LIVE_PATH:-}" >"$FIXTURE_ROOT/fixer.env"
printf '%s\\n' "$PATH" >>"$FIXTURE_ROOT/fixer.env"
mkdir -p "$HOME/.claude/state/logs/demo"
printf 'tokens used\\n1\\n' >"$HOME/.claude/state/logs/demo/$1.log"
''')
        result = self.f.run(self.mq, 'run')
        self.assertIn('mq done:', result.stdout)
        values = (self.f.root / 'fixer.env').read_text().splitlines()
        self.assertEqual(values[0], '||')
        self.assertEqual(values[1], self.f.env['PATH'])
        self.assertEqual(list(self.tmp.glob('mq.*')), [])

    def test_signal_exit_removes_bundle(self):
        # Requirement 2; boundary: TERM while an external helper is still running.
        self.prepare_repair()
        p, bundle = self.start_gated()
        p.terminate()
        (self.f.root / 'gate.release').touch()
        p.communicate(timeout=20)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotEqual(bundle, self.tools)
        self.assertFalse(bundle.exists())
        self.assertEqual(list(self.tmp.glob('mq.*')), [])

    def test_missing_helper_fails_before_queue_state(self):
        # Requirement 3; boundaries: direct, sourced, and transitive helpers.
        for helper in ('bin/mq-guard.py', 'bin/mq-diagnostics.py',
                       'skills/agent-workers/scripts/state.sh',
                       'skills/agent-workers/scripts/run-record.sh',
                       'skills/wt-dev/bin/land', 'skills/wt-dev/bin/wtdev_db.py',
                       'skills/remote-ci/bin/remote-ci', 'skills/remote-ci/bin/fail-summary.py',
                       'skills/remote-ci/bin/blame.py', 'skills/remote-ci/remote/run.sh'):
            with self.subTest(helper=helper):
                path = self.tools / helper
                held = path.with_name(path.name + '.held')
                path.replace(held)
                try:
                    result = self.f.run(self.mq, 'run', check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stderr, f'mq: missing tool {helper}\n')
                    self.assertEqual(result.stdout, '')
                    self.assertFalse(self.f.state.exists())
                    self.assertEqual(list(self.tmp.glob('mq.*')), [])
                finally:
                    held.replace(path)

    def test_short_command_removes_script_copy(self):
        # Requirement 4 permits cheap short commands; requirement 2 removes their copy.
        result = self.f.run(self.mq, 'ls')
        self.assertEqual(result.stdout, 'queue empty\n')
        self.assertEqual(list(self.tmp.glob('mq.*')), [])

    def test_caller_supplied_copy_is_never_deleted(self):
        # Requirement: mq deletes only the temp copy it made. lanes and tests run mq in place with
        # MQ_COPY set to the real script; deleting it would remove the live tool (found 2026-10-13).
        for env in ({'MQ_COPY': self.mq},  # lanes' old call: no MQ_TOOL_ROOT, mq exits with an error
                    {'MQ_COPY': self.mq, 'MQ_TOOL_ROOT': str(self.tools), 'MQ_TOOL_REV': 'fixture'},
                    {'MQ_COPY': self.mq, 'MQ_TOOL_ROOT': str(self.tools), 'MQ_TOOL_REV': 'fixture',
                     'MQ_COPY_OWNER': '1'}):
            with self.subTest(env=sorted(env)):
                subprocess.run(['sh', self.mq, 'waiting'], cwd=self.f.repo, env={**self.f.env, **env},
                               capture_output=True, text=True, timeout=30)
                self.assertTrue(Path(self.mq).is_file(), 'mq deleted the script it was given')

    def test_short_command_removes_only_its_own_copy(self):
        # Requirement: a plain short command leaves no temp copy behind.
        subprocess.run([self.mq, 'ls'], cwd=self.f.repo, env=self.f.env,
                       capture_output=True, text=True, timeout=30)
        self.assertEqual(sorted(p.name for p in self.tmp.glob('mq.*')), [])
        self.assertTrue(Path(self.mq).is_file())

    def prepare_copy_race(self, mode, git_install=False):
        # Brief 2: a verified copy must match one stable revision and file set.
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.run(self.mq, 'add', 'b')
        land = self.tools / 'skills/wt-dev/bin/land'
        client = self.tools / 'skills/remote-ci/bin/remote-ci'
        land.write_text('#!/bin/sh\necho OLD_LAND >>"$FIXTURE_ROOT/pinned.calls"\nremote-ci check\necho "LANDED fixture"\n')
        client.write_text('#!/bin/sh\necho OLD_CLIENT >>"$FIXTURE_ROOT/pinned.calls"\n')
        for name, path in (('land', land), ('remote-ci', client)):
            shim = self.f.shims / name
            if shim.exists():
                shim.replace(shim.with_name(name + '.held'))
            shim.symlink_to(path)
        if git_install:
            self.f.git('init', '-q', '-b', 'main', cwd=self.tools)
            self.f.git('config', 'user.email', 'user@example.invalid', cwd=self.tools)
            self.f.git('config', 'user.name', 'Bundle fixture', cwd=self.tools)
            self.f.git('add', '--', 'bin', 'skills', cwd=self.tools)
            self.f.git('commit', '-q', '-m', 'initial tools', cwd=self.tools)
        self.f.env.update(BUNDLE_MODE=mode, BUNDLE_GIT='1' if git_install else '0')
        self.f.shim('tar', '''#!/usr/bin/env python3
import os, pathlib, shutil, subprocess, sys
args = sys.argv[1:]
subprocess.run(['/usr/bin/tar', *args], check=True)
if '-cf' not in args: sys.exit(0)
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
tools = pathlib.Path(os.environ['MQ_TOOL_ROOT'])
countfile = root / 'copy.attempts'
count = int(countfile.read_text()) + 1 if countfile.exists() else 1
countfile.write_text(str(count))
mode = os.environ['BUNDLE_MODE']
if count != 1 and mode != 'churn': sys.exit(0)
paths = ['skills/wt-dev/bin/land', 'skills/remote-ci/bin/remote-ci']
if mode != 'revision':
    for name in paths:
        path = tools / name; replacement = path.with_name(path.name + '.new')
        shutil.copy2(path, replacement)
        replacement.write_text(path.read_text().replace('OLD_', 'NEW_') + f'# upgrade {count}\\n')
        replacement.replace(path)
if os.environ['BUNDLE_GIT'] == '1':
    subprocess.run(['git', '-C', str(tools), 'add', '--', *paths], check=True)
    subprocess.run(['git', '-C', str(tools), 'commit', '-q', '--allow-empty', '-m', 'upgrade'], check=True)
if mode == 'mixed':
    # The archive has old remote-ci and new land. This models an update between file reads.
    archive = args[args.index('-cf') + 1]
    subprocess.run(['/usr/bin/tar', '-C', str(tools), '-rf', archive, paths[0]], check=True)
''')

    def assert_verified_upgrade(self, git_install):
        result = self.f.run(self.mq, 'run')
        self.assertIn('mq done: landed 1;', result.stdout)
        self.assertEqual((self.f.root / 'pinned.calls').read_text().splitlines(),
                         ['NEW_LAND', 'NEW_CLIENT'])
        self.assertEqual((self.f.root / 'copy.attempts').read_text(), '2')
        revision = self.f.git('rev-parse', 'HEAD', cwd=self.tools) if git_install else 'unknown'
        landing = [row for row in (self.f.state / 'events').read_text().splitlines()
                   if '\tlanding\t' in row]
        self.assertEqual(len(landing), 1)
        self.assertTrue(landing[0].endswith('tool=' + revision), landing)
        self.assertEqual(list(self.tmp.glob('mq.*')), [])

    def test_copy_retries_mixed_bundle_without_git(self):
        # Brief 2. Boundary: files change between archive reads with MQ_TOOL_REV=unknown.
        # Gaps: tar injects the update; land and remote-ci are marker stubs.
        self.prepare_copy_race('mixed')
        self.assert_verified_upgrade(False)

    def test_copy_retries_mixed_bundle_with_revision_change(self):
        # Brief 2. Boundary: a real tool Git commit changes during archive creation.
        # Gaps: the copy window is forced by a tar shim; no real updater runs.
        self.prepare_copy_race('mixed', git_install=True)
        self.assert_verified_upgrade(True)

    def test_copy_retries_revision_change_even_when_files_match(self):
        # Brief 2. Boundary: HEAD changes without changing any copied file.
        # Gaps: an empty Git commit forces the revision window; helper bodies are stubs.
        self.prepare_copy_race('revision', git_install=True)
        result = self.f.run(self.mq, 'run')
        self.assertIn('mq done: landed 1;', result.stdout)
        self.assertEqual((self.f.root / 'copy.attempts').read_text(), '2')
        revision = self.f.git('rev-parse', 'HEAD', cwd=self.tools)
        landing = [row for row in (self.f.state / 'events').read_text().splitlines()
                   if '\tlanding\t' in row]
        self.assertEqual(len(landing), 1)
        self.assertTrue(landing[0].endswith('tool=' + revision), landing)
        self.assertEqual(list(self.tmp.glob('mq.*')), [])

    def test_copy_rejects_continuous_changes_after_three_attempts(self):
        # Brief 2. Boundary: every copy attempt races a new file set.
        # Gaps: churn is injected after each archive; no real updater runs.
        self.prepare_copy_race('churn')
        queue = self.f.queue()
        result = self.f.run(self.mq, 'run', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, 'mq: tool bundle changed during copy (3 attempts)\n')
        self.assertEqual(result.stdout, '')
        self.assertEqual((self.f.root / 'copy.attempts').read_text(), '3')
        self.assertEqual(self.f.queue(), queue)
        self.assertFalse((self.f.root / 'pinned.calls').exists())
        self.assertFalse((self.f.state / 'run.lock').exists())
        self.assertEqual(list(self.tmp.glob('mq.*')), [])

    def test_git_copy_rejects_upgrade_paused_between_files(self):
        # Review P1. Boundary: land changes, HEAD and remote-ci stay at the old revision.
        # Gap: the paused updater is injected; no real installer runs.
        self.prepare_copy_race('paused', git_install=True)
        land = self.tools / 'skills/wt-dev/bin/land'
        replacement = land.with_name('land.new')
        shutil.copy2(land, replacement)
        replacement.write_text(land.read_text().replace('OLD_LAND', 'NEW_LAND'))
        replacement.replace(land)
        # Count real copies, without completing the paused upgrade.
        self.f.shim('tar', """#!/usr/bin/env python3
import os, pathlib, subprocess, sys
subprocess.run(['/usr/bin/tar', *sys.argv[1:]], check=True)
if '-cf' in sys.argv:
    count = pathlib.Path(os.environ['FIXTURE_ROOT']) / 'copy.attempts'
    count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
""")
        queue = self.f.queue()
        result = self.f.run(self.mq, 'run', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, 'mq: tool bundle changed during copy (3 attempts)\n')
        self.assertEqual(result.stdout, '')
        self.assertEqual((self.f.root / 'copy.attempts').read_text(), '3')
        self.assertFalse((self.f.root / 'pinned.calls').exists())
        self.assertEqual(self.f.queue(), queue)
        self.assertEqual(list(self.tmp.glob('mq.*')), [])

    def test_archive_inside_unrelated_git_repo_uses_unknown_revision(self):
        # Re-review A. Boundary: an archive tool root sits below an unrelated Git top-level.
        # Gap: the archive tree is a fixture copy; land is a local stub.
        self.f.git('init', '-q', '-b', 'main', cwd=self.f.root)
        self.f.git('config', 'user.email', 'user@example.invalid', cwd=self.f.root)
        self.f.git('config', 'user.name', 'Unrelated fixture', cwd=self.f.root)
        self.f.commit(self.f.root, 'unrelated.txt', 'unrelated project\n')
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.run(self.mq, 'add', 'b')
        result = self.f.run(self.mq, 'run', check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mq done: landed 1;', result.stdout)
        self.assertEqual(self.f.queue(), [])
        events = (self.f.state / 'events').read_text().splitlines()
        self.assertTrue(events)
        self.assertTrue(all(row.endswith('tool=unknown') for row in events), events)

    def test_untracked_tool_tree_at_git_top_level_uses_unknown_revision(self):
        # Re-review A. Boundary: ROOT equals the Git top-level, but tools are not in HEAD.
        # Gap: Git holds an unrelated file only; this does not run an installer.
        self.f.git('init', '-q', '-b', 'main', cwd=self.tools)
        self.f.git('config', 'user.email', 'user@example.invalid', cwd=self.tools)
        self.f.git('config', 'user.name', 'Untracked fixture', cwd=self.tools)
        self.f.commit(self.tools, 'unrelated.txt', 'unrelated project\n')
        self.f.commit(self.f.lanes['b'], 'b.txt', 'b\n')
        self.f.run(self.mq, 'add', 'b')
        result = self.f.run(self.mq, 'run', check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mq done: landed 1;', result.stdout)
        self.assertTrue(all(row.endswith('tool=unknown') for row in
                            (self.f.state / 'events').read_text().splitlines()))
