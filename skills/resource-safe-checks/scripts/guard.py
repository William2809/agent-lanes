#!/usr/bin/env python3
"""Cheap PreToolUse guard for Claude Code and Codex; never runs submitted code."""
import ast
import json
from pathlib import Path
import re
import shlex
import sys

HEAVY = {'vitest', 'jest', 'playwright', 'cypress', 'mocha', 'ava', 'tap', 'nyc',
         'pytest', 'py.test', 'tox', 'nox', 'eslint', 'tsc', 'vue-tsc', 'tsup',
         'webpack', 'rollup', 'vite', 'next', 'nuxt', 'turbo', 'nx', 'lerna',
         'depcruise', 'depcruise-baseline', 'jscpd', 'make', 'gmake', 'cmake',
         'ctest', 'ninja', 'gradle', 'gradlew', 'mvn', 'mvnw', 'bazel'}
PACKAGES = {'npm', 'pnpm', 'yarn', 'bun', 'npx', 'bunx', 'uv', 'pip', 'pip3', 'poetry'}
READ_PACKAGE = {'--version', '-v', '--help', '-h', 'help', 'list', 'ls', 'why', 'info',
                'view', 'outdated', 'config', 'root', 'bin', 'prefix', 'store', 'cache'}
SHELLS = {'sh', 'bash', 'zsh', 'dash', 'fish'}
INTERPRETERS = {'node', 'nodejs', 'python', 'python3', 'perl', 'ruby', 'tsx', 'ts-node'}
WRAPPERS = {'env', 'command', 'exec', 'nohup', 'nice', 'time', 'timeout', 'gtimeout', 'sudo', 'corepack', 'xargs'}
SEPARATORS = {';', '&&', '||', '|', '&', '\n', '(', ')'}
HEAVY_FILE = re.compile(r'(^|[/_.-])(tests?|check\w*|build\w*|lint\w*|typecheck\w*|benchmark\w*)([/_.-]|$)', re.I)
REASON = ('resource-safe-checks: heavy execution requires the shared queue. '
          'Use safe-check --run <quoted command> (or safe-check for the project gate). '
          'A configured runner outage is deferred validation; never run locally, disable '
          'the guard, set CI=1, or claim a pass. Do not queue paid/live provider operations.')


