#!/usr/bin/env python3
# ABOUTME: mq's repair guard: lists the test and check-config files a bounce repair weakened.
# Usage: mq-guard.py LANE_DIR BOUNCE_HEAD MAIN_HEAD. Prints one file per line; exit 2 if it cannot tell.
import json
import os
import re
import subprocess
import sys

# Path patterns are POSIX EREs matched by awk, as they always were (MQ_GUARD_PATHS / MQ_GUARD_CONFIG
# replace them); an invalid one stops the guard, so the repair parks.
# Tests: adding is fine; changing or removing an existing line, or adding a skip, needs review.
TESTS = ('MQ_GUARD_PATHS', os.environ.get('MQ_GUARD_PATHS') or (
    r'(^|/)(tests?|__tests__|specs?|e2e|playwright)/|\.(test|spec)\.[^/]+$|(^|/)test_[^/]+\.py$|_test\.(py|go)$'))
# Check config and baselines: any change needs review (an added line can override an earlier one).
CONFIG = ('MQ_GUARD_CONFIG', os.environ.get('MQ_GUARD_CONFIG') or (
    r'(^|/)(vitest|jest|playwright|cypress)\.config\.|(^|/)(pytest\.ini|conftest\.py|\.wt-dev\.conf|'
    r'\.remote-ci\.conf|\.remote-ci\.conf)$|baseline[^/]*$'))
PACKAGE = ('package.json', r'(^|/)package\.json$')
SKIP = re.compile(r'\.skip\(|\.only\(|(^|[^a-z])xit\(|(^|[^a-z])xdescribe\(|@skip|mark\.skip|skipTest|test\.fixme|t\.Skip\(')


class Unknown(Exception):
    pass


def git(lane, *args, ok=(0,)):
    done = subprocess.run(['git', '-C', lane, *args], capture_output=True, text=True, timeout=60)
    if done.returncode not in ok:
        raise Unknown('git ' + ' '.join(args[:2]))
    return done.stdout


def matching(pattern, paths):
    """The paths the ERE matches. A path awk cannot see as one line counts as matched."""
    name, regex = pattern
    lines = [p for p in paths if '\n' not in p]
    done = subprocess.run(['awk', 'BEGIN { re = ENVIRON["MQ_GUARD_RE"]; "" ~ re } $0 ~ re'],
                          input=''.join(p + '\n' for p in lines), capture_output=True, text=True,
                          timeout=60, env={**os.environ, 'MQ_GUARD_RE': regex})
    if done.returncode:
        raise Unknown(f'{name} is not a valid ERE')
    return set(done.stdout.splitlines()) | {p for p in paths if '\n' in p}


def scripts(lane, tree, path, exists):
    if not exists:
        return None
    try:
        return json.loads(git(lane, 'show', f'{tree}:{path}')).get('scripts')
    except ValueError:
        return 'unparsable'


def weakened(lane, old, new):
    """Guarded files whose change from tree OLD to tree NEW needs review."""
    found = set()
    raw = git(lane, 'diff-tree', '-r', '-z', '--no-renames', '--raw', old, new).split('\0')
    rows = [(meta.lstrip(':').split(' '), path) for meta, path in zip(raw[0::2], raw[1::2])]
    paths = {path for _, path in rows}
    tests, config, package = matching(TESTS, paths), matching(CONFIG, paths), matching(PACKAGE, paths)
    for (old_mode, new_mode, _, _, status), path in rows:
        if path in config:
            found.add(path)
        elif path in tests and (status in 'DT' or (status == 'M' and old_mode != new_mode)):
            found.add(path)
        elif path in package and scripts(lane, old, path, status != 'A') != scripts(lane, new, path, status != 'D'):
            found.add(path + ' (scripts)')  # land runs check:remote, which may call any other script
    for row in git(lane, 'diff-tree', '-r', '-z', '--no-renames', '--numstat', '--diff-filter=M', old, new).split('\0'):
        parts = row.split('\t', 2)
        if len(parts) == 3 and parts[1] != '0' and parts[2] in tests:
            found.add(parts[2])
    path = None
    for line in git(lane, 'diff-tree', '-p', '-U0', '--no-renames', old, new).splitlines():
        if line.startswith('+++ '):
            path = line[6:] if line.startswith('+++ b/') else None
        elif line.startswith('+') and path in tests and SKIP.search(line):
            found.add(path)
    return found


def main():
    lane, base, main_head = sys.argv[1:4]
    try:
        for ref in (base, main_head):
            git(lane, 'cat-file', '-e', ref + '^{commit}')
        # The repair's own changes: HEAD against the pre-bounce work replayed onto the main commit
        # HEAD now builds on. Whatever the lane did before the bounce, and whatever came from main by
        # a rebase or merge, is in both; a change made and undone within the repair is in neither.
        # Replay conflicts stay as conflict markers, so a guarded file Git could not merge shows.
        on = git(lane, 'merge-base', 'HEAD', main_head).strip()
        fork = git(lane, 'merge-base', base, on).strip()
        expected = git(lane, 'merge-tree', '--write-tree', '--merge-base=' + fork, on, base, ok=(0, 1)).split('\n')[0]
        found = weakened(lane, expected, 'HEAD^{tree}')
        # A merge's own changes (conflict resolutions, edits no parent had) need review when they
        # touch guarded files; what it brings in from main does not.
        # A guarded file both sides changed was resolved by hand or by Git; either way someone must
        # check that main's stricter version survived.
        for merge in git(lane, 'rev-list', '--merges', 'HEAD', '--not', base, main_head).split():
            paths = set(git(lane, 'diff-tree', '--cc', '-r', '--name-only', '--no-commit-id', merge).splitlines())
            parents = git(lane, 'rev-list', '--parents', '-n', '1', merge).split()[1:]
            if len(parents) == 2:
                fork = git(lane, 'merge-base', *parents).strip()
                sides = [set(git(lane, 'diff', '--name-only', '--no-renames', fork, p).splitlines()) for p in parents]
                paths |= sides[0] & sides[1]
            guarded = matching(TESTS, paths) | matching(CONFIG, paths) | matching(PACKAGE, paths)
            found |= {path + ' (merge resolution)' for path in guarded}
    except (Unknown, subprocess.TimeoutExpired, ValueError) as error:
        print(f'mq-guard: cannot inspect the repair ({error})', file=sys.stderr)
        sys.exit(2)
    for path in sorted(found):
        print(path)


if __name__ == '__main__':
    main()
