#!/usr/bin/env python3
# ABOUTME: Lists weakened bounce repairs or first-submission check policy changes for mq review.
# Usage: mq-guard.py LANE_DIR BOUNCE_HEAD MAIN_HEAD | --policy LANE_DIR TARGET. Exit 2 if it cannot tell.
import configparser
import json
import os
import re
import shutil
import subprocess
import sys

# Path patterns are POSIX EREs matched by awk, as they always were (MQ_GUARD_PATHS / MQ_GUARD_CONFIG
# replace them); an invalid one stops the guard, so the repair parks.
# Tests: adding is fine; changing or removing an existing line, or adding a skip, needs review.
TESTS = ('MQ_GUARD_PATHS', os.environ.get('MQ_GUARD_PATHS') or (
    r'(^|/)(tests?|__tests__|specs?|e2e|playwright)/|\.(test|spec)\.[^/]+$|(^|/)test_[^/]+\.py$|_test\.(py|go)$'))
# Check config always needs review; baselines and tests are guarded only on bounce repairs.
CHECK_CONFIG = (r'(^|/)(vitest|jest|playwright|cypress|eslint|biome)\.config\.|'
    r'(^|/)(\.mq-policy|\.?pytest\.(ini|toml)|conftest\.py|tox\.ini|\.eslintrc(\.[a-z]+)?|biome\.jsonc?|\.wt-dev\.conf|'
    r'\.remote-ci\.conf|\.remote-ci\.conf|\.gitlab-ci\.yml|Jenkinsfile|azure-pipelines\.yml)$|'
    r'(^|/)(\.github/workflows|\.circleci|\.buildkite)/|(^|/)tools/land-preflight\.sh$')
POLICY = ('MQ_GUARD_CONFIG', os.environ.get('MQ_GUARD_CONFIG') or CHECK_CONFIG)
# Bounce repairs guard the whole Python config; first submissions compare policy sections.
CONFIG = ('MQ_GUARD_CONFIG', os.environ.get('MQ_GUARD_CONFIG') or
          CHECK_CONFIG + r'|(^|/)(setup\.cfg|pyproject\.toml)$|baseline[^/]*$')
PACKAGE = ('package.json', r'(^|/)package\.json$')
PYTHON_CONFIG = ('Python config', r'(^|/)(setup\.cfg|pyproject\.toml)$')
POLICY_INPUTS = set()
SKIP = re.compile(r'\.skip\(|\.only\(|(^|[^a-z])xit\(|(^|[^a-z])xdescribe\(|@skip|mark\.skip|skipTest|test\.fixme|t\.Skip\(')


class Unknown(Exception):
    pass


def run(args, env, stdin=''):
    """ARGS' exit code and stdout, decoded byte for byte: text=True would turn a CR inside a
    path or a patch line into a line break."""
    done = subprocess.run(args, input=stdin.encode('utf-8', 'surrogateescape'), capture_output=True,
                          timeout=60, env={**os.environ, **env})
    return done.returncode, done.stdout.decode('utf-8', 'surrogateescape')


def git(lane, *args, ok=(0,), status=False):
    # Paths after -- are literal file names, never patterns.
    rc, out = run(['git', '-C', lane, *args], {'GIT_LITERAL_PATHSPECS': '1'})
    if rc not in ok:
        raise Unknown('git ' + ' '.join(args[:2]))
    return (rc, out) if status else out


def replay_conflicts(out):
    """Paths `git merge-tree -z --name-only` reports in conflict: the conflicted-file list, plus the
    paths of CONFLICT messages (some directory rename conflicts list no file)."""
    parts = out.split('\0')
    end = parts.index('', 1)
    paths, at = set(parts[1:end]), end + 1
    while at < len(parts) and parts[at]:
        count = int(parts[at])
        named, kind = parts[at + 1:at + 1 + count], parts[at + 1 + count]
        if kind.startswith('CONFLICT'):
            paths |= set(named)
        at += count + 3
    return paths


def under(lane, paths, trees):
    """The files at or below PATHS in any of TREES: a directory conflict covers the files it holds,
    also when the directory exists only before the rename."""
    found = set(paths)
    for tree in trees:
        for path in paths:
            found |= {p for p in git(lane, 'ls-tree', '-r', '-z', '--name-only', tree, '--', path).split('\0') if p}
    return found


def guarded_conflicts(paths):
    """The conflict paths a guard pattern matches, a directory also as dir/."""
    probe = set(paths) | {p.rstrip('/') + '/' for p in paths}
    hit = matching(TESTS, probe) | matching(CONFIG, probe) | matching(PACKAGE, probe) | custom_inputs(probe)
    return {p for p in paths if p in hit or p.rstrip('/') + '/' in hit}