def tokens(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    lexer.commenters = '#'
    # shlex groups adjacent punctuation. Keep newline command boundaries separate.
    words = []
    for word in lexer:
        if '\n' in word and not set(word) - set(';&|()\n'):
            words.extend(part for part in re.split(r'(\n)', word) if part)
        else:
            words.append(word)
    return words


def outer_heredoc(command):
    """Find a heredoc operator outside quoted arguments; never execute shell text."""
    quote = None
    escaped = False
    comment = False
    for at, char in enumerate(command):
        if comment:
            comment = char != '\n'
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == '#' and (at == 0 or command[at-1].isspace()):
            comment = True
        elif command.startswith('<<', at):
            return re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<word>[^\s<>|;&'\"]+)(?P=quote)(?P<rest>[^\n]*)\n").match(command, at)
    return None


def heredoc_programs(header, suffix=''):
    # Inspect command positions, not output paths such as notes.sh or python.txt.
    names = []
    command_start = True
    for word in tokens(header):
        if word in {';', '&&', '||', '\n'}:
            names = []
            command_start = True
        elif word == '|':
            command_start = True
        elif command_start and not re.match(r'^\w+=', word):
            name = Path(word).name
            if name in WRAPPERS:
                continue
            names.append(name)
            command_start = False
    # The heredoc belongs to the command before <<. A trailing statement cannot
    # change its consumer, but a following pipe can feed it to an interpreter.
    command_start = False
    for word in tokens(suffix):
        if word in {';', '&&', '||', '&', '\n'}:
            break
        if word == '|':
            command_start = True
        elif command_start and not re.match(r'^\w+=', word):
            name = Path(word).name
            if name in WRAPPERS:
                continue
            names.append(name)
            command_start = False
    return names


def substitutions(command, cwd, depth):
    for nested in re.findall(r'\$\(([^()]*)\)|`([^`]*)`', command):
        reason = inspect(next((x for x in nested if x), ''), cwd, depth+1)
        if reason:
            return reason
    return None


def inspect(command, cwd, depth=0):
    if depth > 6:
        return 'nested command limit'
    # Only outer-shell heredocs belong here. Quoted shell -c payloads recurse later.
    while True:
        heredoc = outer_heredoc(command)
        if not heredoc:
            break
        end = re.search(r'^' + re.escape(heredoc['word']) + r'[ \t]*$', command[heredoc.end():], re.M)
        if not end:
            return 'unterminated heredoc'
        stop = heredoc.end() + end.end()
        body = command[heredoc.end():heredoc.end()+end.start()]
        first = command[:heredoc.start()]
        header = first + heredoc['rest']
        try:
            programs = heredoc_programs(first, heredoc['rest'])
        except ValueError:
            return 'unparseable heredoc command'
        if not heredoc['quote']:
            reason = substitutions(body, cwd, depth)
            if reason:
                return reason
        if any(re.fullmatch(r'python[\d.]*', name) for name in programs):
            reason = inspect_python(body, cwd, depth+1)
            if reason:
                return reason
        elif SHELLS.intersection(programs):
            reason = inspect(body, cwd, depth+1)
            if reason:
                return reason
        elif {'node', 'ruby', 'perl'}.intersection(programs):
            if re.search(r'\b(vitest|jest|playwright|pytest|unittest|eslint|tsc)\b', body):
                return 'interpreter check'
        command = header + '\n' + command[stop:]
    reason = substitutions(command, cwd, depth)
    if reason:
        return reason
    try:
        words = tokens(command)
    except ValueError:
        return 'unparseable shell command'
    parts, part = [], []
    for word in words + [';']:
        if word in SEPARATORS:
            if part:
                parts.append(part)
            part = []
        else:
            part.append(word)
    for args in parts:
        result, cwd = inspect_part(args, cwd, depth)
        if result:
            return result
    return None


def inspect_part(args, cwd, depth):
    while args and (re.match(r'^\w+=', args[0]) or Path(args[0]).name in WRAPPERS):
        if Path(args[0]).name in WRAPPERS:
            if Path(args[0]).name == 'env' and '-S' in args:
                return inspect(args[args.index('-S')+1], cwd, depth+1), cwd
            args = args[1:]
            while args and (args[0].startswith('-') or args[0].isdigit()):
                # env -S payload is examined recursively below, not executed.
                args = args[1:]
        else:
            args = args[1:]
    if not args:
        return None, cwd
    name = Path(args[0]).name
    if re.fullmatch(r'python3(?:\.\d+)?', name):
        name = 'python3'
    tail = args[1:]
    if name == 'cd' and tail:
        return None, (cwd / tail[-1]).resolve()
    if name in {'safe-check', 'remote-ci', 'remote-ci'}:
        return None, cwd
    if tail and all(x in {'--version', '-v', '--help', '-h', 'help'} for x in tail):
        return None, cwd
    if name == 'rtk':
        if tail and tail[0] in {'test', 'build', 'lint'}:
            return 'rtk heavy command', cwd
        return inspect_part(tail, cwd, depth+1)
    if name in {'find', 'watch'}:
        for at, word in enumerate(tail):
            if word in {'-exec', '-execdir'}:
                return inspect_part(tail[at+1:], cwd, depth+1)
    if name in HEAVY:
        return name, cwd
    if name in PACKAGES:
        if tail and tail[0] in READ_PACKAGE:
            # Mutating caches are expensive too; only read-only forms pass.
            if tail[0] in {'store', 'cache', 'config'} and any(x in tail for x in ('prune', 'clean', 'set', 'delete')):
                return name, cwd
            return None, cwd
        return name, cwd
    if name in {'cargo', 'go', 'dotnet', 'swift', 'xcodebuild', 'docker', 'podman'}:
        if any(x in tail for x in ('test', 'build', 'check', 'clippy', 'bench', 'run', 'up', '--build', 'publish', 'restore')) or name == 'xcodebuild':
            return name, cwd
    if name in {'python', 'python3'} and '-c' in tail:
        return inspect_python(tail[tail.index('-c')+1], cwd, depth+1), cwd
    if name in SHELLS or name in INTERPRETERS or name == 'ssh':
        if '--test' in tail or any(x in tail for x in ('pytest', 'unittest', 'compileall')):
            return name, cwd
        if '--check' in tail and name in {'node', 'nodejs'}:
            return None, cwd
        for arg in tail:
            if Path(arg).stem in HEAVY or ('-m' in tail and arg in PACKAGES):
                return name, cwd
            if any(flag in tail for flag in ('-e', '--eval', '-p', '--print')) and inspect_inline(arg):
                return name, cwd
            option, equals, value = arg.partition('=')
            if equals and option in {'-e', '--eval', '-p', '--print'} and inspect_inline(value):
                return name, cwd
            if name in SHELLS or name == 'ssh':
                nested = inspect(arg, cwd, depth+1) if (' ' in arg or '\n' in arg) else None
                if nested:
                    return nested, cwd
                if ' ' in arg or '\n' in arg:
                    continue
            if HEAVY_FILE.search(Path(arg).name) and not arg.startswith('-'):
                return 'check script', cwd
        # Follow small shell wrappers under the actual working directory.
        if name in SHELLS:
            for arg in tail:
                if '\n' in arg:  # The inline shell payload was inspected above.
                    continue
                path = cwd / arg
                if path.is_file() and path.stat().st_size < 65536:
                    nested = inspect(path.read_text(), path.parent, depth+1)
                    if nested:
                        return nested, cwd
    if '/' in args[0] or (cwd / args[0]).is_file():
        if HEAVY_FILE.search(args[0]):
            return 'check executable', cwd
    return None, cwd


def inspect_python(source, cwd, depth):
    # Strings written to a source/config file are data, not subprocess calls.
    # Parse only; never import, evaluate or execute the submitted program.
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 'unparseable Python command'
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func) if hasattr(ast, 'unparse') else ''
        if name in {'pytest.main', 'unittest.main'}:
            return 'Python test runner'
        if name in {'eval', 'exec'}:
            return 'dynamic Python execution'
        if name.startswith(('subprocess.', 'os.exec', 'os.spawn')) or name == 'os.system':
            if not node.args:
                return 'opaque process invocation'
            try:
                command = ast.literal_eval(node.args[0])
            except (ValueError, TypeError):
                return 'opaque process invocation'
            if isinstance(command, (list, tuple)) and all(isinstance(x, str) for x in command):
                command = shlex.join(command)
            if not isinstance(command, str):
                return 'opaque process invocation'
            reason = inspect(command, cwd, depth+1)
            if reason:
                return reason
    return None


def inspect_inline(value):
    # Handles interpreter subprocess wrappers without evaluating their content.
    return bool(re.search(r'\b(?:vitest|jest|playwright|pytest|unittest|eslint|tsc|pnpm|npm|yarn|bun)\b', value)
                and re.search(r'(?:spawn|exec|system|subprocess|require|import|run|test)', value))


def main():
    try:
        payload = json.load(sys.stdin)
        data = payload.get('tool_input', {})
        command = data.get('command', data.get('cmd', ''))
        if not isinstance(command, str):
            raise ValueError('missing command')
        reason = inspect(command, Path(data.get('workdir') or data.get('cwd') or payload.get('cwd') or '.'))
        if reason:
            print(REASON + ' Detected: ' + reason, file=sys.stderr)
            return 2
        return 0
    except Exception:
        # Explicit denial on parse/read errors, rather than the hook host's
        # fail-open behavior for an unhandled exception or malformed JSON.
        print('resource-safe-checks: guard could not inspect this command; execution denied', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
