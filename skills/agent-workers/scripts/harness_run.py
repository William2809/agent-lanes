#!/usr/bin/env python3
"""ABOUTME: Runs one worker through a non-Codex harness (cursor, opencode, pi, omp) and writes a Codex-shaped log.
ABOUTME: Header lines and events; writes the final report to AGENT_LANES_LOG.last under the shared supervisor.

Usage: harness_run.py check  HARNESS SANDBOX                                 exit 2 + reason if unsupported
       harness_run.py run    HARNESS MODEL EFFORT SANDBOX DIR PROMPT_FILE    log on stdout
       harness_run.py resume OLD_LOG MESSAGE [EFFORT]                        same session, model, sandbox, dir

Legacy transcript format (resume headers remain in use): `key: value` header lines in the first 20 lines
(workdir, model, harness, sandbox, reasoning effort, session id), `tokens used` + a count line + the
final message on success, `ERROR: ...` as the last line on failure. Sandbox per harness:
  read-only        cursor --mode ask · opencode: injected agent (edit denied; shell only git diff/log/show/status,
                   no redirects or --output) · pi: tools read,grep,find,ls (no shell) · omp: refused (untested)
  workspace-write  cursor --sandbox enabled (pi, opencode, omp have no workspace sandbox: refused)
  full-access      cursor --sandbox disabled · opencode --auto · pi all tools · omp --approval-mode=yolo
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

HARNESSES = ('cursor', 'opencode', 'pi', 'omp')
BIN_ENV = {'cursor': 'CURSOR_AGENT_BIN', 'opencode': 'OPENCODE_BIN', 'pi': 'PI_BIN', 'omp': 'OMP_BIN'}
BIN_NAMES = {'cursor': ('cursor-agent', 'agent'), 'opencode': ('opencode',), 'pi': ('pi',), 'omp': ('omp',)}
SANDBOXES = {'cursor': ('read-only', 'workspace-write', 'full-access'),
             'opencode': ('read-only', 'full-access'), 'pi': ('read-only', 'full-access'),
             'omp': ('full-access',)}
READ_TOOLS = 'read,grep,find,ls'  # pi built-ins without bash, edit or write
# opencode's own permission engine enforces this (probed live 2026-10-06: no write tool offered; chains,
# redirects, --output and task subagents denied). The agent name is ours, so a user's opencode.json cannot widen it.
OPENCODE_READONLY = json.dumps({'agent': {'agentops-readonly': {
    'mode': 'primary', 'description': 'agent-lanes read-only reviewer',
    # Tool allowlist first: `task` subagents (probed: they ignore this agent's permissions and wrote a file)
    # and the user's MCP tools stay off.
    'tools': {'*': False, 'read': True, 'grep': True, 'glob': True, 'list': True, 'bash': True, 'webfetch': True},
    # `git diff*` would also match `git difftool --extcmd=...`: allow each command bare or with a space.
    'permission': {'edit': 'deny', 'task': 'deny', 'webfetch': 'allow', 'bash': {
        '*': 'deny', **{f'git {c}{a}': 'allow' for c in ('diff', 'log', 'show', 'status') for a in ('', ' *')},
        '*--output*': 'deny', '*--ext-diff*': 'deny', '*>*': 'deny'}}}}})


def fail(msg, code=2):
    print(f'harness_run: {msg}', file=sys.stderr)
    sys.exit(code)


def binary(harness):
    path = os.environ.get(BIN_ENV[harness])
    if path:
        return path if os.access(path, os.X_OK) else None
    return next((p for p in map(shutil.which, BIN_NAMES[harness]) if p), None)


def check(harness, sandbox):
    if harness not in HARNESSES:
        fail(f'unknown harness {harness} (one of codex {" ".join(HARNESSES)})')
    if sandbox not in SANDBOXES[harness]:
        fail(f'{harness} has no {sandbox} mode (it supports {", ".join(SANDBOXES[harness])}); '
             'set the preset sandbox to one of those')
    if not binary(harness):
        fail(f'{harness} CLI not found ({" or ".join(BIN_NAMES[harness])} on PATH, or {BIN_ENV[harness]})')


def argv(harness, model, effort, sandbox, session, message):
    """Command line for one non-interactive run; session = id to resume or pre-assigned id."""
    exe = binary(harness)
    if harness == 'cursor':
        mode = {'read-only': ['--mode', 'ask'], 'workspace-write': ['--force', '--sandbox', 'enabled'],
                'full-access': ['--force', '--sandbox', 'disabled']}[sandbox]
        return [exe, '-p', '--output-format', 'stream-json', '--trust', '--model', model, *mode,
                *(['--resume', session] if session else []), message]
    if harness == 'opencode':
        mode = ['--agent', 'agentops-readonly'] if sandbox == 'read-only' else ['--auto']
        return [exe, 'run', '--format', 'json', '--dir', os.environ['PWD'], '-m', model, *(['--variant', effort] if effort else []), *mode,
                *(['--session', session] if session else []), '--', message]
    if harness == 'pi':
        tools = ['--tools', READ_TOOLS] if sandbox == 'read-only' else []
        return [exe, '-p', '--mode', 'json', '--model', model, '--thinking', effort, '-a', *tools,
                '--session-id', session, '--', message]
    return [exe, '-p', '--mode', 'json', f'--model={model}', f'--thinking={effort}', '--approval-mode=yolo',
            *([f'--resume={session}'] if session else []), message]


def oneline(text, n=300):
    s = ' '.join(str(text).split())
    return s if len(s) <= n else s[:n] + '…'


class Run:
    """Turns one harness's JSON event stream into log lines and remembers session, tokens, final text, error."""

    def __init__(self, harness, session):
        self.harness, self.session = harness, session
        self.tokens, self.final, self.error = 0, '', ''
        self.texts = {}  # opencode: message id -> text parts
        self.pending = []  # body lines held back until the session id is in the header

    def saw_session(self, sid):
        if sid and not self.session:
            self.session = sid
            emit(f'session id: {sid}\n--------')
            self.flush()

    def flush(self):
        for line in self.pending:
            emit(line)
        self.pending = []

    def body(self, line):
        # Startup noise before the first event must not push `session id:` past the 20 header lines.
        # Held back up to 200 lines; past that it streams (a run that long without a session is broken anyway).
        if self.session or len(self.pending) >= 200:
            self.flush()
            emit(line)
        else:
            self.pending.append(line)

    def event(self, e):
        getattr(self, 'ev_' + ('pi' if self.harness == 'omp' else self.harness))(e)

    def ev_pi(self, e):
        t = e.get('type')
        if t == 'session':
            self.saw_session(e.get('id'))
        elif t == 'message_end' and e.get('message', {}).get('role') == 'assistant':
            m = e['message']
            self.tokens += int((m.get('usage') or {}).get('totalTokens') or 0)
            text = '\n'.join(c.get('text', '') for c in m.get('content', []) if c.get('type') == 'text').strip()
            for c in m.get('content', []):
                if c.get('type') == 'toolCall':
                    self.body(f'exec {c.get("name")} {oneline(json.dumps(c.get("arguments")), 200)}')
            self.final = text  # only the last assistant message is the report
            if m.get('stopReason') in ('error', 'aborted'):
                self.error = m.get('errorMessage') or f'run {m.get("stopReason")}'
            elif text:
                self.body(f'assistant\n{text}')
                self.error = ''
        elif t == 'tool_execution_end':
            out = ' '.join(c.get('text', '') for c in (e.get('result') or {}).get('content', []) if isinstance(c, dict))
            self.body(f'{e.get("toolName")} {"failed" if e.get("isError") else "ok"}: {oneline(out, 160)}')

    def ev_opencode(self, e):
        self.saw_session(e.get('sessionID'))
        t, part = e.get('type'), e.get('part') or {}
        if t == 'text':
            self.texts.setdefault(part.get('messageID'), []).append(part.get('text', ''))
            self.final = '\n'.join(self.texts[part.get('messageID')]).strip()
            self.body(f'assistant\n{part.get("text", "")}')
        elif t == 'tool_use':
            st = part.get('state') or {}
            self.body(f'exec {part.get("tool")} {oneline(json.dumps(st.get("input")), 200)} -> {st.get("status")}: '
                 f'{oneline(st.get("output") or st.get("error") or "", 160)}')
        elif t == 'step_finish':
            self.tokens += int((part.get('tokens') or {}).get('total') or 0)
            if part.get('reason') not in (None, 'stop'):
                self.final = ''  # a step that ended in tool calls is commentary, not the report
        elif t == 'error':
            err = e.get('error') or {}
            self.error = (err.get('data') or {}).get('message') or err.get('name') or 'error event'

    def ev_cursor(self, e):
        self.saw_session(e.get('session_id'))
        t = e.get('type')
        if t == 'assistant':
            text = ''.join(c.get('text', '') for c in (e.get('message') or {}).get('content', [])
                           if c.get('type') == 'text')
            if text.strip():
                self.body(f'assistant\n{text}')
        elif t == 'tool_call' and e.get('subtype') == 'started':
            self.body(f'exec {oneline(json.dumps(e.get("tool_call")), 240)}')
        elif t == 'result':
            usage = e.get('usage') or {}
            self.tokens += int(sum(v for v in usage.values() if isinstance(v, (int, float))))
            if e.get('is_error') or e.get('subtype') not in (None, 'success'):
                self.error = oneline(e.get('result') or e.get('subtype') or 'error result', 400)
            else:
                self.final = (e.get('result') or '').strip()