def matching(pattern, paths):
    """The paths the ERE matches. A path awk cannot see as one line counts as matched."""
    name, regex = pattern
    lines = [p for p in paths if '\n' not in p]
    rc, out = run(['awk', 'BEGIN { re = ENVIRON["MQ_GUARD_RE"]; "" ~ re } $0 ~ re'], {'MQ_GUARD_RE': regex},
                  ''.join(p + '\n' for p in lines))
    if rc:
        raise Unknown(f'{name} is not a valid ERE')
    return set(out.split('\n')[:-1]) | {p for p in paths if '\n' in p}


def settings(lane, tree, path, exists):
    if not exists:
        return {}
    text = git(lane, 'show', f'{tree}:{path}')
    try:
        if path.endswith('package.json'):
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError('expected an object')
            return {k: data[k] for k in ('scripts', 'jest', 'nyc', 'ava', 'mocha', 'c8',
                    'eslintConfig', 'prettier', 'tap', 'xo', 'standard') if k in data}
        if path.endswith('pyproject.toml'):
            try:
                import tomllib
            except ImportError:
                try:
                    import tomli as tomllib
                except ImportError:
                    # mq may start us with Apple's Python 3.9. Restart only after a
                    # runtime proves it can parse TOML; keep candidate config out of probes.
                    if not os.environ.get('MQ_GUARD_TOML_RESTART'):
                        probe = 'import importlib; importlib.import_module("tomllib" if __import__("sys").version_info >= (3, 11) else "tomli")'
                        for name in ('python3.14', 'python3.13', 'python3.12', 'python3.11',
                                     '/opt/homebrew/bin/python3', 'python3'):
                            executable = shutil.which(name)
                            if not executable:
                                continue
                            try:
                                ready = subprocess.run([executable, '-I', '-c', probe],
                                                       stdout=subprocess.DEVNULL,
                                                       stderr=subprocess.DEVNULL, timeout=5)
                                if ready.returncode == 0:
                                    os.environ['MQ_GUARD_TOML_RESTART'] = '1'
                                    os.execv(executable, [executable, os.path.abspath(__file__), *sys.argv[1:]])
                            except (OSError, subprocess.TimeoutExpired):
                                continue
                    raise Unknown('pyproject.toml needs Python 3.11+ or tomli; no usable runtime found')
            data = tomllib.loads(text).get('tool', {})
            if not isinstance(data, dict):
                raise ValueError('expected tool table')
            return {k: data[k] for k in ('pytest', 'coverage', 'tox', 'ruff', 'mypy',
                    'black', 'isort', 'pylint', 'flake8') if k in data}
        data = configparser.ConfigParser(interpolation=None)
        data.read_string(text)
        return {k: dict(data[k]) for k in data.sections() if k in
                ('tool:pytest', 'flake8', 'mypy', 'isort') or k.startswith(('coverage:', 'mypy-'))}
    except (ValueError, configparser.Error) as error:
        raise Unknown(f'cannot parse {path} ({type(error).__name__})')


def policy_inputs(lane, target):
    # Only the target's reviewed list is authoritative; a candidate cannot exempt itself.
    if not git(lane, 'ls-tree', '--name-only', target, '--', '.mq-policy').strip():
        return set()
    paths = set()
    for line in git(lane, 'show', target + ':.mq-policy').splitlines():
        path = line.strip()
        if not path or path.startswith('#'):
            continue
        if path.startswith('/') or any(p in ('', '.', '..') for p in path.rstrip('/').split('/')):
            raise Unknown('invalid path in .mq-policy')
        paths.add(path.rstrip('/'))
    return paths


def custom_inputs(paths):
    return {p for p in paths if p == '.mq-policy' or
            any(p == owned or p.startswith(owned + '/') for owned in POLICY_INPUTS)}


