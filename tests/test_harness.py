"""Non-Codex harnesses through wk: fake cursor-agent/opencode/pi/omp CLIs, real harness_run.py, isolated HOME."""
import json
import os
import signal
import subprocess
import time
import unittest
from workflow_fixture import ROOT, WorkflowFixture

# One fake for every harness CLI: records argv/cwd/PWD, then prints that harness's JSON events.
FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
name = pathlib.Path(sys.argv[0]).name; args = sys.argv[1:]
root = pathlib.Path(os.environ['FIXTURE_ROOT']); mode = os.environ.get('FAKE_HARNESS_MODE', 'ok')
if name == 'cursor-agent' and args == ['create-chat']:
    print('chat-123'); sys.exit(0)
with (root / 'harness-calls.jsonl').open('a') as f:
    f.write(json.dumps({'cli': name, 'args': args, 'cwd': os.getcwd(), 'pwd': os.environ.get('PWD'),
                        'occfg': os.environ.get('OPENCODE_CONFIG_CONTENT')}) + '\n')
out = lambda e: print(json.dumps(e), flush=True)
if mode == 'noise':
    for i in range(12): print(f'warning: startup noise {i}', flush=True)
if mode == 'orphan':  # a TERM-proof descendant with no output pipe; the harness itself obeys TERM
    import signal
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN); os.close(1); os.close(2); time.sleep(120); sys.exit(0)
    (root / 'grandchild.pid').write_text(str(child)); time.sleep(120)
if mode in ('hold', 'stubborn'):
    import signal
    if mode == 'stubborn': signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = os.fork()
    if child == 0: time.sleep(120); sys.exit(0)
    (root / 'grandchild.pid').write_text(str(child)); time.sleep(120)
final = 'Changed: fixture.txt\nVerified live: none'
if name in ('pi', 'omp'):
    out({'type': 'session', 'id': 'pi-session-1'})
    out({'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'toolCall', 'name': 'bash', 'arguments': {'command': 'ls'}}], 'usage': {'totalTokens': 100}}})
    out({'type': 'tool_execution_end', 'toolName': 'bash', 'result': {'content': [{'type': 'text', 'text': 'a.txt'}]}, 'isError': False})
    if mode == 'aborted':
        out({'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Looking at the files first.'}]}})
        out({'type': 'message_end', 'message': {'role': 'assistant', 'content': [], 'stopReason': 'aborted'}}); sys.exit(0)
    if mode == 'model-error':
        out({'type': 'message_end', 'message': {'role': 'assistant', 'content': [], 'stopReason': 'error', 'errorMessage': 'model not supported'}}); sys.exit(0)
    out({'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': final}], 'usage': {'totalTokens': 1134}}})
elif name == 'opencode':
    sid = args[args.index('--session') + 1] if '--session' in args else 'ses_fixture'
    out({'type': 'step_start', 'sessionID': sid, 'part': {}})
    if mode == 'error-event':
        out({'type': 'error', 'sessionID': sid, 'error': {'name': 'APIError', 'data': {'message': 'Bad Request: model not supported'}}}); sys.exit(1)
    out({'type': 'text', 'sessionID': sid, 'part': {'messageID': 'm1', 'text': final}})
    out({'type': 'step_finish', 'sessionID': sid, 'part': {'tokens': {'total': 2000}}})
elif name == 'cursor-agent':
    out({'type': 'system', 'subtype': 'init', 'session_id': 'chat-123'})
    out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'working'}]}, 'session_id': 'chat-123'})
    out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': final, 'session_id': 'chat-123'})
