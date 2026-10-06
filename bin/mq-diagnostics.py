#!/usr/bin/env python3
# ABOUTME: Bound and redact queue diagnostics; accept only a unique, real blame overlap.
import importlib.util
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    'failure', Path(__file__).resolve().parents[1] / 'skills/remote-ci/bin/fail-summary.py')
failure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(failure)


def diagnostic(command, sha, lanes, timeout=45):
    if not re.fullmatch(r'[0-9a-f]{7,40}', sha):
        return ''
    try:
        # A private process group also bounds SSH children. A file avoids waiting
        # for inherited pipe handles after the deadline and caps the bytes read.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(['remote-ci', command, sha, *lanes],
                                       stdout=output, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
            try:
                rc = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                return ''
            if rc:
                return ''
            output.seek(0)
            return output.read(65536).decode(errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        return ''


def suspect(text, lanes):
    ranked = []
    for line in text.splitlines():
        match = re.fullmatch(r'suspect ([A-Za-z0-9_.-]+): (.+)', line)
        if not match or match[1] not in lanes:
            return ''
        reason = match[2]
        score = (3 if reason.startswith('touches failing file ') else
                 2 if re.fullmatch(r'touches .+ in failing directory .+', reason) else
                 1 if re.fullmatch(r'touches .+ in failing package .+', reason) else 0)
        ranked.append((score, match[1], reason))
    if len(ranked) != len(lanes) or {r[1] for r in ranked} != set(lanes):
        return ''
    ranked.sort(reverse=True)
    if not ranked or not ranked[0][0] or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
        return ''
    return f'{ranked[0][1]}: {ranked[0][2]}'


def main():
    command, sha, *lanes = sys.argv[1:]
    text = diagnostic('blame' if command == 'suspect' else 'fail', sha, lanes)
    # Bound input before matching or redacting; long lines can make regex work costly.
    text = '\n'.join(line[:300] for line in text.splitlines()[:16])
    if command == 'suspect':
        text = suspect(text, lanes)
    # remote-ci already redacts its output. Keep that boundary even with alternate clients.
    lines = [failure.redact(line)[:300] for line in text.splitlines() if line.strip()]
    if lines:
        print('\n'.join(lines))


if __name__ == '__main__':
    main()
