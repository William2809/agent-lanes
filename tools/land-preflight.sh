#!/bin/sh
# ABOUTME: Cheap whitespace and source syntax checks before the full land check.
set -eu
cd "$(dirname "$0")/.."
git diff --check "${1:-master}" --
python3 - <<'CHECK'
from pathlib import Path
import subprocess
import sys

files = subprocess.check_output([
    'git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'
]).split(b'\0')
for raw in sorted(set(files) - {b''}):
    path = Path(raw.decode('utf-8', 'surrogateescape'))
    if not path.is_file():
        continue
    with path.open('rb') as source:
        first = source.readline(160).rstrip()
    command = None
    if first.startswith((b'#!/bin/sh', b'#!/usr/bin/env sh')):
        command = ['sh', '-n', str(path)]
    elif first.startswith((b'#!/bin/bash', b'#!/usr/bin/env bash')):
        command = ['bash', '-n', str(path)]
    elif path.suffix == '.py' or first.startswith(b'#!/usr/bin/env python3'):
        try:
            compile(path.read_bytes(), str(path), 'exec')
        except (SyntaxError, ValueError):
            sys.exit(f'preflight: invalid Python syntax: {path}')
    elif first.startswith(b'#!/usr/bin/env node'):
        command = ['node', '--input-type=module', '--check']
    if command:
        result = subprocess.run(command, input=path.read_bytes() if command[0] == 'node' else None,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            sys.exit(f'preflight: invalid {command[0]} syntax: {path}')
print('preflight: whitespace and syntax checks pass')
CHECK