'''


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(('a',))
        self.addCleanup(self.f.close)
        for cli in ('pi', 'omp', 'opencode', 'cursor-agent'):
            self.f.shim(cli, FAKE)
        models = self.f.root / 'models.conf'
        models.write_text('\n'.join([
            'pibuild   openai-codex/gpt-5.5  low     full-access      -   pi',
            'pireview  openai-codex/gpt-5.5  medium  read-only        -   pi',
            'piwrite   openai-codex/gpt-5.5  low     workspace-write  -   pi',
            'ompbuild  kimi-for-coding       high    full-access      -   omp',
            'ompreview kimi-for-coding       high    read-only        -   omp',
            'leadro    shared-model          high    read-only        codex',
            'leadpi    shared-model          high    full-access      -   pi',
            'ocbuild   openai/gpt-5.5        low     full-access      -   opencode',
            'ocreview  openai/gpt-5.5        high    read-only        -   opencode',
            'cubuild   gpt-5                 medium  workspace-write  -   cursor',
            'routea    gpt-6.1-sol           medium  full-access      proxy-a',
            'routeb    gpt-6.1-sol           medium  full-access      proxy-b', '']))
        self.f.env['AGENT_LANES_MODELS'] = str(models)

    def log(self, name):
        return self.f.home / '.claude/state/logs/demo' / (name + '.log')

    def finish(self, name):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            text = self.log(name).read_text() if self.log(name).exists() else ''
            if '\ntokens used\n' in text or '\nERROR: ' in text:
                return text
            time.sleep(0.05)
        raise AssertionError(f'{name} did not finish: {text}')

    def calls(self):
        p = self.f.root / 'harness-calls.jsonl'
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def report(self, name):
        return self.f.run('sh', str(ROOT / 'skills/agent-workers/scripts/report.sh'), str(self.log(name))).stdout

    def test_pi_launch_writes_codex_shaped_log(self):
        out = self.f.wk('w1', '-d', 'a', '-m', 'pibuild', '-t', 'fixture:new').stdout
        self.assertIn('started w1: pi openai-codex/gpt-5.5 low', out)
        text = self.finish('w1')
        head = text.splitlines()[:20]
        lane = str(self.f.lanes['a'].resolve())
        for line in (f'workdir: {lane}', 'harness: pi', 'sandbox: full-access', 'reasoning effort: low'):
            self.assertIn(line, head)
        self.assertTrue(any(l.startswith('session id: ') for l in head))
        self.assertIn('tokens used\n1,234\n', text)
        self.assertIn('Changed: fixture.txt', self.report('w1'))
        call = self.calls()[0]
        self.assertEqual((call['cwd'], call['pwd']), (lane, lane))
        self.assertIn('--session-id', call['args'])
        self.assertNotIn('--tools', call['args'])
        self.assertEqual(call['args'][call['args'].index('--thinking') + 1], 'low')

    def test_read_only_presets_use_each_harness_read_only_mode(self):
        self.f.wk('r1', '-d', 'a', '-m', 'pireview', '-t', 'fixture:review')
        self.f.wk('r2', '-d', 'a', '-m', 'ocreview', '-t', 'fixture:review')
        self.finish('r1'); self.finish('r2')
        args = {c['cli']: c['args'] for c in self.calls()}
        self.assertEqual(args['pi'][args['pi'].index('--tools') + 1], 'read,grep,find,ls')
        self.assertEqual(args['opencode'][args['opencode'].index('--agent') + 1], 'agentops-readonly')
        self.assertNotIn('--auto', args['opencode'])
        cfg = json.loads(next(c for c in self.calls() if c['cli'] == 'opencode')['occfg'])
        perm = cfg['agent']['agentops-readonly']['permission']
        self.assertEqual((perm['edit'], perm['task'], perm['bash']['*'], perm['bash']['*>*']), ('deny',) * 4)
        self.assertFalse(cfg['agent']['agentops-readonly']['tools']['*'])
        self.assertNotIn('git diff*', perm['bash'])  # would allow git difftool --extcmd

    def test_read_only_opencode_keeps_the_users_inline_config(self):
        user = json.dumps({'provider': {'mine': {'name': 'x'}}, 'agent': {'other': {'mode': 'primary'}}})
        self.f.wk('r3', '-d', 'a', '-m', 'ocreview', '-t', 'fixture:review', env={'OPENCODE_CONFIG_CONTENT': user})
        self.finish('r3')
        cfg = json.loads(self.calls()[0]['occfg'])
        self.assertEqual(set(cfg['agent']), {'other', 'agentops-readonly'})
        self.assertIn('mine', cfg['provider'])

    def test_unsupported_sandbox_is_refused_before_launch(self):
        r = self.f.wk('w2', '-d', 'a', '-m', 'piwrite', '-t', 'fixture:new', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('pi has no workspace-write mode', r.stderr)
        r = self.f.wk('w3', '-d', 'a', '-m', 'ompreview', '-t', 'fixture:review', check=False)
        self.assertIn('omp has no read-only mode', r.stderr)
        self.assertEqual(self.calls(), [])

    def test_model_error_and_error_event_end_with_error_line(self):
        self.f.wk('e1', '-d', 'a', '-m', 'pibuild', '-t', 'fixture:new', env={'FAKE_HARNESS_MODE': 'model-error'})
        self.f.wk('e2', '-d', 'a', '-m', 'ocbuild', '-t', 'fixture:new', env={'FAKE_HARNESS_MODE': 'error-event'})
        self.assertTrue(self.finish('e1').rstrip().endswith('ERROR: pi model not supported'))
        self.assertTrue(self.finish('e2').rstrip().endswith('ERROR: opencode Bad Request: model not supported'))
        self.assertNotIn('tokens used', self.log('e1').read_text())

    def test_resume_reuses_session_model_and_dir(self):
        self.f.wk('o1', '-d', 'a', '-m', 'ocbuild', '-t', 'fixture:new')
        self.finish('o1')
        os.utime(self.log('o1'), (1, 1))  # past the recent-log guard
        out = self.f.run(str(ROOT / 'bin/wk'), 'o1', '-r', 'continue please', cwd=self.f.lanes['a']).stdout
        self.assertIn('resumed o1 (opencode openai/gpt-5.5 low, ses_fixture)', out)
        text = self.finish('o1')
        self.assertIn('session id: ses_fixture', text.splitlines()[:20])
        last = self.calls()[-1]['args']
        self.assertEqual(last[last.index('--session') + 1], 'ses_fixture')
        self.assertEqual(last[-1], 'continue please')

    def test_cursor_and_omp_adapters(self):
        self.f.wk('c1', '-d', 'a', '-m', 'cubuild', '-t', 'fixture:new')
        self.f.wk('p1', '-d', 'a', '-m', 'ompbuild', '-t', 'fixture:new')
        self.assertIn('session id: chat-123', self.finish('c1'))
        self.assertIn('Changed: fixture.txt', self.report('c1'))
        self.assertIn('tokens used', self.finish('p1'))
        args = {c['cli']: c['args'] for c in self.calls()}
        self.assertEqual(args['cursor-agent'][args['cursor-agent'].index('--resume') + 1], 'chat-123')
        self.assertIn('enabled', args['cursor-agent'])
        self.assertIn('--approval-mode=yolo', args['omp'])

    def test_stop_kills_the_harness_tree_and_batches_sees_it(self):
        self.f.wk('h1', '-d', 'a', '-m', 'pibuild', '-t', 'fixture:new', env={'FAKE_HARNESS_MODE': 'hold'})
        gc = self.f.root / 'grandchild.pid'
        until = time.monotonic() + 10
        while not gc.exists() and time.monotonic() < until:
            time.sleep(0.05)
        status = self.f.run(str(ROOT / 'skills/agent-workers/scripts/batches.sh'), check=False).stdout
        self.assertRegex(status, r'h1 +running')
        wrapper = int((self.log('h1').parent / 'h1.log.pid').read_text())
        os.kill(wrapper, signal.SIGTERM)
        until = time.monotonic() + 10
        while time.monotonic() < until and subprocess.run(['kill', '-0', gc.read_text()], capture_output=True).returncode == 0:
            time.sleep(0.05)
        self.assertNotEqual(subprocess.run(['kill', '-0', gc.read_text()], capture_output=True).returncode, 0)
        self.assertIn('stopped by signal 15', self.log('h1').read_text())

    def test_codex_plan_guard_skips_other_harnesses_and_route_is_kept(self):
        self.f.shim('codex-limit', '#!/bin/sh\necho 100\n')
        r = self.f.wk('g1', '-d', 'a', '-m', 'routeb', '-t', 'fixture:new', check=False, env={'WK_FORCE': '0'})
        self.assertIn('Codex plan at 100% used', r.stderr)
        self.f.wk('g2', '-d', 'a', '-m', 'pibuild', '-t', 'fixture:new', env={'WK_FORCE': '0'})
        self.finish('g2')
        self.f.shim('codex-limit', '#!/bin/sh\necho 0\n')
        self.f.shim('codex', '#!/bin/sh\necho "$@" >>"$FIXTURE_ROOT/codex-args"\necho "session id: s"\necho "tokens used"\necho 1\n')
        self.f.wk('g3', '-d', 'a', '-m', 'routeb', '-t', 'fixture:new')
        self.finish('g3')
        self.assertIn('--profile proxy-b', (self.f.root / 'codex-args').read_text())

    def test_aborted_run_is_an_error_not_a_report(self):
        self.f.wk('a1', '-d', 'a', '-m', 'pibuild', '-t', 'fixture:new', env={'FAKE_HARNESS_MODE': 'aborted'})
        text = self.finish('a1')
        self.assertTrue(text.rstrip().endswith('ERROR: pi run aborted'))
        self.assertNotIn('tokens used', text)

    def test_startup_noise_keeps_session_id_in_header(self):
        self.f.wk('n1', '-d', 'a', '-m', 'ocbuild', '-t', 'fixture:new', env={'FAKE_HARNESS_MODE': 'noise'})
        lines = self.finish('n1').splitlines()
        self.assertIn('session id: ses_fixture', lines[:20])
        self.assertIn('warning: startup noise 11', lines)

    def test_stop_kills_a_tree_that_ignores_term(self):
        for mode in ('stubborn', 'orphan'):
            with self.subTest(mode=mode):
                self.stop_case(mode)

    def stop_case(self, mode):
        name = 's' + mode
        gc = self.f.root / 'grandchild.pid'
        if gc.exists(): gc.rename(self.f.root / f'grandchild.{mode}.old')
        self.f.wk(name, '-d', 'a', '-m', 'pibuild', '-t', 'fixture:new', env={'FAKE_HARNESS_MODE': mode})
        gc = self.f.root / 'grandchild.pid'
        until = time.monotonic() + 10
        while not gc.exists() and time.monotonic() < until:
            time.sleep(0.05)
        time.sleep(0.3)
        os.kill(int((self.log(name).parent / (name + '.log.pid')).read_text()), signal.SIGTERM)
        until = time.monotonic() + 12
        while time.monotonic() < until and subprocess.run(['kill', '-0', gc.read_text()], capture_output=True).returncode == 0:
            time.sleep(0.1)
        self.assertNotEqual(subprocess.run(['kill', '-0', gc.read_text()], capture_output=True).returncode, 0)

    def test_resume_works_without_codex_installed(self):
        self.f.wk('o2', '-d', 'a', '-m', 'ocbuild', '-t', 'fixture:new')
        self.finish('o2')
        os.utime(self.log('o2'), (1, 1))
        (self.f.shims / 'codex').rename(self.f.shims / 'codex.off')
        env = {'CODEX_BIN': ''}
        out = self.f.run(str(ROOT / 'bin/wk'), 'o2', '-r', 'go on', cwd=self.f.lanes['a'], env=env).stdout
        self.assertIn('resumed o2 (opencode', out)

    def test_spawn_failure_ends_with_error_line(self):
        bad = self.f.root / 'badpi'
        bad.write_text('#!/nonexistent/interpreter\n')
        bad.chmod(0o755)
        self.f.wk('b1', '-d', 'a', '-m', 'pibuild', '-t', 'fixture:new', env={'PI_BIN': str(bad)})
        self.assertIn('ERROR: pi did not start', self.finish('b1'))

    def test_lead_guard_ignores_other_harness_presets(self):
        cfg = self.f.root / 'lead-config'
        cfg.write_text('AGENT_LANES_LEAD_PROFILE=fixture-proxy\n')
        prompt = self.f.root / 'lead.prompt'
        prompt.write_text('task\n')
        r = self.f.run('sh', str(ROOT / 'skills/agent-workers/scripts/run-worker.sh'), '--lead', 'shared-model', 'high',
                       'read-only', str(self.f.lanes['a']), str(prompt), str(self.f.root / 'lead.log'),
                       check=False, env={'AGENT_LANES_CONFIG': str(cfg)})
        self.assertIn('--lead is not for read-only models', r.stderr)


if __name__ == '__main__':
    unittest.main()
