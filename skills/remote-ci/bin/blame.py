#!/usr/bin/env python3
# ABOUTME: Rank actual changed-file overlaps against a red check, without modifying lanes or queue state.
import json
from pathlib import PurePosixPath, Path
import re
import subprocess
import sys
sys.dont_write_bytecode = True
from importlib.util import module_from_spec, spec_from_file_location

spec = spec_from_file_location('failure', Path(__file__).with_name('fail-summary.py'))
failure = module_from_spec(spec)
spec.loader.exec_module(failure)


def run(args, cwd):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def git(root, *args):
    return run(['git', *args], root)


def packages(root, sha):
    paths = git(root, 'ls-tree', '-r', '--name-only', sha) or ''
    result = {}
    for path in paths.splitlines():
        if path.endswith('/package.json'):
            try:
                name = json.loads(git(root, 'show', f'{sha}:{path}') or '{}').get('name')
            except ValueError:
                continue
            if name:
                result.setdefault(name, []).append(str(PurePosixPath(path).parent))
    return result


def normalized(root, sha, records, manifests):
    result = []
    for record in records:
        file = record['file']
        # Absolute runner paths end in work[/slotN]/<repo-relative path>.
        file = re.sub(r'^.*?/work(?:-slot\d+)?/', '', file) if file.startswith('/') else file
        choices = [f'{p}/{file}' for p in manifests.get(record.get('package', ''), [])]
        choices.append(file)
        existing = [f for f in dict.fromkeys(choices) if not f.startswith('/') and git(root, 'cat-file', '-e', f'{sha}:{f}') is not None]
        if len(existing) == 1:
            result.append(existing[0])
    return list(dict.fromkeys(result))


def rank(changed, failing, package_dirs):
    best = (0, 'no failing-file overlap')
    def package(file):
        return next((p for p in package_dirs if file.startswith(p + '/')), '')
    for file in changed:
        for fail in failing:
            if file == fail:
                candidate = (3, f'touches failing file {file}')
            elif PurePosixPath(file).parent == PurePosixPath(fail).parent:
                candidate = (2, f'touches {file} in failing directory {PurePosixPath(fail).parent}')
            elif package(file) and package(file) == package(fail):
                candidate = (1, f'touches {file} in failing package {package(file)}')
            else:
                continue
            if candidate[0] > best[0]:
                best = candidate
    return best


def main():
    root, sha, *names = sys.argv[1:]
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 1
    if not data.get('failed'):
        return 1
    if not data.get('files'):
        failure.emit('suspect unknown: failing files not named')
        return 0
    manifests = packages(root, sha)
    failing = normalized(root, sha, data['files'], manifests)
    if not failing:
        failure.emit('suspect unknown: failing files could not be resolved')
        return 0
    common = git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    # The queue's BASE is the main checkout's branch; fork-point retains the
    # original fork even if that branch has since incorporated the lane.
    base = git(root, '--git-dir=' + common, 'symbolic-ref', 'HEAD') if common else None
    if not base:
        base = git(root, '--git-dir=' + common, 'rev-parse', 'HEAD') if common else None
    ranked = []
    for name in names:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or name in ('.', '..'):
            ranked.append((0, name, 'invalid lane name'))
            continue
        lane = run(['wt-dev', 'path', name], root)
        head = git(lane, 'rev-parse', 'HEAD') if lane else None
        lane_common = git(lane, 'rev-parse', '--path-format=absolute', '--git-common-dir') if lane else None
        if not head or lane_common != common or not base:
            ranked.append((0, name, 'lane/base unavailable'))
            continue
        fork = git(root, 'merge-base', '--fork-point', base, head) or git(root, 'merge-base', base, head)
        changed = git(lane, 'diff', '--name-only', f'{fork}...{head}') if fork else None
        score, reason = rank(changed.splitlines(), failing, sorted({p for ps in manifests.values() for p in ps}, key=len, reverse=True)) if changed is not None else (0, 'changed files unavailable')
        ranked.append((score, name, reason))
    for _, name, reason in sorted(ranked, key=lambda item: -item[0]):
        failure.emit(f'suspect {name}: {reason}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
