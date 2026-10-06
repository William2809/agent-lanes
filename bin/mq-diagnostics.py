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


def diagnostic(command, sha, lanes, timeout=45, tail=False):
    if not re.fullmatch(r'[0-9a-f]{7,40}', sha):
        return ''
    try:
        # A private process group also bounds SSH children. A file avoids waiting
        # for inherited pipe handles after the deadline and caps the bytes read.
        with tempfile.TemporaryFile() as output:
            args = ['remote-ci', command, *(['--tail'] if tail else []), sha, *lanes]
            process = subprocess.Popen(args,
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


# Transient storage faults only. Keep labels fixed: never log matched URLs or values.
# Network errors require storage context in the bounded tail; generic timeouts stay red.
STORAGE = re.compile(r'\b(?:R2|S3)\b|(?:r2\.cloudflarestorage\.com|s3[.-][\w.-]*amazonaws\.com)', re.I)
TRANSIENT_MARKERS = (
    ('InternalError', re.compile(r'\bInternalError\b'), True),
    ('We encountered an internal error. Please try again',
     re.compile(r'We encountered an internal error\. Please try again'), False),
    ('HTTP 503 SlowDown', re.compile(r'(?s)(?:\b503\b.{0,200}\bSlowDown\b|\bSlowDown\b.{0,200}\b503\b)'), True),
    ('ECONNRESET', re.compile(r'\bECONNRESET\b'), True),
    ('ETIMEDOUT', re.compile(r'\bETIMEDOUT\b'), True),
)


def transient(text):
    text = '\n'.join(line[:300] for line in text.splitlines()[-80:])
    for label, pattern, storage_required in TRANSIENT_MARKERS:
        match = pattern.search(text)
        if not match:
            continue
        context = text
        if label in ('ECONNRESET', 'ETIMEDOUT'):
            # Require the endpoint on the error line or an adjacent line.
            lines = text.splitlines()
            context = '\n'.join(line for i, line in enumerate(lines)
                                if any(pattern.search(x) for x in lines[max(0, i - 1):i + 2]))
        if not storage_required or STORAGE.search(context):
            return label
    return ''


def main():
    command, sha, *lanes = sys.argv[1:]
    text = diagnostic('blame' if command == 'suspect' else 'fail', sha,
                      lanes, tail=command == 'transient')
    if command == 'transient':
        # land can carry errors from local/custom checks without a remote log.
        print(transient(text + '\n' + sys.stdin.read(65536)))
        return
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