def weakened(lane, old, new, policy=False):
    """Guarded files whose change from tree OLD to tree NEW needs review."""
    found = set()
    raw = git(lane, 'diff-tree', '-r', '-z', '--no-renames', '--raw', old, new).split('\0')
    rows = [(meta.lstrip(':').split(' '), path) for meta, path in zip(raw[0::2], raw[1::2])]
    paths = {path for _, path in rows}
    tests = set() if policy else matching(TESTS, paths)
    config = matching(POLICY if policy else CONFIG, paths) | custom_inputs(paths)
    structured = matching(PACKAGE, paths) | matching(PYTHON_CONFIG, paths)
    for (old_mode, new_mode, _, _, status), path in rows:
        if path in config:
            found.add(path)
        elif path in tests and (status in 'DT' or (status == 'M' and old_mode != new_mode)):
            found.add(path)
        elif path in structured:
            before = settings(lane, old, path, status != 'A')
            after = settings(lane, new, path, status != 'D')
            if before != after or old_mode != new_mode and status not in 'AD':
                label = 'scripts' if before.get('scripts') != after.get('scripts') else 'check settings'
                found.add(path + ' (' + label + ')')
    if policy:
        return found
    for row in git(lane, 'diff-tree', '-r', '-z', '--no-renames', '--numstat', '--diff-filter=M', old, new).split('\0'):
        parts = row.split('\t', 2)
        if len(parts) == 3 and parts[1] != '0' and parts[2] in tests:
            found.add(parts[2])
    # Each test file's own patch: no header parsing, so a quoted file name keeps its skips.
    for (_, _, _, _, status), path in rows:
        if path in tests and status in 'AM' and any(
                SKIP.search(line) for line in added(git(lane, 'diff-tree', '-p', '-U0', '--no-renames', old, new, '--', path))):
            found.add(path)
    return found


def added(patch):
    """The added lines of PATCH, found inside hunks: an added line may itself start with "++"."""
    lines, hunk = [], False
    for line in patch.split('\n'):  # not splitlines(): a form feed inside a line is not a line break
        if line.startswith('diff --git '):
            hunk = False
        elif line.startswith('@@'):
            hunk = True
        elif hunk and line.startswith('+'):
            lines.append(line[1:])
    return lines


def main():
    global POLICY_INPUTS
    policy = sys.argv[1] == '--policy'
    if policy:
        lane, main_head = sys.argv[2:4]
        try:
            POLICY_INPUTS = policy_inputs(lane, main_head)
            base = git(lane, 'merge-base', 'HEAD', main_head).strip()
            for path in sorted(weakened(lane, base, 'HEAD^{tree}', policy=True)):
                print(path)
        except (Unknown, subprocess.TimeoutExpired, ValueError) as error:
            print(f'mq-guard: cannot inspect check policy ({error})', file=sys.stderr)
            sys.exit(2)
        return
    lane, base, main_head = sys.argv[1:4]
    try:
        POLICY_INPUTS = policy_inputs(lane, main_head)
        for ref in (base, main_head):
            git(lane, 'cat-file', '-e', ref + '^{commit}')
        # The repair's own changes: HEAD against the pre-bounce work replayed onto the main commit
        # HEAD now builds on. Whatever the lane did before the bounce, and whatever came from main by
        # a rebase or merge, is in both; a change made and undone within the repair is in neither.
        # A guarded file the replay conflicts on needs review however it was resolved: a
        # modify/delete conflict leaves no markers to diff.
        on = git(lane, 'merge-base', 'HEAD', main_head).strip()
        fork = git(lane, 'merge-base', base, on).strip()
        rc, replay = git(lane, 'merge-tree', '--write-tree', '-z', '--name-only', '--merge-base=' + fork, on, base,
                         ok=(0, 1), status=True)
        expected = replay.split('\0')[0]
        found = weakened(lane, expected, 'HEAD^{tree}')
        if rc == 1:
            conflicts = replay_conflicts(replay)
            found |= guarded_conflicts(under(lane, conflicts, (expected, 'HEAD', base, on)))
            if not conflicts:
                found.add('(replay conflict that names no file)')
        # A merge's own changes (conflict resolutions, edits no parent had) need review when they
        # touch guarded files; what it brings in from main does not.
        # A guarded file both sides changed was resolved by hand or by Git; either way someone must
        # check that main's stricter version survived.
        for merge in git(lane, 'rev-list', '--merges', 'HEAD', '--not', base, main_head).split():
            # -z: plain output quotes a name with a control character, so no pattern would match it.
            paths = {p for p in git(lane, 'diff-tree', '--cc', '-r', '-z', '--name-only', '--no-commit-id', merge).split('\0') if p}
            parents = git(lane, 'rev-list', '--parents', '-n', '1', merge).split()[1:]
            if len(parents) == 2:
                fork = git(lane, 'merge-base', *parents).strip()
                sides = [{q for q in git(lane, 'diff', '-z', '--name-only', '--no-renames', fork, p).split('\0') if q} for p in parents]
                paths |= sides[0] & sides[1]
            guarded = matching(TESTS, paths) | matching(CONFIG, paths) | matching(PACKAGE, paths) | custom_inputs(paths)
            found |= {path + ' (merge resolution)' for path in guarded}
    except (Unknown, subprocess.TimeoutExpired, ValueError) as error:
        print(f'mq-guard: cannot inspect the repair ({error})', file=sys.stderr)
        sys.exit(2)
    for path in sorted(found):
        print(path)


if __name__ == '__main__':
    main()
