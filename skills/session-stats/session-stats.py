#!/usr/bin/env python3
"""Read-only, streaming session metrics. Never emit transcript text or commands."""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

UTC = timezone.utc
KNOWN_TOOLS = set('Bash Read Write Edit MultiEdit Glob Grep Agent Task TaskOutput TaskStop Skill ToolSearch AskUserQuestion AskUserQuestion TodoWrite NotebookEdit WebFetch WebSearch SendMessage ListAgents Monitor Artifact EnterPlanMode ExitPlanMode'.split())
KNOWN_TOOLS.update('mcp__claude-in-chrome__tabs_context_mcp mcp__claude-in-chrome__navigate mcp__claude-in-chrome__computer mcp__claude-in-chrome__javascript_tool mcp__claude-in-chrome__resize_window mcp__claude-in-chrome__find mcp__claude-in-chrome__tabs_close_mcp mcp__claude-in-chrome__tabs_create_mcp mcp__claude-in-chrome__browser_batch mcp__claude-in-chrome__read_console_messages'.split())
EXIT = re.compile(r'(?:exit(?:ed)?(?:\s+with)?\s+(?:code|status)|exit[_-]code)[\s:=]+(-?\d+)', re.I)
UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)
OUTPUT_PATH = re.compile(r'(?:/|~/)[^\s<>\"\'`;|&()]*?\.output\b')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
# Command labels are fixed vocabulary: never echo arbitrary command tokens.
BASH_WORDS = set('cd cat tail head sed awk rg grep find ls wc du ps kill wait sleep echo printf git pnpm npm npx node python python3 bash sh zsh env export source curl wget ssh scp rsync chmod mkdir cp mv rm touch date sort uniq cut tr tee test true false for if while land land.sh remote-ci run-worker.sh pipeline.sh'.split())


def output_paths(text):
    return {str(Path(p).expanduser().resolve()) for p in OUTPUT_PATH.findall(text)}


def source_name(name, command):
    if name != 'Bash':
        return name
    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    while words and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0]):
        words = words[1:]
    # "cd <dir> && real-command": name the real command, not cd.
    while len(words) > 3 and words[0] == 'cd' and words[2] in ('&&', ';'):
        words = words[3:]
        while words and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0]):
            words = words[1:]
    word = Path(words[0]).name if words else ''
    return 'Bash/' + (word if word in BASH_WORDS else 'Other')


def image_count(value):
    if not isinstance(value, list):
        return 0
    return sum(1 for b in value if isinstance(b, dict) and b.get('type') == 'image')


def command_kinds(command):
    """Recognize invocations, excluding quoted text and heredoc script writes."""
    lines, delimiters = [], []
    for line in command.splitlines():
        if delimiters:
            if line.strip() == delimiters[0]:
                delimiters.pop(0)
            continue
        lines.append(line)
        delimiters.extend(m.group(2) for m in re.finditer(r"<<-?\s*(['\"]?)([\w-]+)\1", line))
    try:
        lexer = shlex.shlex('\n'.join(lines), posix=True, punctuation_chars=';|&()\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    segments, segment = [], []
    for token in tokens:
        if token and all(c in ';|&()\n' for c in token):
            if segment:
                segments.append(segment)
            segment = []
        else:
            segment.append(token)
    if segment:
        segments.append(segment)
    kinds = set()
    for words in segments:
        while words and (words[0] in ('if', 'then', 'elif', 'else', 'do', 'while', 'until', '!', '{', '}')
                         or re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0])):
            words = words[1:]
        while words and Path(words[0]).name in ('env', 'nohup', 'nice', 'time', 'sudo', 'command'):
            words = words[1:]
            while words and (words[0].startswith('-') or re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0])):
                words = words[1:]
        if not words:
            continue
        name = Path(words[0]).name
        args = words[1:]
        if name in ('bash', 'sh', 'zsh'):
            if any(a.startswith('-') and not a.startswith('--') and 'n' in a[1:] for a in args):
                continue  # shell syntax validation does not execute the script
            if '-c' in args and args.index('-c') + 1 < len(args):
                kinds.update(command_kinds(args[args.index('-c') + 1]))
                continue
            args = [a for a in args if not a.startswith('-')]
            if args:
                name, args = Path(args[0]).name, args[1:]
        if '--help' in args or '-h' in args:
            continue
        if name in ('land', 'land.sh'):
            kinds.add('land')
        if name in ('run-worker.sh', 'pipeline.sh'):
            kinds.add('launches')
        if name == 'pnpm' and args and args[0] in ('check', 'check:remote'):
            kinds.add('checks')
        if name == 'pnpm' and len(args) > 1 and args[0] == 'run' and args[1] in ('check', 'check:remote'):
            kinds.add('checks')
        if name == 'remote-ci' and args and args[0] == 'check':
            kinds.add('checks')
    return sorted(kinds)


