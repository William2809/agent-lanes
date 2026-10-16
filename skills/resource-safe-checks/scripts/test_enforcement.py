"""Small deterministic guard probes. No test framework or browser is launched."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
ROOT=Path(__file__).resolve().parents[3]
SCRIPT=Path(__file__).with_name('guard.py')
spec=importlib.util.spec_from_file_location('guard',SCRIPT); g=importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
class GuardTests(unittest.TestCase):
 def test_interpreter_heredoc_before_trailing_command(self):
  """Review 4: executed heredoc stays guarded before a trailing no-op.
  Boundary: semicolon after delimiter. Gap: parser only, no subprocess executes.
  """
  command="python3 <<'PY'; :\nimport subprocess\nsubprocess.run(['pytest'])\nPY\n"
  self.assertIsNotNone(g.inspect(command,Path.cwd()))
 def test_attached_interpreter_eval(self):
  """Review 4: attached eval/print options cannot hide heavy execution.
  Boundary: equals-value option. Gap: parser only, no node execution.
  """
  for flag in ('--eval', '--print'):
   with self.subTest(flag=flag):
    self.assertIsNotNone(g.inspect(f'''node {flag}="require('child_process').execSync('npm test')"''',Path.cwd()))
 def test_benign_words_and_process_listing(self):
  """Review 4: words in data and similar script names are benign.
  Boundary: quoted force-push text, testimonials, ps/awk pipeline.
  Gap: parser only; no command execution or force-push policy test.
  """
  for command in ('echo "never git push --force"', 'python3 testimonials.py',
                  "ps -axo pid,command | awk '/worker/ {print $1}'"):
   with self.subTest(command=command): self.assertIsNone(g.inspect(command,Path.cwd()))
 def test_interpreter_script_checks_own_filename(self):
  """Brief 4a. Boundary: heavy directory names, benign/actual check basenames.
  Gap: inspect text only; never run the named install script.
  """
  for directory in ('resource-safe-checks', 'tests', 'build', 'lint'):
   for interpreter in ('python3', 'python3.12', 'node'):
    command=f'{interpreter} /tmp/{directory}/scripts/install.py'
    with self.subTest(command=command): self.assertIsNone(g.inspect(command,Path.cwd()))
  for script in ('test_enforcement.py', 'check.py', 'build.mjs', 'lint.py'):
   with self.subTest(script=script): self.assertIsNotNone(g.inspect(f'python3 /tmp/plain/{script}',Path.cwd()))
 def test_file_heredocs_are_data(self):
  """Brief 4b. Boundary: quoted cat/tee heredocs, punctuation delimiters and shell wrappers.
  Gap: parser probes only; do not write or execute the shell payload.
  """
  import shlex
  body='sh tests/run-all; remote-ci check\nsh skills/remote-ci/remote/run.sh\npython3 -m unittest\n'
  for writer in ('cat > FILE', 'cat > notes.sh', 'tee FILE > /dev/null'):
   for delimiter in ('EOF', 'DOC-END'):
    command=writer+" <<'"+delimiter+"'\n"+body+delimiter+'\n'
    for form in (command, 'bash -lc '+shlex.quote(command), command+command):
     with self.subTest(form=form): self.assertIsNone(g.inspect(form,Path.cwd()))
 def test_heredoc_does_not_hide_execution(self):
  """Brief 4b: distinguish data from execution.
  Boundary: trailing command, shell consumer and unquoted substitution.
  Gap: parser probes only; no shell or tests execute.
  """
  for command in ("cat > FILE <<'EOF'\nnotes\nEOF\npytest",
                  "sh <<'EOF'\npytest\nEOF\n",
                  "cat <<'EOF' | sh\npytest\nEOF\n",
                  'cat > FILE <<EOF\n$(pytest)\nEOF\n'):
   with self.subTest(command=command): self.assertIsNotNone(g.inspect(command,Path.cwd()))
 def test_raw_checks_and_wrappers(self):
  commands=['CI=1 pnpm test','node node_modules/jest/bin/jest.js','node node_modules/typescript/bin/tsc','python3.12 -m pip install something','env CI=1 /usr/local/bin/pnpm check:full','pnpm run arbitrary-alias',
   'npm install','pnpm --dir sub lint','corepack pnpm test','npx jest --runInBand',
   'node node_modules/vitest/vitest.mjs run','./node_modules/.bin/vitest run','pytest -n auto',
   'python3 -m unittest discover','node --test tests/a.js','bash -lc "pnpm test"',
   'env -S "pnpm test"','rtk test pnpm test','rtk pnpm check','echo $(pnpm test)',
   'find . -exec pnpm test \\;','cargo test','go test ./...','docker compose up --build',
   'safe-check --status; pnpm test','safe-check --run "pnpm test" || pnpm test',
   'AGENT_CI_ADMITTED=1 CI=1 jest','node -e "require(\'child_process\').execSync(\'pnpm test\')"',
   'python3 -c "import subprocess; subprocess.run([\'pytest\'])"','bash tests/run-all']
  for command in commands:
   with self.subTest(command=command): self.assertIsNotNone(g.inspect(command,Path.cwd()))
 def test_read_only_and_routed(self):
  for command in ['git status --short','rg vitest package.json','cat package.json','echo vitest',
    'node --check file.mjs','pnpm --version','pnpm list --depth 0','git diff --check',
    'safe-check --run "pnpm test"','remote-ci check --summary --run "pnpm lint"',
    'cd /tmp && safe-check --run "pytest"']:
   with self.subTest(command=command): self.assertIsNone(g.inspect(command,Path.cwd()))
 def test_python_file_edit_is_data_but_process_launch_is_guarded(self):
  source="from pathlib import Path; Path('note').write_text('vitest subprocess run')"
  self.assertIsNone(g.inspect_python(source,Path.cwd(),0))
  self.assertIsNone(g.inspect("python3 - <<'PY'\n"+source+"\nPY",Path.cwd()))
  launch="import subprocess; subprocess.run(['pnpm', 'test'])"
  self.assertIsNotNone(g.inspect_python(launch,Path.cwd(),0))
  self.assertIsNotNone(g.inspect("python3 - <<'PY'\n"+launch+"\nPY",Path.cwd()))
 def test_protocol_deny_and_bad_input(self):
  for payload in ['{',json.dumps({'tool_input':{'command':'CI=1 pnpm test'},'cwd':str(ROOT)}),
                  json.dumps({'tool_input':{'cmd':'npm test','workdir':str(ROOT)}})]:
   p=subprocess.run([sys.executable,str(SCRIPT)],input=payload,text=True,capture_output=True)
   self.assertEqual(p.returncode,2); self.assertIn('resource-safe-checks',p.stderr)
 def test_protocol_read_only(self):
  p=subprocess.run([sys.executable,str(SCRIPT)],input=json.dumps({'tool_input':{'command':'git status'}}),text=True,capture_output=True)
  self.assertEqual(p.returncode,0); self.assertEqual(p.stdout,'')

class AdmissionTests(unittest.TestCase):
 def test_local_switch_and_policy_cannot_fall_back(self):
  import tempfile
  for remote_conf in (True,False):
   with tempfile.TemporaryDirectory() as tmp:
    home=Path(tmp); repo=home/'repo';repo.mkdir()
    subprocess.run(['git','init','-q',str(repo)],check=True)
    if remote_conf: (repo/'.remote-ci.conf').write_text('REMOTE_CI_NAME=fixture\n')
    else:
     policy=home/'.config/agent-ci/policy.json';policy.parent.mkdir(parents=True);policy.write_text('{"execution":"remote-required"}')
    command=['/bin/bash',str(ROOT/'bin/safe-check'),'--local','--run','touch escaped']
    result=subprocess.run(command,cwd=repo,env=dict(os.environ,HOME=tmp),capture_output=True,text=True,timeout=30)
    self.assertEqual(result.returncode,3,result.stdout+result.stderr);self.assertFalse((repo/'escaped').exists())
 def test_only_live_matching_process_group_has_a_lease(self):
  import tempfile,time,uuid
  helper=SCRIPT.with_name('resource-lease.cjs')
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp)/'.local/state/agent-ci';state.mkdir(parents=True)
   token=uuid.uuid4().hex
   owner_start=subprocess.check_output(['ps','-o','lstart=','-p',str(os.getpid())],text=True).strip()
   node='const l=require(process.argv[1]);process.stdout.write(JSON.stringify(l.allocation() !== null));'
   env=dict(os.environ,HOME=tmp,AGENT_CI_TOKEN=token,CI='1',AGENT_CI_ADMITTED='1')
   def row(group,start=owner_start+'    '):
    path=state/'queue.json';new=state/'queue.json.new';new.write_text(json.dumps({'jobs':{token:{'owner':os.getpid(),'start':start,'workers':1,'group':group}}}));new.replace(path)
   row(999999)
   result=subprocess.run(['node','-e',node,str(helper)],env=env,text=True,capture_output=True,timeout=30,start_new_session=True)
   self.assertEqual(result.stdout,'false')
   child=subprocess.Popen(['node','-e',node,str(helper)],env=env,text=True,stdout=subprocess.PIPE,start_new_session=True)
   row(child.pid)
   self.assertEqual(child.communicate(timeout=30)[0],'true');self.assertEqual(child.returncode,0)
   row(os.getpgid(0),'stale owner')
   result=subprocess.run(['node','-e',node,str(helper)],env=env,text=True,capture_output=True,timeout=30)
   self.assertEqual(result.stdout,'false')
   (state/'queue.json').write_text('{')
   result=subprocess.run(['node','-e',node,str(helper)],env=env,text=True,capture_output=True,timeout=30)
   self.assertEqual(result.stdout,'false')
 def test_host_policy_rejects_raw_queue_entry(self):
  import tempfile
  kit=ROOT/'skills/remote-ci/remote/resources.py'
  if not kit.exists():kit=ROOT/'skills/remote-ci/remote/resources.py'
  with tempfile.TemporaryDirectory() as tmp:
   policy=Path(tmp)/'.config/agent-ci/policy.json';policy.parent.mkdir(parents=True);policy.write_text('{"execution":"remote-required"}')
   result=subprocess.run([sys.executable,str(kit),'run','--wait','1','--command','touch escaped'],cwd=tmp,env=dict(os.environ,HOME=tmp,AGENT_CI_ADMITTED='1'),text=True,capture_output=True,timeout=30)
   self.assertNotEqual(result.returncode,0);self.assertFalse((Path(tmp)/'escaped').exists())

 def test_install_preserves_settings_and_is_idempotent(self):
  import tempfile
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'.claude/settings.json';path.parent.mkdir()
   original={'theme':'dark','hooks':{'PreToolUse':[{'matcher':'Bash','hooks':[{'type':'command','command':'rtk hook claude'}]}]}}
   path.write_text(json.dumps(original))
   for _ in range(2):
    p=subprocess.run([sys.executable,str(SCRIPT.with_name('install.py')),'--execution','remote-required'],env=dict(os.environ,HOME=tmp),capture_output=True,text=True,timeout=30)
    self.assertEqual(p.returncode,0,p.stderr)
   data=json.loads(path.read_text());self.assertEqual(data['theme'],'dark')
   hooks=[h['command'] for g in data['hooks']['PreToolUse'] for h in g['hooks']]
   self.assertEqual(len(hooks),2);self.assertIn('rtk hook claude',hooks)
   self.assertEqual(json.loads((Path(tmp)/'.config/agent-ci/policy.json').read_text())['execution'],'remote-required')

if __name__=='__main__': unittest.main()