def emit(line):
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


def execute(harness, model, effort, sandbox, workdir, message, session=None, route='-'):
    check(harness, sandbox)
    if not os.path.isdir(workdir):
        fail(f'workdir not found: {workdir}')
    resumed = bool(session)
    workdir = os.path.realpath(workdir)
    os.environ['PWD'] = workdir
    child, stopped = [], []

    def kill(sig):
        for c in child:
            try:
                os.killpg(c.pid, sig)
            except ProcessLookupError:
                pass

    def stop(signum, _frame):
        # TERM the whole group (harness + its tools); KILL whatever ignores it 5 s later.
        stopped.append(signum)
        kill(signal.SIGTERM)
        t = threading.Timer(5, kill, (signal.SIGKILL,))
        t.daemon = True
        t.start()

    def reap():
        # After a stop, wait for the group to go (a descendant may outlive the harness), then KILL it.
        for c in child:
            for _ in range(50):
                try:
                    os.killpg(c.pid, 0)
                except (ProcessLookupError, PermissionError):
                    break
                time.sleep(0.1)
            else:
                kill(signal.SIGKILL)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, stop)
    if not session and harness == 'pi':
        session = str(uuid.uuid4())  # pi creates a session with this exact id
    if not session and harness == 'cursor':
        try:
            # A Popen in `child`, so a stop during a hung create-chat reaches it too.
            made = subprocess.Popen([binary('cursor'), 'create-chat'], cwd=workdir, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            child.append(made)
            try:
                out, err = made.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                kill(signal.SIGKILL)
                out, err = made.communicate()
            child.remove(made)
            session = out.strip().splitlines()[-1] if made.returncode == 0 and out.strip() else None
            why = err or out
        except (OSError, TypeError) as e:  # TypeError: the CLI vanished after check (binary() is None)
            why = str(e)
        if stopped:
            emit(f'stopped by signal {stopped[0]}')
            return 128 + stopped[0]
        if not session:
            emit(f'ERROR: cursor create-chat failed: {oneline(why, 200)}')
            return 1
    emit(f'{harness} worker via agent-lanes harness_run{" (resumed)" if resumed else ""}\n--------')
    emit(f'workdir: {workdir}\nmodel: {model}\nharness: {harness}\nprovider: {route}\nsandbox: {sandbox}\n'
         f'reasoning effort: {effort}')
    run = Run(harness, None)
    run.saw_session(session)
    # No prompt echo (it is in NAME.prompt): opencode/omp print their session id after the first event,
    # and it must stay inside the 20 header lines that wk -r reads.
    cmd = argv(harness, model, effort, sandbox, session if resumed or harness in ('pi', 'cursor') else None, message)
    # PWD too: opencode follows an inherited $PWD over the real cwd (it ran in the caller's repo).
    env = {**os.environ, 'PWD': workdir}
    if harness == 'opencode' and sandbox == 'read-only':
        # Merge into the user's own inline config (it may define the provider) instead of replacing it.
        try:
            cfg = json.loads(env.get('OPENCODE_CONFIG_CONTENT') or '{}')
        except ValueError:
            cfg = {}
        cfg = cfg if isinstance(cfg, dict) else {}
        if not isinstance(cfg.get('agent'), dict):
            cfg['agent'] = {}
        cfg['agent'].update(json.loads(OPENCODE_READONLY)['agent'])
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps(cfg)
    if stopped:
        emit(f'stopped by signal {stopped[0]}')
        return 128 + stopped[0]
    try:
        proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors='replace', bufsize=1,
                                start_new_session=True)
    except (OSError, TypeError) as e:  # TypeError: the CLI vanished after check (binary() is None)
        run.flush()
        emit(f'ERROR: {harness} did not start: {oneline(e, 300)}')
        return 1
    child.append(proc)
    if stopped:
        kill(signal.SIGTERM)
    for line in proc.stdout:
        line = line.rstrip('\n')
        try:
            e = json.loads(line)
        except ValueError:
            e = None
        if isinstance(e, dict):
            run.event(e)
        elif line.strip():
            run.body(line)
    run.flush()
    try:
        rc = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        kill(signal.SIGKILL)
        rc = proc.wait()
    if stopped:
        reap()
        emit(f'stopped by signal {stopped[0]}')
        return 128 + stopped[0]
    if rc == 0 and run.final and not run.error:
        if os.environ.get('AGENT_LANES_LOG'):
            with open(os.environ['AGENT_LANES_LOG'] + '.last', 'w', encoding='utf-8') as f:
                f.write(run.final + '\n')
        emit(f'tokens used\n{run.tokens:,}\n{run.final}')
        return 0
    emit(f'ERROR: {harness} {oneline(run.error, 400) if run.error else f"exited {rc} without a final message"}')
    return rc or 1


def header(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = [next(f, '') for _ in range(20)]
    h = {}
    for line in lines:
        k, sep, v = line.rstrip('\n').partition(': ')
        if sep and k not in h:
            h[k] = v
    return h


def main(args):
    if args[:1] == ['check'] and len(args) == 3:
        check(args[1], args[2])
        return 0
    if args[:1] == ['run'] and len(args) == 7:
        _, harness, model, effort, sandbox, workdir, prompt = args
        with open(prompt, encoding='utf-8') as f:
            message = f.read()
        return execute(harness, model, effort, sandbox, workdir, message,
                       route=os.environ.get('WK_ROUTE', '-'))
    if args[:1] == ['resume'] and len(args) in (3, 4):
        h = header(args[1])
        need = ('harness', 'model', 'sandbox', 'workdir', 'session id')
        if any(not h.get(k) for k in need):
            fail(f'{args[1]} lacks a harness header ({", ".join(k for k in need if not h.get(k))})')
        effort = args[3] if len(args) == 4 else h.get('reasoning effort', 'medium')
        return execute(h['harness'], h['model'], effort, h['sandbox'], h['workdir'], args[2],
                       session=h['session id'], route=h.get('provider', '-'))
    print(__doc__.split('\n\n')[1], file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