def stamp(value):
    if not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()
    except (ValueError, OverflowError):
        return None


def text_of(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(b['text'] for b in value if isinstance(b, dict) and isinstance(b.get('text'), str))
    return ''


def safe_id(value):
    return value.lower() if UUID.fullmatch(value) else 'session-' + hashlib.sha256(value.encode()).hexdigest()[:12]


def safe_tool(value):
    if value in KNOWN_TOOLS:
        return value
    return 'MCP' if isinstance(value, str) and value.startswith('mcp__') else 'Other'


def union_seconds(intervals):
    end = None
    total = 0.0
    for a, b in sorted(intervals):
        if b <= a:
            continue
        total += max(0, b - max(a, end if end is not None else a))
        end = max(b, end) if end is not None else b
    return total


def outcome(text, metadata, error=False, status=None, land=False):
    text = ANSI.sub('', text)
    # Explicit failure wins; absence of a failure is not evidence of success.
    codes = [int(x) for x in EXIT.findall(text)]
    codes.extend(int(x) for x in re.findall(r'(?m)^\s*== finished rc=(-?\d+)\b', text))
    for key in ('exitCode', 'exit_code', 'returnCode'):
        if isinstance(metadata.get(key), int):
            codes.append(metadata[key])
    if error or status in ('failed', 'killed') or any(c != 0 for c in codes):
        return 'failed'
    if land and re.search(r'^\s*FAILED\b', text, re.M):
        return 'failed'
    if land and re.search(r'^\s*LANDED\b', text, re.M):
        return 'passed'
    if land:
        return 'unknown'
    if re.search(r'(?:^\s*(?:FAIL\b|FAILED\b|Error:|Failed:|fatal:|✖)|\bchecks? failed\b|^\s*(?:Test Files|Tests)\s+.*\b[1-9]\d* failed\b|\berror TS\d+\b|\bERR_[A-Z_]+\b|^.*command not found:)', text, re.I | re.M):
        return 'failed'
    if codes or status == 'completed' or metadata.get('success') is True:
        return 'passed'
    # A suite's "N passed" is only partial evidence. remote-ci's final rc
    # (or its exact-tree cache hit) establishes the whole check's outcome.
    if re.search(r'(?:^\s*PASS(?:ED)?\b|\bAll checks passed\b|\bchecks? (?:passed|successful)\b|^remote-ci: this exact code already passed on the (?:runner)\b)', text, re.I | re.M):
        return 'passed'
    return 'unknown'


def inspect(path, since, now):
    times, intervals = [], []
    turns, calls, tasks, outputs = {}, {}, {}, {}
    tools, sources = Counter(), Counter()
    images = 0
    seen_rows = set()
    bad = 0

    def finish(call, t, text='', meta=None, error=False, status=None):
        meta = meta if isinstance(meta, dict) else {}
        task = meta.get('backgroundTaskId')
        if not task:
            match = re.search(r'Command running in background with ID:\s*([\w-]+)', text)
            task = match.group(1) if match else None
        if task:
            tasks[str(task)] = call
            call['background'] = True
            for filename in output_paths(text + '\n' + str(meta.get('outputFile', ''))):
                outputs[filename] = call
            return
        settled = status in ('completed', 'failed', 'killed') or not call.get('background')
        for kind in call['kinds']:
            result = outcome(text, meta, error, status, kind == 'land')
            # Later notifications can settle an earlier unknown outcome.
            if result != 'unknown' and call['outcomes'].get(kind) != 'failed':
                call['outcomes'][kind] = result
            settled |= result != 'unknown'
        if t is not None and settled and call['end'] is None:
            call['end'] = max(t, call['start'])

    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    bad += 1
                    continue
                if not isinstance(row, dict):
                    bad += 1
                    continue
                t = stamp(row.get('timestamp'))
                if t is None or t > now:
                    continue
                uid = row.get('uuid')
                if isinstance(uid, str):
                    if uid in seen_rows:
                        continue
                    seen_rows.add(uid)
                times.append(t)
                m = row.get('message', {})
                m = m if isinstance(m, dict) else {}
                content = m.get('content', [])
                blocks = content if isinstance(content, list) else []
                if row.get('type') == 'assistant':
                    mid = m.get('id') or uid
                    if isinstance(mid, str):
                        info = turns.setdefault(mid, {'time': t, 'usage': [0, 0, 0]})
                        usage = m.get('usage', {})
                        if isinstance(usage, dict):
                            for i, key in enumerate(('input_tokens', 'output_tokens', 'cache_read_input_tokens')):
                                n = usage.get(key, 0)
                                if isinstance(n, (int, float)) and n >= 0:
                                    info['usage'][i] = max(info['usage'][i], int(n))
                    for b in blocks:
                        if not isinstance(b, dict) or b.get('type') != 'tool_use':
                            continue
                        cid = b.get('id')
                        if not isinstance(cid, str) or cid in calls:
                            continue
                        name = safe_tool(b.get('name'))
                        inp = b.get('input', {})
                        command = inp.get('command', '') if isinstance(inp, dict) else ''
                        command = command if isinstance(command, str) else ''
                        kinds = command_kinds(command) if name == 'Bash' else []
                        read_text = command + '\n' + str(inp.get('file_path', '')) if isinstance(inp, dict) else command
                        calls[cid] = {'start': t, 'end': None, 'kinds': kinds, 'outcomes': {},
                                      'source': source_name(name, command), 'reads': output_paths(read_text),
                                      'task_id': inp.get('task_id') if isinstance(inp, dict) and name == 'TaskOutput' else None}
                        if t >= since:
                            tools[name] += 1
                for b in blocks:
                    if not isinstance(b, dict) or b.get('type') != 'tool_result':
                        continue
                    call = calls.get(b.get('tool_use_id'))
                    result_text = text_of(b.get('content'))
                    if t >= since:
                        images += image_count(b.get('content'))
                        sources[call['source'] if call else 'Other'] += len(result_text)
                    if call:
                        finish(call, t, result_text, row.get('toolUseResult'), b.get('is_error') is True)
                        # The reader's exit code says nothing about the job it read.
                        if len(call['reads']) == 1:
                            original = outputs.get(next(iter(call['reads'])))
                            if original:
                                finish(original, t, result_text)
                        original = tasks.get(str(call['task_id']))
                        if original:
                            meta = row.get('toolUseResult', {})
                            task_meta = meta.get('task', {}) if isinstance(meta, dict) else {}
                            finish(original, t, result_text, task_meta,
                                   status=task_meta.get('status') if isinstance(task_meta, dict) else None)
                # Background Bash completion is commonly a user task-notification.
                notification = text_of(content)
                if '<task-notification>' in notification:
                    for snippet in re.findall(r'<task-notification>(.*?)</task-notification>', notification, re.S):
                        tid = re.search(r'<task-id>([^<]+)</task-id>', snippet)
                        state = re.search(r'<status>([^<]+)</status>', snippet)
                        tool_id = re.search(r'<tool-use-id>([^<]+)</tool-use-id>', snippet)
                        original = tasks.get(tid.group(1)) if tid else None
                        if original is None and tool_id:
                            original = calls.get(tool_id.group(1))
                        if original:
                            for filename in output_paths(snippet):
                                outputs[filename] = original
                            finish(original, t, re.sub(r'<[^>]*>', '\n', snippet),
                                   status=state.group(1) if state else None)
                attachment = row.get('attachment', {})
                if isinstance(attachment, dict) and attachment.get('type') == 'task_status':
                    call = tasks.get(str(attachment.get('taskId')))
                    status = attachment.get('status')
                    if call and status in ('completed', 'failed', 'killed'):
                        finish(call, t, status=status)
    except OSError:
        return None, 1
    times.sort()
    if not times or times[-1] < since:
        return None, bad
    # Task output may never have been read into the transcript. Stream only
    # explicitly referenced Bash output files; do not load whole logs.
    for filename, call in outputs.items():
        if not call['kinds'] or call['start'] < since:
            continue
        try:
            with Path(filename).open(encoding='utf-8', errors='replace') as stream:
                for entry in stream:
                    for kind in call['kinds']:
                        result = outcome(entry, {}, land=kind == 'land')
                        if result != 'unknown' and call['outcomes'].get(kind) != 'failed':
                            call['outcomes'][kind] = result
        except OSError:
            pass
    first, last = max(since, times[0]), times[-1]
    for a, b in zip(times, times[1:]):
        if b - a <= 300:
            intervals.append((max(a, since), min(b, now)))
    metrics = {k: {'runs': 0, 'seconds': 0.0, 'passed': 0, 'failed': 0, 'unknown': 0, 'timed': 0} for k in ('checks', 'land')}
    launches = 0
    for call in calls.values():
        end = call['end']
        if end is not None:
            intervals.append((max(call['start'], since), min(end, now)))
        if call['start'] < since:
            continue
        launches += 'launches' in call['kinds']
        for kind in ('checks', 'land'):
            if kind in call['kinds']:
                metric = metrics[kind]
                metric['runs'] += 1
                metric[call['outcomes'].get(kind, 'unknown')] += 1
                if end is not None:
                    metric['timed'] += 1
                    metric['seconds'] += max(0, end - call['start'])
    usage = [0, 0, 0]
    count = 0
    for turn in turns.values():
        if turn['time'] >= since:
            count += 1
            usage = [a + b for a, b in zip(usage, turn['usage'])]
    wall = max(0, last - first)
    active = min(wall, union_seconds(intervals))
    return {'session_id': safe_id(path.stem), 'wall_seconds': wall, 'active_seconds': active,
            'idle_seconds': wall - active, 'assistant_turns': count,
            'tokens': dict(zip(('input', 'output', 'cache_read'), usage)),
            'context_reread_tokens': usage[2], 'average_context_tokens': usage[2] / count if count else 0,
            'images_read': images, 'tool_result_characters': dict(sources),
            'tools': dict(sorted(tools.items())), **metrics, 'worker_launches': launches}, bad


def workers(project_path, since, now):
    result = dict(done=0, died=0, running=0, unknown=0)
    registry = Path.home() / '.claude/state/workers.tsv'
    seen = set()
    repositories = {}

    def repository(directory):
        if directory not in repositories:
            try:
                proc = subprocess.run(['git', '-C', str(directory), 'rev-parse',
                                       '--path-format=absolute', '--git-common-dir'],
                                      capture_output=True, text=True, timeout=5)
                repositories[directory] = Path(proc.stdout.strip()).resolve() if proc.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                repositories[directory] = None
        return repositories[directory]

    project_repo = repository(project_path)
    try:
        with registry.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                fields = line.rstrip('\n').split('\t')
                if len(fields) != 4:
                    continue
                start, pid, logfile, directory = fields
                try:
                    start, pid = float(start), int(pid)
                    directory = Path(directory).expanduser().resolve()
                    if not since <= start <= now:
                        continue
                    # Worker worktrees often sit beside the project directory.
                    if directory != project_path and (project_repo is None or repository(directory) != project_repo):
                        continue
                    key = (start, pid, logfile)
                    if key in seen:
                        continue
                    seen.add(key)
                    done = False
                    try:
                        with Path(logfile).expanduser().open(encoding='utf-8', errors='replace') as log:
                            for entry in log:
                                if entry.startswith('tokens used'):
                                    done = True
                                    break
                    except OSError:
                        pass
                    if done:
                        result['done'] += 1
                        continue
                    if pid <= 0:
                        result['died'] += 1
                        continue
                    try:
                        os.kill(pid, 0)
                        proc = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                                              capture_output=True, text=True, timeout=5)
                        result['running' if proc.returncode == 0 and 'codex' in proc.stdout else 'died'] += 1
                    except ProcessLookupError:
                        result['died'] += 1
                    except PermissionError:
                        result['died'] += 1
                except (OSError, ValueError, OverflowError, subprocess.TimeoutExpired):
                    result['unknown'] += 1
    except OSError:
        pass
    return result


