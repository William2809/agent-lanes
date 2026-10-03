#!/usr/bin/env python3
"""Daily, read-only agent loop metrics. No transcript content is emitted."""
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import statistics
import subprocess
import sys

BUCKETS = ('model/gaps', 'typecheck', 'targeted tests', 'full check (+slot wait)',
           'lint/preflight', 'install/build', 'BROWSER AUTOMATION', 'other')
REASONS = ('new', 'rework', 'bounce', 'review', 'review-fix', 'other')
EVENTS = ('queued', 'landing', 'landed', 'bounced', 'parked', 'requeued',
          'dropped', 'outside-claim')


def label(value):
    # Names only; malformed/free-text identifiers become opaque labels.
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,47}', value) and not re.search(
            r'(?:sk-|gh[pousr]_|AKIA|token|secret|password)', value, re.I):
        return value
    return 'id-' + hashlib.sha256(value.encode()).hexdigest()[:10]


def epoch(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError, OverflowError):
        return None


def lines(path):
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            yield from stream
    except OSError:
        return


def records(path):
    for line in lines(path):
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                yield row
        except ValueError:
            continue


def git(directory, *args):
    try:
        result = subprocess.run(['git', '-C', str(directory), *args],
                                capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def repository(directory):
    common = git(directory, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    return Path(common).parent if common else None


def read_workers(home, repo, root):
    result, seen, roots = [], set(), {}
    for line in lines(home / '.claude/state/workers.tsv'):
        fields = line.rstrip('\n').split('\t')
        if len(fields) != 4 or epoch(fields[0]) is None:
            continue
        start, pid, log, directory = fields
        path = Path(directory).expanduser()
        log = Path(log).expanduser()
        # Logs retain repo identity after a worktree has been deleted.
        belongs = log.parent.name == repo or path.name == repo or (root and path == root)
        if not belongs and not path.exists():
            belongs = repo in path.parts
        if not belongs and path.exists():
            if directory not in roots:
                roots[directory] = repository(path)
            belongs = root is not None and roots[directory] == root
        key = (float(start), pid, str(log))
        if not belongs or key in seen:
            continue
        seen.add(key)
        result.append(dict(start=float(start), name=log.name.split('.')[0],
                           log=log, cwd=directory, lane=path.name))
    return sorted(result, key=lambda r: r['start'])


def read_tags(home, repo):
    result = defaultdict(list)
    for line in lines(home / '.claude/state/worker-tags.tsv'):
        fields = line.rstrip('\n').split('\t')
        if len(fields) != 5:
            continue
        at, project, name, feature, reason = fields
        at = epoch(at)
        if at is not None and project == repo and reason in REASONS:
            result[name].append((at, feature, reason))
    return result


def tag_runs(runs, tags):
    # One nearby tag per launch, including tags written just after wk starts.
    # Never carry an old reason forward to an untagged resume.
    used = set()
    for run in runs:
        candidates = [(abs(at - run['start']), at, i, feature, reason)
                      for i, (at, feature, reason) in enumerate(tags.get(run['name'], []))
                      if abs(at - run['start']) <= 180 and (run['name'], i) not in used]
        if candidates:
            _, _, i, run['feature'], run['reason'] = min(candidates)
            used.add((run['name'], i))


def shell_segments(command):
    """Tokenize executable shell lines; omit comments and heredoc bodies."""
    delimiters = []
    for line in command.splitlines():
        if delimiters:
            if line.strip() == delimiters[0]:
                delimiters.pop(0)
            continue
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=';&|()<>')
            lexer.whitespace_split = True
            lexer.commenters = '#'
            tokens = list(lexer)
        except ValueError:
            continue
        for i, word in enumerate(tokens[:-1]):
            if word in ('<<', '<<-'):
                delimiters.append(tokens[i + 1])
        segment = []
        for word in tokens + [';']:
            if word in (';', '&&', '||', '|', '&', '(', ')'):
                if segment:
                    yield segment
                segment = []
            else:
                segment.append(word)


def browser_code(value):
    return bool(re.search(
        r"(?:require|import)\s*\(\s*['\"](?:playwright(?:-core)?|@playwright/test)['\"]|"
        r"(?:from|import)\s+['\"](?:playwright(?:-core)?|@playwright/test)['\"]",
        value))


def node_browser_heredoc(command):
    marker, node, body = None, False, []
    for line in command.splitlines():
        if marker:
            if line.strip() == marker:
                if node and browser_code('\n'.join(body)):
                    return True
                marker, node, body = None, False, []
            else:
                body.append(line)
            continue
        for words in shell_segments(line):
            for i, word in enumerate(words[:-1]):
                if word in ('<<', '<<-'):
                    marker = words[i + 1]
                    executable = next((w for w in words if not re.match(r'^[A-Za-z_]\w*=', w)), '')
                    node = Path(executable).name in ('node', 'nodejs')
                    break
    return False


def locator_timeouts(output):
    return len(re.findall(r'(?:locator\.[\w.]+: Timeout|Timeout[^\n]*locator|locator[^\n]*timed out)', output, re.I))


def categories(command):
    """Timebudget families, extended with explicit browser executions."""
    found = {6} if node_browser_heredoc(command) else set()
    for words in shell_segments(command):
        while words and (re.match(r'^[A-Za-z_][\w]*=', words[0]) or
                         words[0] in ('env', 'command', 'exec', 'then', 'do')):
            words = words[1:]
        if not words:
            continue
        name, args = Path(words[0]).name, words[1:]
        # Read/search/write arguments are never evidence of execution.
        if name in ('cat', 'rg', 'echo', 'printf', 'sed', 'grep', 'head', 'tail'):
            continue
        if name == 'git':
            continue
        if name in ('sh', 'bash', 'zsh'):
            if '-n' in args:
                continue
            if '-c' in args or '-lc' in args:
                index = args.index('-c' if '-c' in args else '-lc')
                if len(args) > index + 1:
                    found.update(k for k in categories(args[index + 1]) if k != 7)
                continue
            name = Path(args[0]).name if args else name
        if name in ('pnpm', 'npm', 'npx', 'bun', 'yarn'):
            if 'check:remote' in args:
                found.add(3)
            elif 'typecheck' in args or 'tsc' in args:
                found.add(1)
            elif 'playwright' in args:
                found.add(6)
            elif 'vitest' in args or any(re.fullmatch(r'test(?::[\w-]+)?', a) for a in args):
                found.add(2)
            elif any(re.fullmatch(r'(?:lint|format)(?::[\w-]+)?', a) for a in args):
                found.add(4)
            elif any(a in ('install', 'build', 'dev') for a in args):
                found.add(5)
        elif name in ('remote-ci', 'remote-ci') and 'check' in args:
            found.add(3)
        elif name == 'tsc' or name == 'turbo' and 'typecheck' in args:
            found.add(1)
        elif name == 'vitest':
            found.add(2)
        elif name in ('eslint', 'prettier', 'land-preflight.sh', 'check-file-size.mjs',
                      'check-files.sh') or name == 'node' and args and Path(args[0]).name in (
                          'check-file-size.mjs', 'check-files.mjs'):
            found.add(4)
        elif name == 'next' and any(a in ('build', 'dev') for a in args) or (
                name == 'wt-dev' and any(a in ('new', 'start', 'restart', 'stop') for a in args)):
            found.add(5)
        elif name in ('playwright', 'pw-run', 'ui-shots', 'agent-browser'):
            found.add(6)
        elif name in ('node', 'nodejs') and any(browser_code(a) or (
                not a.startswith('-') and re.fullmatch(r'[\w/.-]*(?:playwright|pw-run|ui-shots)[\w/.-]*\.(?:js|mjs|cjs)', a)) for a in args):
            found.add(6)
    return sorted(found) or [7]


def tool_command(value):
    if isinstance(value, dict):
        return value.get('cmd', '') if isinstance(value.get('cmd', ''), str) else ''
    if not isinstance(value, str):
        return ''
    try:
        obj = json.loads(value)
        if isinstance(obj, dict):
            return obj.get('cmd', '') if isinstance(obj.get('cmd', ''), str) else ''
    except ValueError:
        pass
    # functions.exec wraps exec_command calls in JS string literals.
    commands = []
    for match in re.finditer(r'\bcmd\s*:\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')', value):
        try:
            commands.append(json.loads(match[1]) if match[1][0] == '"' else
                            match[1][1:-1].replace("\\'", "'").replace('\\n', '\n'))
        except ValueError:
            pass
    return '\n'.join(commands)


def session_index(home, runs, until):
    directories = {r['cwd'] for r in runs}
    result = {}
    for path in sorted((home / '.codex/sessions').rglob('*.jsonl')):
        stream = records(path)
        first = next(stream, {})
        meta = first.get('payload', {})
        if not isinstance(meta, dict) or meta.get('cwd') not in directories:
            continue
        starts = []
        for row in stream:
            at = timestamp(row.get('timestamp'))
            payload = row.get('payload', {})
            if at is not None and at <= until and isinstance(payload, dict) and payload.get('type') == 'task_started':
                starts.append(at)
        if not starts:
            at = timestamp(first.get('timestamp'))
            starts = [at] if at is not None else []
        sid = meta.get('id', meta.get('session_id'))
        if sid and starts:
            result[sid] = dict(path=path, cwd=meta['cwd'], starts=starts)
    return result


def match_runs(runs, sessions):
    headers, by_name = {}, defaultdict(set)
    paths = {r['log'] for r in runs}
    for folder in {p.parent for p in paths}:
        paths.update(folder.glob('*.log'))
    for path in paths:
        try:
            with path.open(errors='replace') as stream:
                head = stream.read(5000)
        except OSError:
            continue
        match = re.search(r'session id:\s*([\w-]+)', head)
        if match and match[1] in sessions:
            headers[path] = match[1]
            by_name[path.name.split('.')[0]].add(match[1])
    used = set()
    for run in runs:
        candidates = set(by_name[run['name']])
        if run['log'] in headers:
            candidates.add(headers[run['log']])
        pairs = [(abs(at - run['start']), sid, at) for sid in candidates
                 for at in sessions[sid]['starts'] if (sid, at) not in used]
        match = min(pairs) if pairs else None
        run['match'] = 'log-session-id'
        if match is None or match[0] >= 180:
            pairs = [(abs(at - run['start']), sid, at) for sid, meta in sessions.items()
                     if meta['cwd'] == run['cwd'] for at in meta['starts']
                     if abs(at - run['start']) < 60 and (sid, at) not in used]
            match = min(pairs) if pairs else None
            run['match'] = 'cwd/start estimate'
        if match and match[0] < 180:
            _, run['sid'], run['begin'] = match
            used.add((run['sid'], run['begin']))
        else:
            run['match'] = 'unmatched'


def parse_session(path):
    commands, calls, tools, finishes = {}, {}, [], []
    for row in records(path):
        at, payload = timestamp(row.get('timestamp')), row.get('payload', {})
        if at is None or not isinstance(payload, dict):
            continue
        kind = payload.get('type')
        if kind == 'task_complete':
            finishes.append(at)
        item = payload.get('item', {})
        if isinstance(item, dict) and item.get('type') == 'CommandExecution':
            duration = item.get('duration', 0)
            if isinstance(duration, dict):
                duration = (epoch(duration.get('secs', 0)) or 0) + (epoch(duration.get('nanos', 0)) or 0) / 1e9
            duration = max(0, epoch(duration) or 0)
            cmd = item.get('command', '')
            cmd = cmd[-1] if isinstance(cmd, list) and cmd else cmd
            output = item.get('aggregated_output') or item.get('stdout') or item.get('formatted_output') or ''
            output = output if isinstance(output, str) else ''
            # Retain only hashes and counts from output, never its content.
            hashes = re.findall(r'^\[[^\]\n]+\s([0-9a-f]{7,40})\]', output, re.M)
            cats = categories(cmd) if isinstance(cmd, str) else [7]
            timeouts = locator_timeouts(output) if 6 in cats else 0
            commands[item.get('id', str(at))] = dict(start=at-duration, end=at,
                cats=cats,
                sleep=isinstance(cmd, str) and any(Path(s[0]).name == 'sleep' for s in shell_segments(cmd)),
                status=item.get('status'), exit=item.get('exit_code'), hashes=hashes,
                locator_timeouts=timeouts)
        if kind in ('function_call', 'custom_tool_call'):
            name = payload.get('name', '')
            name = name if isinstance(name, str) else ''
            cats = categories(tool_command(payload.get('arguments', payload.get('input', ''))))
            if re.search(r'(?:playwright|pw_run|ui_shots|agent_browser|node_repl|cua_repl)', name, re.I):
                cats = [6]
            calls[payload.get('call_id')] = (at, cats, name)
        if kind in ('function_call_output', 'custom_tool_call_output'):
            call = calls.pop(payload.get('call_id'), None)
            if call:
                a, cats, name = call
                # Tools with no shell command remain other, as in timebudget.
                output = payload.get('output', '')
                timeouts = locator_timeouts(output) if cats == [6] and isinstance(output, str) else 0
                tools.append(dict(start=a, end=at, cats=cats, sleep='sleep' in name, locator_timeouts=timeouts))
    return list(commands.values()), tools, finishes


def allocate(start, end, intervals):
    events = defaultdict(list)
    for i, (a, b, cats, priority) in enumerate(intervals):
        a, b = max(start, a), min(end, b)
        if b > a:
            events[a].append((True, i))
            events[b].append((False, i))
    events[start]; events[end]
    sums, active, previous = [0.0] * len(BUCKETS), set(), start
    for at in sorted(events):
        if active:
            priority = max(intervals[i][3] for i in active)
            cats = {k for i in active if intervals[i][3] == priority for k in intervals[i][2]}
            if len(cats) > 1:
                cats.discard(7)
        else:
            cats = {0}
        for k in cats:
            sums[k] += (at - previous) / len(cats)
        for add, i in events[at]:
            active.add(i) if add else active.discard(i)
        previous = at
    return sums


def measure_runs(runs, sessions, until):
    grouped = defaultdict(list)
    for run in runs:
        run['status'] = 'unmatched'
        if 'sid' in run:
            grouped[run['sid']].append(run)
    for sid, group in grouped.items():
        commands, tools, finishes = parse_session(sessions[sid]['path'])
        for run in group:
            begin = run['begin']
            stop = min([until] + [t for t in sessions[sid]['starts'] if t > begin + .001])
            ends = [t for t in finishes if begin <= t < stop]
            run['status'] = 'incomplete'
            if not ends or min(ends) < run['start']:
                continue
            end = min(ends)
            cs = [dict(c) for c in commands if begin <= c['end'] <= end and c['status'] == 'completed']
            ts = [dict(t) for t in tools if t['end'] >= begin and t['start'] <= end]
            remote = [c for c in cs if 3 in c['cats']]
            for c in cs + ts:
                if c['sleep'] and any(0 <= c['start'] - r['end'] < 100 and r['exit'] == 75 for r in remote):
                    c['cats'] = [3]
            for t in ts:
                if any(c['start'] <= t['start'] and c['end'] >= t['end'] for c in remote):
                    t['cats'] = [3]
            intervals = [(c['start'], c['end'], c['cats'], 2) for c in cs]
            intervals += [(t['start'], t['end'], t['cats'], 1) for t in ts]
            run.update(status='complete', end=end, seconds=allocate(run['start'], end, intervals),
                       locator_timeouts=sum(c['locator_timeouts'] for c in cs) + sum(t['locator_timeouts'] for t in ts if not any(c['start'] <= t['start'] and c['end'] >= t['end'] for c in cs)),
                       hashes=[h for c in cs for h in c['hashes']])


def read_events(home, repo):
    rows = []
    for line in lines(home / '.claude/state/mq' / repo / 'events'):
        fields = line.rstrip('\n').split('\t', 3)
        if len(fields) >= 3 and epoch(fields[0]) is not None and fields[2] in EVENTS:
            rows.append((float(fields[0]), fields[1], fields[2], fields[3] if len(fields) == 4 else ''))
    return sorted(rows, key=lambda r: r[0])


def median(values):
    return statistics.median(values) if values else None


def queue_stats(events, start, end):
    pending, landing = {}, {}
    waits, lands, counts = [], [], Counter()
    lanes = defaultdict(set)
    for at, lane, event, _ in events:
        if at >= end:
            break
        today = at >= start
        if today:
            counts[event] += 1
            lanes[event].add(lane)
        if event in ('queued', 'requeued'):
            if event == 'requeued':
                pending[lane] = at
            else:
                pending.setdefault(lane, at)
            landing.pop(lane, None)
        elif event == 'landing':
            queued = pending.pop(lane, None)
            if today and queued is not None:
                waits.append(at - queued)
            landing[lane] = at  # A new attempt replaces an unclosed attempt.
        elif event == 'landed':
            began = landing.pop(lane, None)
            if today and began is not None:
                lands.append(at - began)
            pending.pop(lane, None)
        elif event in ('bounced', 'parked', 'dropped'):
            pending.pop(lane, None)
            landing.pop(lane, None)
    return dict(events={e: counts[e] for e in EVENTS},
                lanes={e: len(lanes[e]) for e in EVENTS},
                median_queue_wait_seconds=median(waits), median_land_seconds=median(lands),
                queue_wait_samples=len(waits), land_samples=len(lands),
                has_data=any(start <= row[0] < end for row in events))


def reflog_landings(root):
    if root is None:
        return []
    branch = git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    if not branch:
        return []
    rows = []
    output = git(root, 'reflog', 'show', branch, '--date=unix', '--format=%gD%x09%H%x09%gs')
    for line in output.splitlines():
        fields = line.split('\t', 2)
        if len(fields) == 3 and fields[2].startswith('merge '):
            match = re.search(r'@\{(\d+)\}', fields[0])
            if match:
                rows.append((float(match[1]), fields[1]))
    return sorted(set(rows))


def feature_stats(runs, events, reflog, start, end):
    # Reflog is authoritative when an event's landing hash matches it.
    landings = {(at, sha): None for at, sha in reflog if start <= at < end}
    for at, lane, event, detail in events:
        if event != 'landed' or not start <= at < end:
            continue
        match = re.match(r'([0-9a-f]{7,40})\b', detail)
        matches = [key for key in landings if match and key[1].startswith(match[1])]
        key = min(matches, key=lambda k: abs(k[0] - at)) if matches else (at, '')
        landings[key] = lane
    last, unlinked = {}, 0
    for (at, sha), lane in landings.items():
        candidates = [r for r in runs if r.get('status') == 'complete' and r['end'] <= at
                      and (lane and lane in (r['name'], r['lane']) or
                           sha and any(sha.startswith(h) for h in r.get('hashes', [])))]
        run = max(candidates, key=lambda r: r['end']) if candidates else None
        if run is None or 'feature' not in run:
            unlinked += 1
            continue
        feature = run['feature']
        last[feature] = max(at, last.get(feature, 0))
    rows = []
    for feature, at in sorted(last.items()):
        first = min(r['start'] for r in runs if r.get('feature') == feature and r['start'] <= at)
        rows.append(dict(feature=label(feature), launch_to_last_landing_hours=(at-first)/3600,
                         estimate=True))
    return rows, len(landings), unlinked


def daily(runs, events, reflog, start, end, day):
    cohort = [r for r in runs if start <= r['start'] < end]
    complete = [r for r in cohort if r['status'] == 'complete']
    reasons = Counter(r.get('reason', 'untagged') for r in cohort)
    rebuilds = Counter(label(r['feature']) for r in cohort if r.get('reason') == 'rework')
    features, landed, unlinked = feature_stats(runs, events, reflog, start, end)
    return dict(day=day, workers=dict(runs=len(cohort), completed=len(complete),
        incomplete=sum(r['status'] == 'incomplete' for r in cohort),
        unmatched=sum(r['status'] == 'unmatched' for r in cohort),
        fallback_matches=sum(r['match'] == 'cwd/start estimate' for r in cohort),
        bucket_seconds={name: sum(r['seconds'][i] for r in complete) for i, name in enumerate(BUCKETS)},
        locator_timeouts=sum(r['locator_timeouts'] for r in complete),
        reasons={k: reasons[k] for k in (*REASONS, 'untagged')},
        rebuild_rounds=dict(sorted(rebuilds.items()))), queue=queue_stats(events, start, end),
        features=features, landings=landed, unlinked_landings=unlinked)


def report(home, repo, root, day, days, now=None):
    now = datetime.now().timestamp() if now is None else now
    # Local calendar midnights preserve the host timezone, including DST.
    dates = [day - timedelta(days=i) for i in reversed(range(days))]
    end = datetime.combine(day + timedelta(days=1), time()).timestamp()
    until = min(now, end)
    runs = read_workers(home, repo, root)
    runs = [r for r in runs if r['start'] < until]
    tag_runs(runs, read_tags(home, repo))
    sessions = session_index(home, runs, until)
    match_runs(runs, sessions)
    measure_runs(runs, sessions, until)
    events, reflog = read_events(home, repo), reflog_landings(root)
    trend = [daily(runs, events, reflog, datetime.combine(d, time()).timestamp(),
                   min(now, datetime.combine(d + timedelta(days=1), time()).timestamp()),
                   d.isoformat()) for d in dates]
    return dict(repo=label(repo), day=day.isoformat(), daily=trend[-1], trend=trend,
                method=dict(time='completed launch-day cohorts; cumulative worker seconds, including startup',
                    intervals='deduplicated item IDs; command intervals override wrappers; union; mixed families share equally (estimate)',
                    gaps='wall minus command/tool union; upper-bound model proxy, includes idle (estimate)',
                    matching='log session IDs + task_started within 180s; equal cwd within 60s fallback (estimate); no mtime',
                    tags='nearest unused same-repo/name tag within 180s of launch; untagged resumes stay untagged',
                    browser='explicit executable names/inline node browser code and browser/node REPL tools; opaque scripts may be other',
                    timeouts='deduplicated completed browser-command/tool locator timeout markers; lower bound',
                    queue='distinct lanes plus event counts; queue pairs end on landing day, land pairs end on landed day; requeue starts a new wait',
                    features='first recorded tagged launch to last linked landing (estimate); current-branch merge reflog plus mq landed events',
                    missing='no evidence is no data; numeric trends use 0 for empty counts/hours, -1 for missing medians',
                    timezone='host local calendar days'))


def render(result):
    d, trend = result['daily'], result['trend']
    w, q = d['workers'], d['queue']
    def minutes(value):
        return 'no data' if value is None else f'{value/60:.2f}m'
    output = [f"loop-stats {result['repo']} {result['day']} (local day; estimates)",
              f"Workers: {w['runs']} runs; {w['completed']} complete; {w['incomplete']} incomplete; {w['unmatched']} unmatched; {w['fallback_matches']} fallback matches"]
    output.append('Worker hours (completed launch-day cohorts; cumulative):' if w['completed'] else 'Worker time: no data')
    if w['completed']:
        output += [f'  {name}: {seconds/3600:.2f}' for name, seconds in w['bucket_seconds'].items()]
    output.append(f"Locator timeouts (lower bound): {w['locator_timeouts']}" if w['completed'] else 'Locator timeouts: no data')
    output.append('Runs by reason: ' + ' '.join(f'{k}={v}' for k, v in w['reasons'].items()) if w['runs'] else 'Runs by reason: no data')
    output.append('Rebuild rounds (rework): ' + (' '.join(f'{k}={v}' for k, v in w['rebuild_rounds'].items()) or 'no data'))
    output.append('Queue lanes: ' + ' '.join(f'{k}={q["lanes"][k]}' for k in ('queued', 'landed', 'bounced', 'parked')) +
                  f"; outside-claim={q['events']['outside-claim']}" if q['has_data'] else 'Queue: no data')
    output.append(f"Queue median wait={minutes(q['median_queue_wait_seconds'])}; land={minutes(q['median_land_seconds'])}")
    output.append(f"Landings: {d['landings']}; without feature link={d['unlinked_landings']}")
    features = ' '.join(f"{r['feature']}={r['launch_to_last_landing_hours']:.2f}h" for r in d['features'])
    output.append('Feature first launch → last landing (estimate): ' + (features or 'no data'))
    output.append(f"Trend {trend[0]['day']}..{trend[-1]['day']} (oldest first; 0=empty, -1=missing median):")
    metrics = {
        'worker hours': lambda x: sum(x['workers']['bucket_seconds'].values()) / 3600,
        'browser hours': lambda x: x['workers']['bucket_seconds']['BROWSER AUTOMATION'] / 3600,
        'locator timeouts': lambda x: x['workers']['locator_timeouts'],
        'runs': lambda x: x['workers']['runs'],
        'rebuild rounds': lambda x: sum(x['workers']['rebuild_rounds'].values()),
        'landings': lambda x: x['landings'],
        'bounced lanes': lambda x: x['queue']['lanes']['bounced'],
        'queue wait min': lambda x: x['queue']['median_queue_wait_seconds'] / 60 if x['queue']['median_queue_wait_seconds'] is not None else -1,
        'land min': lambda x: x['queue']['median_land_seconds'] / 60 if x['queue']['median_land_seconds'] is not None else -1,
    }
    output += [name + ': ' + ' '.join(f'{fn(x):.2f}' for x in trend) for name, fn in metrics.items()]
    output.append('Gaps include idle; mixed/collected intervals are estimates; unlinked features excluded. --json for all metrics.')
    return '\n'.join(output)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, 'Invalid arguments; use --help.\n')


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument('--repo', help='repository name (default: current Git repository)')
    parser.add_argument('--day', default=date.today().isoformat(), help='local YYYY-MM-DD')
    parser.add_argument('--days', type=int, default=7, help='trend length ending on --day (default: 7)')
    parser.add_argument('--json', action='store_true', help='all metrics and definitions')
    args = parser.parse_args()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.day) or not 1 <= args.days <= 366:
        parser.error('invalid date or days')
    try:
        day = date.fromisoformat(args.day)
        day - timedelta(days=args.days - 1)
    except (ValueError, OverflowError):
        parser.error('invalid date')
    root = repository(Path.cwd())
    repo = args.repo or (root or Path.cwd()).name
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', repo) or repo in ('.', '..'):
        parser.error('invalid repo')
    home = Path.home()
    if root is None or root.name != repo:
        # Resolve named repos from the ledger or a sibling of cwd, never guess a branch.
        candidates = [Path.cwd().parent / repo]
        candidates += [Path(r['cwd']) for r in read_workers(home, repo, None)]
        root = None
        for candidate in candidates:
            if candidate.exists():
                common = repository(candidate)
                if common and common.name == repo:
                    root = common
                    break
    result = report(home, repo, root, day, args.days)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        print('Unable to read loop metrics; no source content displayed.', file=sys.stderr)
        sys.exit(1)
