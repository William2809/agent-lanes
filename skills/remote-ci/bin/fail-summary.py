#!/usr/bin/env python3
# ABOUTME: Read-only, bounded red-check summaries; every output field passes through redact.
import argparse
import json
from pathlib import Path
import re
import sys

ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
FILE = re.compile(r'(?:[\w@.+-]+/)*[\w.+-]+\.(?:[cm]?[jt]sx?|vue|svelte|json|sql)')


def redact(value):
    """One output boundary, also used recursively before serializing diagnostics."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    text = ANSI.sub('', value).replace('\t', ' ')
    text = re.sub(r'[\x00-\x1f\x7f]', '', text)
    text = re.sub(r'([a-z][a-z0-9+.-]*://)[^\s/\"<>]+@', r'\1***@', text, flags=re.I)
    text = re.sub(r'''\bAuthorization["']?\s*[:=].*''', 'Authorization: [redacted]', text, flags=re.I)
    text = re.sub(r'''([\w.-]*(?:password|passwd|token|secret|credential|key)[\w.-]*["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s&;,\)\]}]+)''', r'\1[redacted]', text, flags=re.I)
    text = re.sub(r'\bBearer\s+\S+', 'Bearer [redacted]', text, flags=re.I)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?', '[redacted]', text)
    text = re.sub(r'(?<![\w])[a-f0-9]{32,}(?![\w])', '[redacted]', text, flags=re.I)
    def token(match):
        # A long path prefix isn't a token. Preserve known diagnostic/log filenames;
        # punctuation such as a sentence-ending period must not defeat masking.
        suffix = text[match.end():]
        return match.group() if re.match(r'\.(?:(?:test|spec)\.)?(?:[cm]?[jt]sx?|log|json|vue|svelte|sql)\b', suffix) else '[redacted]'
    text = re.sub(r'(?<![\w/-])[A-Za-z0-9+/_-]{40,}={0,2}(?![\w/-])', token, text)

    return text


def emit(value, structured=False):
    safe = redact(value)
    print(json.dumps(safe) if structured else safe[:300])


def parse(text, path=''):
    text = ANSI.sub('', text)
    finishes = re.findall(r'^== finished rc=(\d+)', text, re.M)
    if not finishes or int(finishes[-1]) in (0, 3, 75):
        return dict(failed=False)
    files, tests, errors, step, eslint_file, package = [], [], [], '', '', ''
    for raw in text.splitlines():
        # Turbo's prefix identifies the package even when Vitest prints src/... paths.
        prefix = re.match(r'^([^\s:]+):([\w:-]+):\s*', raw)
        line = raw[prefix.end():].strip() if prefix else raw.strip()
        if prefix:
            package = prefix[1]
        if re.search(r'\bFAIL\s+', line):
            step = 'test'
            tail = re.split(r'\bFAIL\s+', line, maxsplit=1)[1]
            match = FILE.search(tail)
            if match:
                files.append(dict(file=match.group(), package=package if prefix else ''))
                name = tail[match.end():].strip(' >:')
                if name:
                    tests.append(name)
        elif re.match(r'[×✗]\s+', line):
            step = 'test'
            tests.append(re.sub(r'\s+\d+(?:\.\d+)?\s*(?:m?s|ms)\s*$', '', line[1:].strip()))
        elif re.search(r'\berror TS\d+', line):
            if step != 'test':
                step = 'typecheck'
            line = re.sub(r'\((\d+),(\d+)\)', r':\1:\2', line)
            errors.append(line)
            files.extend(dict(file=f, package=package if prefix else '') for f in FILE.findall(line.split(': error', 1)[0]))
        elif re.search(r'\b\d+:\d+\s+error\b|\bESLint\b.*\berror\b', line, re.I):
            if step not in ('test', 'typecheck'):
                step = 'lint'
            diagnostic = re.sub(r'^(\d+:\d+)\s+', r'\1: ', line)
            inline = FILE.search(line)
            errors.append(line if inline else (eslint_file + ':' if eslint_file else '') + diagnostic)
            files.extend(dict(file=f, package=package if prefix else '') for f in FILE.findall(line if inline else eslint_file))
        elif FILE.fullmatch(line) or (line.startswith('/') and FILE.search(line) and ' ' not in line):
            eslint_file = line
        elif re.match(r'(?:AssertionError|TypeError|Error):', line):
            errors.append(line)
        elif re.search(r'preflight.*(?:fail|error)|(?:fail|error).*preflight', line, re.I):
            step = 'preflight'
            errors.append(line)
    files = list({(f['file'], f['package']): f for f in files}.values())
    tests, errors = [list(dict.fromkeys(items)) for items in (tests, errors)]
    if not step:
        markers = re.findall(r'^== (.*)', text, re.M)
        commands = ' '.join(x for x in markers if not x.startswith(('finished', 'timing')))
        step = next((s for s, pattern in [('preflight', r'preflight'), ('typecheck', r'\b(?:tsc|typecheck)\b'), ('lint', r'\blint\b'), ('test', r'\b(?:vitest|test)\b')] if re.search(pattern, commands)), 'preflight')
    summary = [f'Failure: {step} (rc={finishes[-1]})']
    groups = [('File: ', [f['file'] + (f" (package {f['package']})" if f['package'] else '') for f in files]), ('Test: ', tests), ('Error: ', errors)]
    for i in range(max([len(items) for _, items in groups] + [0])):
        for prefix, items in groups:
            if i < len(items):
                summary.append(prefix + items[i])
    return dict(failed=True, files=files, summary=summary[:15], path=path)


def latest(logs, target, tail=False):
    candidates = []
    for file in Path(logs).glob('*.log'):
        try:
            # Filename contains the short SHA; headers disambiguate longer targets.
            if target != 'latest' and target[:8] not in file.name:
                continue
            candidates.append((file.stat().st_mtime_ns, file))
        except OSError:
            pass
    for _, file in sorted(candidates, reverse=True):
        try:
            text = file.read_text(errors='replace')
        except OSError:
            continue
        if target != 'latest' and not any(s.startswith(target) for s in re.findall(r'\bsha ([0-9a-f]{40})\b', text)):
            continue
        # Followers have no check output. Read the named owner's log for fail/blame.
        followed = re.search(r'^== following run ([a-zA-Z0-9_.-]+) sha ', text, re.M)
        if followed and parse(text)['failed']:
            owner = Path(logs) / (followed[1] + '.log')
            try:
                owner_text = owner.read_text(errors='replace')
                if parse(owner_text)['failed']:
                    text, file = owner_text, owner
            except OSError:
                pass
        result = parse(text, str(file))
        if tail and result['failed']:
            # Last 80 lines only; bound each line before the redaction boundary.
            result['summary'] = [line[:300] for line in text.splitlines()[-80:]]
        return result
    return dict(failed=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--logs', required=True)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--tail', action='store_true')
    parser.add_argument('target', nargs='?', default='latest')
    args = parser.parse_args()
    if args.target != 'latest' and not re.fullmatch(r'[0-9a-f]{7,40}', args.target):
        return 1
    result = latest(args.logs, args.target, args.tail)
    if args.json:
        emit(result, structured=True)
    elif result['failed']:
        for line in result['summary'] + ['Log: ' + result['path']]:
            emit(line)
    return 0 if result['failed'] else 1


if __name__ == '__main__':
    sys.exit(main())