def total_of(sessions):
    result = {'wall_seconds': 0, 'active_seconds': 0, 'idle_seconds': 0, 'assistant_turns': 0,
              'tokens': dict(input=0, output=0, cache_read=0), 'tools': {}, 'worker_launches': 0,
              'images_read': 0, 'context_reread_tokens': 0, 'tool_result_characters': {},
              **{k: dict(runs=0, seconds=0.0, passed=0, failed=0, unknown=0, timed=0) for k in ('checks', 'land')}}
    tools, sources = Counter(), Counter()
    for session in sessions:
        for key in ('wall_seconds', 'active_seconds', 'idle_seconds', 'assistant_turns', 'worker_launches', 'images_read', 'context_reread_tokens'):
            result[key] += session[key]
        for group in ('tokens', 'checks', 'land'):
            for key, value in session[group].items():
                result[group][key] += value
        tools.update(session['tools'])
        sources.update(session['tool_result_characters'])
    result['tools'] = dict(sorted(tools.items()))
    result['tool_result_characters'] = dict(sources)
    result['average_context_tokens'] = result['context_reread_tokens'] / result['assistant_turns'] if result['assistant_turns'] else 0
    return result


def duration(seconds):
    seconds = int(round(seconds))
    return f'{seconds // 3600}h{seconds % 3600 // 60:02}m{seconds % 60:02}s'


