#!/usr/bin/env python3
# ABOUTME: End-to-end tests for wt-dev's framework presets, installs and database copies, run against
# ABOUTME: throwaway git repos under a temporary HOME with stub dev servers (no network, no real frameworks).
import json, os, sqlite3, subprocess, tempfile, textwrap, time, unittest

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin')
SERVE = textwrap.dedent('''\
    import http.server, os, sys
    port = int(sys.argv[1])
    open(os.environ['STUB_OUT'], 'w').write(repr(sys.argv) + '\\n' + os.environ.get('DATABASE_URL', '-') + '\\n' + os.environ.get('SECRET', '-'))
    http.server.HTTPServer(('127.0.0.1', port), http.server.SimpleHTTPRequestHandler).serve_forever()
''')


class WtDev(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wtdev-test-')
        self.home = os.path.join(self.tmp.name, 'home')
        self.repo = os.path.join(self.tmp.name, 'repo')
        os.makedirs(self.home); os.makedirs(self.repo)
        self.env = dict(os.environ, HOME=self.home, PATH=BIN + ':' + os.environ['PATH'],
                        STUB_OUT=os.path.join(self.tmp.name, 'stub.out'), WT_DEV_READY_TIMEOUT='20')
        for k in ('WT_DEV_CMD', 'WT_DEV_INSTALL', 'WT_DEV_APP', 'WT_DEV_SHARED_DB', 'WT_DEV_DB_CLONE'):
            self.env.pop(k, None)
        self.git('init', '-q', '-b', 'main')
        self.write('serve.py', SERVE)
        self.write('.gitignore', '.env\n*.db\n*.sqlite3\nnode_modules/\n')
        self.names = []

    def tearDown(self):
        for n in self.names:
            self.wt('stop', n, check=False)
        self.tmp.cleanup()

    def git(self, *a):
        subprocess.run(['git', '-C', self.repo, '-c', 'user.name=t', '-c', 'user.email=user@example.invalid', *a],
                       check=True, capture_output=True)

    def write(self, rel, text, mode=None):
        p = os.path.join(self.repo, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, 'w').write(text)
        if mode: os.chmod(p, mode)

    def commit(self):
        self.git('add', '-A'); self.git('commit', '-qm', 'x')

    def wt(self, *a, check=True):
        r = subprocess.run(['wt-dev', *a], cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=120)
        if check and r.returncode:
            self.fail(f'wt-dev {a} rc={r.returncode}\n{r.stdout}\n{r.stderr}')
        return r

    def new(self, name, *a):
        self.names.append(name)
        return self.wt('new', name, *a)

    def path(self, name):
        return os.path.join(self.home, 'worktrees', 'repo', name)

    def port(self, name):
        return open(os.path.join(self.home, '.cache', 'wt-dev', 'repo', name + '.port')).read().strip()

    def listening(self, port):
        return subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN'], capture_output=True).returncode == 0

    def test_vite_preset_install_override_and_stop_kills_tree(self):
        self.write('package.json', json.dumps({'devDependencies': {'vite': '6'}}))
        self.write('tools/vite', '#!/bin/sh\n# stub: vite --port N --strictPort\nexec python3 "$(dirname "$0")/../../serve.py" "$2" "$@"\n', 0o755)
        self.write('.wt-dev.conf', 'WT_DEV_INSTALL="mkdir -p node_modules/.bin && cp tools/vite node_modules/.bin/vite && echo x >> installs"\n')
        self.commit()
        out = self.new('v1').stdout
        self.assertIn('ready: http://localhost:', out)
        port = self.port('v1')
        self.assertTrue(self.listening(port))
        self.assertIn("'--strictPort'", open(self.env['STUB_OUT']).read())
        self.wt('install', 'v1', '--if-changed')
        self.assertEqual(open(os.path.join(self.path('v1'), 'installs')).read(), 'x\n')  # unchanged: skipped
        self.wt('stop', 'v1')
        time.sleep(0.5)
        self.assertFalse(self.listening(port), 'stop left the server (a grandchild of npx/sh) running')

    def test_django_preset_relative_sqlite_copy_and_env_loading(self):
        self.write('manage.py', 'import os, sys\nos.execvp("python3", ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve.py"), sys.argv[2].split(":")[1]])\n')
        self.commit()
        self.write('.env', 'DATABASE_URL=sqlite:///db.sqlite3\nSECRET="from env"\n')
        con = sqlite3.connect(os.path.join(self.repo, 'db.sqlite3')); con.execute('create table t (x)'); con.commit(); con.close()
        out = self.new('d1').stdout
        self.assertIn('sqlite db.sqlite3 (copied into the worktree)', out)
        copy = os.path.join(self.path('d1'), 'db.sqlite3')
        self.assertTrue(os.path.isfile(copy))
        stub = open(self.env['STUB_OUT']).read()
        self.assertIn('sqlite:///db.sqlite3', stub)
        self.assertIn('from env', stub)  # .env loaded without dotenv-cli
        con = sqlite3.connect(copy); con.execute('create table only_in_copy (x)'); con.commit(); con.close()
        names = [r[0] for r in sqlite3.connect(os.path.join(self.repo, 'db.sqlite3')).execute('select name from sqlite_master')]
        self.assertEqual(names, ['t'])
        self.wt('rm', 'd1'); self.names.remove('d1')
        self.assertFalse(os.path.exists(self.path('d1')))

    def test_absolute_sqlite_copy_rewrites_env_and_is_dropped(self):
        self.commit()
        db = os.path.join(self.tmp.name, 'data', 'app.db'); os.makedirs(os.path.dirname(db))
        sqlite3.connect(db).execute('create table t (x)').connection.commit()
        self.write('.env', f'DATABASE_URL="file:{db}"\nOTHER=1\n')
        out = self.new('a1').stdout
        self.assertIn('sqlite app_wt_a1.db', out)
        self.assertIn('no dev server', out)
        env = open(os.path.join(self.path('a1'), '.env')).read()
        self.assertIn(f'DATABASE_URL="file:{db[:-3]}_wt_a1.db"', env)
        self.assertIn('OTHER=1', env)
        self.assertFalse(os.path.islink(os.path.join(self.path('a1'), '.env')))
        self.assertIn(f'DATABASE_URL="file:{db}"', open(os.path.join(self.repo, '.env')).read())
        self.wt('rm', 'a1'); self.names.remove('a1')
        self.assertFalse(os.path.exists(db[:-3] + '_wt_a1.db'))
        self.assertTrue(os.path.exists(db))

    def test_hook_database(self):
        self.write('.wt-dev.conf', textwrap.dedent('''\
            WT_DEV_DB_CLONE='echo "$WT_DEV_SOURCE_URL" > "$WT_DEV_WORKTREE/../cloned-$WT_DEV_NAME"; echo "mongodb://localhost/app_$WT_DEV_NAME"'
            WT_DEV_DB_DROP='echo "$WT_DEV_DB_URL" > "$(dirname "$WT_DEV_WORKTREE")/dropped-$WT_DEV_NAME"'
        '''))
        self.commit()
        self.write('.env', 'DATABASE_URL=mongodb://localhost/app\n')
        self.new('h1')
        root = os.path.dirname(self.path('h1'))
        self.assertEqual(open(os.path.join(root, 'cloned-h1')).read().strip(), 'mongodb://localhost/app')
        self.assertIn('DATABASE_URL=mongodb://localhost/app_h1', open(os.path.join(self.path('h1'), '.env')).read())
        self.wt('rm', 'h1'); self.names.remove('h1')
        self.assertEqual(open(os.path.join(root, 'dropped-h1')).read().strip(), 'mongodb://localhost/app_h1')

    def test_unknown_local_database_fails_and_removes_the_worktree(self):
        self.commit()
        self.write('.env', 'DATABASE_URL=redis://localhost:6379/0\n')
        r = self.wt('new', 'u1', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('no database adapter for redis://', r.stderr)
        self.assertFalse(os.path.exists(self.path('u1')))
        self.assertEqual(self.wt('new', 'u2', '--shared-db').returncode, 0)
        self.names.append('u2')

    def test_install_commands_follow_lockfiles(self):
        for f in ('pnpm-lock.yaml', 'Gemfile.lock', 'uv.lock', 'yarn.lock', '.yarnrc.yml'):
            self.write(f, '')
        self.commit()
        self.new('i1', '--no-install')
        out = self.wt('install', 'i1', '-n').stdout.splitlines()
        self.assertEqual(out, ['pnpm install --frozen-lockfile --prefer-offline', 'yarn install --immutable',
                               'uv sync --frozen', 'bundle install --quiet'])

    def test_no_framework_means_no_server(self):
        self.write('main.go', 'package main\n'); self.write('go.mod', 'module x\n')
        self.commit()
        out = self.new('g1').stdout
        self.assertIn('no dev server', out)
        self.assertIn('g1', self.wt('ls').stdout)

    def test_busy_port_is_refused_not_reported_ready(self):
        self.write('package.json', json.dumps({'devDependencies': {'vite': '6'}}))
        self.write('tools/vite', '#!/bin/sh\nexec python3 "$(dirname "$0")/../../serve.py" "$2" "$@"\n', 0o755)
        self.write('.wt-dev.conf', 'WT_DEV_INSTALL="mkdir -p node_modules/.bin && cp tools/vite node_modules/.bin/vite"\n')
        self.commit()
        other = subprocess.Popen(['python3', '-m', 'http.server', '3999', '--bind', '127.0.0.1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                if self.listening(3999): break
                time.sleep(0.1)
            r = self.wt('new', 'p1', '--port', '3999', check=False); self.names.append('p1')
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('port 3999 is already in use', r.stderr)
        finally:
            other.kill(); other.wait()

    def test_sqlite_names_stay_unique_and_file_urls_are_absolute(self):
        self.commit()
        db = os.path.join(self.tmp.name, 'data', 'app.db'); os.makedirs(os.path.dirname(db))
        sqlite3.connect(db).execute('create table t (x)').connection.commit()
        self.write('.env', f'DATABASE_URL=file://{db}\n')
        self.new('a-b'); self.new('a_b')
        copies = sorted(f for f in os.listdir(os.path.dirname(db)) if '_wt_' in f)
        self.assertEqual(len(copies), 2, copies)
        self.wt('rm', 'a-b'); self.names.remove('a-b')
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(db), 'app_wt_a_b.db')))
        self.assertTrue(os.path.exists(db))

    def test_relative_sqlite_gets_private_env_and_survives_a_refused_rm(self):
        self.commit()
        self.write('.env', 'DATABASE_URL=file:./dev.db\n')
        sqlite3.connect(os.path.join(self.repo, 'dev.db')).execute('create table t (x)').connection.commit()
        self.new('r1')
        wt = self.path('r1')
        self.assertFalse(os.path.islink(os.path.join(wt, '.env')))
        open(os.path.join(wt, 'serve.py'), 'a').write('# dirty\n')
        r = self.wt('rm', 'r1', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(wt, 'dev.db')), 'refused rm lost the lane database')
        subprocess.run(['git', '-C', wt, 'checkout', '--', 'serve.py'], check=True)
        self.wt('rm', 'r1'); self.names.remove('r1')
        self.assertTrue(os.path.isfile(os.path.join(self.repo, 'dev.db')))

    def test_rm_holds_only_sqlite_files_so_untracked_notes_block_removal(self):
        self.commit()
        self.write('.env', 'DATABASE_URL=file:./dev.db\n')
        sqlite3.connect(os.path.join(self.repo, 'dev.db')).execute('create table t (x)').connection.commit()
        self.new('n1')
        wt = self.path('n1')
        open(os.path.join(wt, 'dev.db-wal'), 'w').write('wal')
        open(os.path.join(wt, 'dev.db.notes'), 'w').write('keep me')
        r = self.wt('rm', 'n1', check=False)
        self.assertNotEqual(r.returncode, 0, 'rm removed a worktree holding untracked notes')
        self.assertEqual(open(os.path.join(wt, 'dev.db.notes')).read(), 'keep me')
        self.assertTrue(os.path.isfile(os.path.join(wt, 'dev.db')), 'refused rm lost the lane database')
        self.assertTrue(os.path.isfile(os.path.join(wt, 'dev.db-wal')))
        os.rename(os.path.join(wt, 'dev.db.notes'), os.path.join(self.tmp.name, 'notes'))
        self.wt('rm', 'n1'); self.names.remove('n1')  # the -wal side file goes with the database
        self.assertFalse(os.path.exists(wt))

    def test_tracked_sqlite_symlink_to_a_shared_file_is_refused(self):
        shared = os.path.join(self.tmp.name, 'shared.db')
        sqlite3.connect(shared).execute('create table t (x)').connection.commit()
        os.symlink(shared, os.path.join(self.repo, 'dev.db'))
        self.git('add', '-f', 'dev.db'); self.commit()
        self.write('.env', 'DATABASE_URL=file:./dev.db\n')
        r = self.wt('new', 's1', check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn('symlink', r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.path('s1')), 'a refused clone left the worktree behind')

    def test_sqlite_folder_link_to_the_worktree_root_is_accepted(self):
        os.symlink('.', os.path.join(self.repo, 'data'))
        self.git('add', '-f', 'data'); self.commit()
        self.write('.env', 'DATABASE_URL=file:./data/dev.db\n')
        r = self.new('l1')
        self.assertIn('not found in the main checkout', r.stdout)

    def test_tracked_dangling_sqlite_symlink_is_refused(self):
        # The app would create the shared database on first use, so a missing target counts too.
        os.symlink(os.path.join(self.tmp.name, 'shared-not-yet.db'), os.path.join(self.repo, 'dev.db'))
        self.git('add', '-f', 'dev.db'); self.commit()
        self.write('.env', 'DATABASE_URL=file:./dev.db\n')
        r = self.wt('new', 's2', check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn('symlink', r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.path('s2')))

    def test_backend_preset_wins_over_asset_package_json(self):
        self.write('package.json', json.dumps({'devDependencies': {'vite': '6'}}))
        self.write('manage.py', 'import os, sys\nos.execvp("python3", ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve.py"), sys.argv[2].split(":")[1]])\n')
        self.write('.wt-dev.conf', 'WT_DEV_INSTALL=""\n')
        self.commit()
        self.assertIn('ready:', self.new('b1').stdout)
        self.assertNotIn('--strictPort', open(self.env['STUB_OUT']).read())  # started via manage.py, not vite


if __name__ == '__main__':
    unittest.main()
