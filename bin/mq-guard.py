#!/usr/bin/env python3
# ABOUTME: mq's repair guard: lists the test and check-config files a bounce repair weakened.
# Usage: mq-guard.py LANE_DIR BOUNCE_HEAD MAIN_HEAD. Prints one file per line; exit 2 if it cannot tell.
import json
import os
import re
import subprocess
import sys

# Tests: adding is fine; changing or removing an existing line, or adding a skip, needs review.
TESTS = os.environ.get('MQ_GUARD_PATHS') or (
    r'(^|/)(tests?|__tests__|specs?|e2e|playwright)/|\.(test|spec)\.[^/]+$|(^|/)test_[^/]+\.py$|_test\.(py|go)$')
# Check config and baselines: any change needs review (an added line can override an earlier one).
CONFIG = os.environ.get('MQ_GUARD_CONFIG') or (
    r'(^|/)(vitest|jest|playwright|cypress)\.config\.|(^|/)(pytest\.ini|conftest\.py|\.wt-dev\.conf|'
    r'\.remote-ci\.conf|\.remote-ci\.conf)$|baseline[^/]*$')
SKIP = re.compile(r'\.skip\(|\.only\(|(^|[^a-z])xit\(|(^|[^a-z])xdescribe\(|@skip|mark\.skip|skipTest|test\.fixme|t\.Skip\(')


class Unknown(Exception):
    pass


def git(lane, *args, text=True):
    done = subprocess.run(['git', '-C', lane, *args], capture_output=True, text=text, timeout=60)
    if done.returncode:
        raise Unknown(' '.join(args[:2]))
    return done.stdout


def patch_id(lane, commit):
    # No context lines: a rebase over nearby changes on main keeps the same ID.
    diff = git(lane, 'diff-tree', '-p', '-U0', '--no-renames', '--no-commit-id', commit)
    done = subprocess.run(['git', '-C', lane, 'patch-id', '--stable'], input=diff, capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise Unknown('patch-id')
    return done.stdout.split(' ')[0] or None


def scripts(lane, commit, path, exists):
    if not exists:
        return None
    try:
        return json.loads(git(lane, 'show', f'{commit}:{path}')).get('scripts')
    except ValueError:
        return 'unparsable'


def weakened(lane, commit):
    tests, config = re.compile(TESTS), re.compile(CONFIG)
    found = set()
    raw = git(lane, 'diff-tree', '-r', '-z', '--no-commit-id', '--no-renames', '--root', '--raw', commit).split('\0')
    for meta, path in zip(raw[0::2], raw[1::2]):
        old_mode, new_mode, _, _, status = meta.lstrip(':').split(' ')
        if config.search(path):
            found.add(path)
        elif tests.search(path) and (status in 'DT' or (status == 'M' and old_mode != new_mode)):
            found.add(path)
        elif os.path.basename(path) == 'package.json' and (
                scripts(lane, commit + '^', path, status != 'A') != scripts(lane, commit, path, status != 'D')):
            found.add(path + ' (scripts)')  # land runs check:remote, which may call any other script
    for row in git(lane, 'diff-tree', '-r', '-z', '--no-commit-id', '--no-renames', '--numstat', '--diff-filter=M', commit).split('\0'):
        parts = row.split('\t', 2)
        if len(parts) == 3 and parts[1] != '0' and tests.search(parts[2]):
            found.add(parts[2])
    path = None
    for line in git(lane, 'diff-tree', '-p', '-U0', '--no-commit-id', '--no-renames', commit).splitlines():
        if line.startswith('+++ '):
            path = line[6:] if line.startswith('+++ b/') else None
        elif line.startswith('+') and path and tests.search(path) and SKIP.search(line):
            found.add(path)
    return found


def main():
    lane, base, main_head = sys.argv[1:4]
    try:
        for ref in (base, main_head):
            git(lane, 'cat-file', '-e', ref + '^{commit}')
        # The repair's own commits: not merges, not on main (merged or rebased-in work of others),
        # and not a rebased copy of a lane commit from before the bounce (same patch).
        before = {patch_id(lane, c) for c in git(lane, 'rev-list', '--no-merges', base, '--not', main_head).split()}
        found = set()
        for commit in git(lane, 'rev-list', '--no-merges', 'HEAD', '--not', base, main_head).split():
            if patch_id(lane, commit) not in before:
                found |= weakened(lane, commit)
        # A merge's own changes (conflict resolutions, edits no parent had) need review when they
        # touch guarded files; what it brings in from main does not.
        guarded = re.compile(f'(?:{TESTS})|(?:{CONFIG})|(^|/)package\\.json$')
        # A guarded file both sides changed was resolved by hand or by Git; either way someone must
        # check that main's stricter version survived.
        for merge in git(lane, 'rev-list', '--merges', 'HEAD', '--not', base, main_head).split():
            paths = set(git(lane, 'diff-tree', '--cc', '-r', '--name-only', '--no-commit-id', merge).splitlines())
            parents = git(lane, 'rev-list', '--parents', '-n', '1', merge).split()[1:]
            if len(parents) == 2:
                fork = git(lane, 'merge-base', *parents).strip()
                sides = [set(git(lane, 'diff', '--name-only', '--no-renames', fork, p).splitlines()) for p in parents]
                paths |= sides[0] & sides[1]
            for path in paths:
                if guarded.search(path):
                    found.add(path + ' (merge resolution)')
    except (Unknown, subprocess.TimeoutExpired, ValueError) as error:
        print(f'mq-guard: cannot inspect the repair ({error})', file=sys.stderr)
        sys.exit(2)
    for path in sorted(found):
        print(path)


if __name__ == '__main__':
    main()