def render(session, label):
    tok = session['tokens']
    line = f"{label} wall={duration(session['wall_seconds'])} active={duration(session['active_seconds'])} idle={duration(session['idle_seconds'])} turns={session['assistant_turns']} tokens(i/o/cache)={tok['input']}/{tok['output']}/{tok['cache_read']}"
    details = []
    for kind in ('checks', 'land'):
        m = session[kind]
        outcomes = f"{m['passed']}/{m['failed']}/{m['unknown']}"
        details.append(f"{kind}={m['runs']} {duration(m['seconds'])} ({outcomes}; timed={m['timed']})")
    details.append(f"launches={session['worker_launches']}")
    details.append(f"images={session['images_read']} context re-read={session['context_reread_tokens']} avg/turn={session['average_context_tokens']:.0f}")
    top = sorted(session['tool_result_characters'].items(), key=lambda item: (-item[1], item[0]))[:5]
    details.append('top result chars=' + (','.join(f'{k}:{v}' for k, v in top) or 'none'))
    return line + ' | ' + ' '.join(details)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise echoes arbitrary invalid arguments.
        self.print_usage(sys.stderr)
        self.exit(2, 'Invalid arguments; use --help for supported options.\n')


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd(), help='project working directory (default: current directory)')
    parser.add_argument('--since', default='24h', help='24h, 7d, or YYYY-MM-DD (UTC midnight)')
    parser.add_argument('--session', help='one exact session ID')
    parser.add_argument('--json', action='store_true', help='all sessions as JSON')
    args = parser.parse_args()
    now = datetime.now(UTC)
    if args.since in ('24h', '7d'):
        since = now - timedelta(hours=24 if args.since == '24h' else 168)
    elif re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.since):
        try:
            since = datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=UTC)
        except ValueError:
            parser.error('invalid date')
    else:
        parser.error('--since requires 24h, 7d, or YYYY-MM-DD')
    if args.session and not UUID.fullmatch(args.session):
        parser.error('--session requires a UUID')
    project = args.project.expanduser().resolve()
    encoded = re.sub(r'[^A-Za-z0-9]', '-', str(project))
    folder = Path.home() / '.claude/projects' / encoded
    if not folder.is_dir():
        print('No transcript directory for this project.', file=sys.stderr)
        return 1
    files = [folder / (args.session + '.jsonl')] if args.session else sorted(folder.glob('*.jsonl'))
    if args.session and not files[0].is_file():
        print('Session not found.', file=sys.stderr)
        return 1
    sessions, bad = [], 0
    for path in files:
        session, errors = inspect(path, since.timestamp(), now.timestamp())
        bad += errors
        if session:
            sessions.append(session)
    sessions.sort(key=lambda s: s['wall_seconds'], reverse=True)
    totals = total_of(sessions)
    # Registry has no session IDs: never attribute workers to individual sessions.
    registry = None if args.session else workers(project, since.timestamp(), now.timestamp())
    report = {'since_utc': since.isoformat(), 'until_utc': now.isoformat(), 'sessions': sessions,
              'total': totals, 'workers': registry, 'skipped_lines_or_unreadable_files': bad,
              'method': {'idle_gap_seconds': 300, 'active': 'union of event gaps <=300s and completed tool intervals',
                         'wall': 'first to last timestamp clipped to window; totals sum sessions',
                         'tokens': 'maximum per assistant message ID; cache creation excluded',
                         'scope': 'top-level transcripts; calls/turns counted by start timestamp',
                         'context': 'sum cache_read per unique assistant message; average divides by assistant turns',
                         'tool_results': 'text characters (image payloads excluded); fixed tool/command labels',
                         'background': 'task IDs and referenced output files; file-only outcomes have no inferred completion time',
                         'workers': 'project and accessible Git worktrees; registry starts in window; tokens used marker=done, otherwise alive PID with codex command=running, else died; no session attribution'}}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Window {since.strftime('%Y-%m-%d %H:%M UTC')} to {now.strftime('%Y-%m-%d %H:%M UTC')}; {len(sessions)} sessions")
        print('Outcomes: checks=pass/fail/unknown; land=LANDED/FAILED/unknown. Durations cover timed calls.')
        for session in sessions[:19]:
            print(render(session, session['session_id']))
        if len(sessions) > 19:
            print(f'{len(sessions) - 19} more sessions included in TOTAL; use --json for all.')
        print(render(totals, 'TOTAL'))
        print('Workers: ' + ('unattributed (registry has no session IDs)' if registry is None else ' '.join(f'{k}={v}' for k, v in registry.items())))
        print(f'Active estimate: gaps <=5m plus tool intervals; totals sum sessions. Skipped/unreadable={bad}.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, OverflowError):
        print('Unable to read session metrics; no transcript data displayed.', file=sys.stderr)
        sys.exit(1)
