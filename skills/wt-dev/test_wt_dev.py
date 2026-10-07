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

    def test_parallel_new_reserves_distinct_ports_and_releases_them(self):
        self.write('.wt-dev.conf', "WT_DEV_CMD='sleep 2; exec python3 serve.py \"$PORT\"'\nWT_DEV_INSTALL=''\n")
        self.commit(); self.env['WT_DEV_MAX_SETUPS'] = '2'
        self.names.extend(['race1', 'race2'])
        procs = [subprocess.Popen(['wt-dev', 'new', n], cwd=self.repo, env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in self.names]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, out + err)
        self.assertNotEqual(self.port('race1'), self.port('race2'))
        locks = os.path.join(self.home, '.cache', 'wt-dev', 'repo', 'ports')
        self.assertEqual(os.listdir(locks), ['.guard'])

    def test_port_reservations_expire_and_old_owner_cannot_release_new_lock(self):
        root = os.path.join(self.tmp.name, 'ports')
        helper = os.path.join(BIN, 'wtdev_db.py')
        def call(cmd, owner):
            return subprocess.run(['python3', helper, cmd, root, '3001', owner], capture_output=True)
        self.assertEqual(call('reserve-port', 'old').returncode, 0)
        self.assertNotEqual(call('reserve-port', 'new').returncode, 0)
        os.utime(os.path.join(root, '3001'), (time.time() - 301, time.time() - 301))
        self.assertEqual(call('reserve-port', 'new').returncode, 0)
        self.assertEqual(call('release-port', 'old').returncode, 0)
        self.assertEqual(open(os.path.join(root, '3001', 'owner')).read(), 'new')
        self.assertEqual(call('release-port', 'new').returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(root, '3001')))

    def test_failed_server_releases_port_reservation(self):
        self.write('.wt-dev.conf', "WT_DEV_CMD='exit 1'\nWT_DEV_INSTALL=''\n")
        self.commit(); self.names.append('failedport')
        self.assertNotEqual(self.wt('new', 'failedport', check=False).returncode, 0)
        locks = os.path.join(self.home, '.cache', 'wt-dev', 'repo', 'ports')
        self.assertEqual(os.listdir(locks), ['.guard'])

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

    def test_rm_uses_fresh_hold_and_reports_failed_rollback(self):
        self.commit(); self.write('.env', 'DATABASE_URL=file:./dev.db\n')
        sqlite3.connect(os.path.join(self.repo, 'dev.db')).execute('create table t (x)').connection.commit()
        self.new('holds'); lane = self.path('holds')
        open(os.path.join(lane, 'notes'), 'w').write('block removal')
        shims = os.path.join(self.tmp.name, 'mvshims'); os.makedirs(shims)
        stub = textwrap.dedent('''\
            #!/bin/sh
            case "$2" in *.dbhold.*/)
              printf '%s\\n' "$2" >>"$HOLD_LOG";; esac
            case "$1" in *.dbhold.*/*)
              [ "${FAIL_ROLLBACK:-0}" != 1 ] || exit 1;; esac
            exec /bin/mv "$@"
        ''')
        path = os.path.join(shims, 'mv'); open(path, 'w').write(stub); os.chmod(path, 0o755)
        self.env.update(PATH=shims + ':' + self.env['PATH'], HOLD_LOG=os.path.join(self.tmp.name, 'holds.log'))
        for _ in range(2):
            r = self.wt('rm', 'holds', check=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertTrue(os.path.isfile(os.path.join(lane, 'dev.db')))
        holds = open(self.env['HOLD_LOG']).read().splitlines()
        self.assertEqual(len(set(holds)), 2)
        # A leftover from an older run must not mix with the next hold.
        os.makedirs(holds[0]); open(os.path.join(holds[0], 'sentinel'), 'w').write('keep')
        self.env['FAIL_ROLLBACK'] = '1'
        r = self.wt('rm', 'holds', check=False)
        self.assertNotEqual(r.returncode, 0)
        held = open(self.env['HOLD_LOG']).read().splitlines()[-1].rstrip('/')
        self.assertIn('database rollback failed; held files at ' + held, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(held, 'dev.db')))
        self.assertEqual(open(os.path.join(holds[0], 'sentinel')).read(), 'keep')

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

    # A lane's own .wt-dev.conf (review 2026-10-06, P3)
    def test_lane_commands_use_the_lanes_own_config(self):
        self.write('.wt-dev.conf', "WT_DEV_INSTALL='printf main > config.used'\n")
        self.commit()
        self.new('c1', '--no-install')
        open(os.path.join(self.path('c1'), '.wt-dev.conf'), 'w').write("WT_DEV_INSTALL='printf lane > config.used'\n")
        self.wt('install', 'c1', '--if-changed')
        self.assertEqual(open(os.path.join(self.path('c1'), 'config.used')).read(), 'lane')

    def test_new_uses_the_config_committed_at_from(self):
        self.write('.wt-dev.conf', "WT_DEV_INSTALL='printf main > config.used'\n")
        self.commit()
        self.git('switch', '-q', '-c', 'cfg')
        self.write('.wt-dev.conf', "WT_DEV_INSTALL='printf cfg > config.used'\n")
        self.commit()
        self.git('switch', '-q', 'main')
        self.new('c2', '--from', 'cfg')
        self.assertEqual(open(os.path.join(self.path('c2'), 'config.used')).read(), 'cfg')

    def test_rm_drops_with_the_command_recorded_at_new(self):
        self.write('.wt-dev.conf', textwrap.dedent('''\
            WT_DEV_DB_CLONE='echo "mongodb://localhost/app_$WT_DEV_NAME"'
            WT_DEV_DB_DROP='touch "$(dirname "$WT_DEV_WORKTREE")/dropped-recorded"'
        '''))
        self.commit()
        self.write('.env', 'DATABASE_URL=mongodb://localhost/app\n')
        self.new('h2')
        lane = self.path('h2')
        open(os.path.join(lane, '.wt-dev.conf'), 'w').write("WT_DEV_DB_DROP='touch \"$(dirname \"$WT_DEV_WORKTREE\")/dropped-edited\"'\n")
        subprocess.run(['git', '-C', lane, '-c', 'user.name=t', '-c', 'user.email=user@example.invalid', 'commit', '-qam', 'edit'], check=True)
        self.write('.wt-dev.conf', "WT_DEV_DB_DROP='touch \"$(dirname \"$WT_DEV_WORKTREE\")/dropped-main\"'\n")
        self.commit()
        self.wt('rm', 'h2'); self.names.remove('h2')
        root = os.path.dirname(lane)
        self.assertEqual(sorted(f for f in os.listdir(root) if f.startswith('dropped-')), ['dropped-recorded'])

    def test_rm_of_a_lane_made_before_recording_uses_the_main_settings(self):
        self.write('.wt-dev.conf', textwrap.dedent('''\
            WT_DEV_DB_CLONE='echo "mongodb://localhost/app_$WT_DEV_NAME"'
            WT_DEV_DB_DROP='touch "$(dirname "$WT_DEV_WORKTREE")/dropped-main"'
        '''))
        self.commit()
        self.write('.env', 'DATABASE_URL=mongodb://localhost/app\n')
        self.new('h3')
        for f in ('h3.dbvar', 'h3.dbdrop'):  # what an older wt-dev left behind
            os.remove(os.path.join(self.home, '.cache/wt-dev/repo', f))
        lane = self.path('h3')
        open(os.path.join(lane, '.wt-dev.conf'), 'w').write("WT_DEV_DB_DROP='touch \"$(dirname \"$WT_DEV_WORKTREE\")/dropped-edited\"'\n")
        subprocess.run(['git', '-C', lane, '-c', 'user.name=t', '-c', 'user.email=user@example.invalid', 'commit', '-qam', 'edit'], check=True)
        self.wt('rm', 'h3'); self.names.remove('h3')
        root = os.path.dirname(lane)
        self.assertEqual(sorted(f for f in os.listdir(root) if f.startswith('dropped-')), ['dropped-main'])

    def land(self, name):
        env = dict(self.env, LAND_CHECK='true', GIT_AUTHOR_NAME='t', GIT_COMMITTER_NAME='t',
                   GIT_AUTHOR_EMAIL='user@example.invalid', GIT_COMMITTER_EMAIL='user@example.invalid')
        return subprocess.run(['land', name, '--keep'], cwd=self.repo, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_land_baseline_uses_main_config_and_only_listed_files(self):
        self.write('baseline', 'old\n'); self.write('other', 'old\n')
        self.write('.wt-dev.conf', "LAND_BASELINE_CMD='printf new > baseline'\nLAND_BASELINE_FILES='baseline'\n"); self.commit()
        self.new('baseline')
        lane = self.path('baseline')
        open(os.path.join(lane, 'change'), 'w').write('x')
        self.git('-C', lane, 'add', 'change'); self.git('-C', lane, 'commit', '-qm', 'change')
        r = self.land('baseline')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(open(os.path.join(self.repo, 'baseline')).read(), 'new')
        log = subprocess.check_output(['git', '-C', lane, 'log', '-1', '--format=%s'], text=True)
        self.assertEqual(log.strip(), 'chore(tooling): update baseline')

    def test_land_baseline_refuses_other_tracked_changes(self):
        self.write('baseline', 'old\n'); self.write('other', 'old\n')
        self.write('.wt-dev.conf', "LAND_BASELINE_CMD='printf new > baseline; printf bad > other'\nLAND_BASELINE_FILES='baseline'\n"); self.commit()
        self.new('badbaseline'); lane = self.path('badbaseline')
        open(os.path.join(lane, 'change'), 'w').write('x')
        self.git('-C', lane, 'add', 'change'); self.git('-C', lane, 'commit', '-qm', 'change')
        r = self.land('badbaseline')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('outside LAND_BASELINE_FILES', r.stdout)
        self.assertEqual(open(os.path.join(self.repo, 'baseline')).read(), 'old\n')

    def test_land_ignores_baseline_command_from_the_lane(self):
        self.write('baseline', 'old\n'); self.commit()
        self.new('lanebaseline'); lane = self.path('lanebaseline')
        canary = os.path.join(self.tmp.name, 'canary')
        open(os.path.join(lane, '.wt-dev.conf'), 'w').write(f"LAND_BASELINE_CMD='touch {canary}'\nLAND_BASELINE_FILES='baseline'\n")
        self.git('-C', lane, 'add', '.wt-dev.conf'); self.git('-C', lane, 'commit', '-qm', 'lane config')
        r = self.land('lanebaseline')
        self.assertFalse(os.path.exists(canary), r.stdout + r.stderr)

    def test_land_skips_unconfigured_project_specific_baseline(self):
        self.write('tools/check-file-size.mjs', 'throw new Error("must not run")\n')
        self.commit(); self.new('skipbaseline'); lane = self.path('skipbaseline')
        open(os.path.join(lane, 'change'), 'w').write('x')
        self.git('-C', lane, 'add', 'change'); self.git('-C', lane, 'commit', '-qm', 'change')
        r = self.land('skipbaseline')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_baseline_only_config_keeps_framework_detection(self):
        self.write('manage.py', 'import os, sys\nos.execvp("python3", ["python3", "serve.py", sys.argv[2].split(":")[1]])\n')
        self.write('.wt-dev.conf', "LAND_BASELINE_CMD='true'\nLAND_BASELINE_FILES='baseline'\n")
        self.commit()
        self.assertIn('ready: http://localhost:', self.new('detect').stdout)

    # Postgres copies: distinct names and an owner mark (review 2026-10-06, 1A)
    PSQL = textwrap.dedent('''\
        #!/usr/bin/env python3
        import json, os, re, sys
        path = os.environ['PG_STATE']; dbs = json.load(open(path)) if os.path.exists(path) else {}
        args = sys.argv[1:]; sql = next(a for a in args[1:] if ' ' in a)
        open(path + '.log', 'a').write(sql + '\\n')
        if 'pg_database' in sql and os.path.exists(path + '.down'): sys.exit(2)
        if m := re.match(r'drop database if exists "([^"]+)"', sql): dbs.pop(m[1], None)
        elif m := re.match(r"comment on database \\"([^\\"]+)\\" is '([^']*)'", sql): dbs[m[1]] = m[2]
        elif m := re.search(r"from pg_database where datname = '([^']*)'", sql):
            if m[1] in dbs: print('x' + dbs[m[1]])
        elif 'from pg_class' in sql: print('public|1')
        json.dump(dbs, open(path, 'w'))
    ''')

    def postgres(self, dbs=None):
        shims = os.path.join(self.tmp.name, 'pgshims'); os.makedirs(shims, exist_ok=True)
        self.pg = os.path.join(self.tmp.name, 'pg.json')
        json.dump(dbs or {}, open(self.pg, 'w'))
        stubs = {'psql': self.PSQL, 'pg_dump': '#!/bin/sh\nexit 0\n', 'pg_restore': '#!/bin/sh\ncat >/dev/null\n',
                 'createdb': '#!/usr/bin/env python3\nimport json, os, sys\np = os.environ["PG_STATE"]; d = json.load(open(p)); d[sys.argv[-1]] = ""; json.dump(d, open(p, "w"))\n'}
        for name, text in stubs.items():
            open(os.path.join(shims, name), 'w').write(text); os.chmod(os.path.join(shims, name), 0o755)
        self.env.update(PG_STATE=self.pg, PATH=shims + ':' + self.env['PATH'])
        self.write('.wt-dev.conf', 'WT_DEV_INSTALL=""\n')
        self.git('add', '-A'); self.git('commit', '-qm', 'x', '--allow-empty')
        self.write('.env', 'DATABASE_URL=postgresql://localhost/main\n')

    def test_postgres_password_is_only_in_client_environment(self):
        self.postgres()
        password = 'fixture-pass@word'
        self.write('.env', 'DATABASE_URL=postgresql://user:fixture-pass%40word@localhost/main\n')
        shims = os.path.join(self.tmp.name, 'pgshims')
        record = "import json, os, sys\nwith open(os.environ['PG_STATE'] + '.argv', 'a') as f: f.write(json.dumps([os.path.basename(sys.argv[0]), sys.argv[1:], os.environ.get('PGPASSWORD'), os.environ.get('WT_DEV_PG_URL')]) + '\\n')\n"
        for tool in ('psql', 'createdb', 'pg_dump', 'pg_restore'):
            path = os.path.join(shims, tool)
            old = open(path).read()
            if tool in ('pg_dump', 'pg_restore'):
                old = '#!/usr/bin/env python3\n' + ('sys.stdin.read()\n' if tool == 'pg_restore' else '')
            open(path, 'w').write(old.split('\n', 1)[0] + '\n' + record + old.split('\n', 1)[1])
        self.new('private')
        self.wt('rm', 'private'); self.names.remove('private')
        calls = [json.loads(line) for line in open(self.pg + '.argv')]
        self.assertEqual({c[0] for c in calls}, {'psql', 'createdb', 'pg_dump', 'pg_restore'})
        for _, args, env_password, raw_url in calls:
            self.assertFalse(any(password in a or 'fixture-pass%40word' in a for a in args))
            self.assertEqual(env_password, password)
            self.assertIsNone(raw_url)

    def dbs(self):
        return json.load(open(self.pg))

    def test_postgres_names_that_lose_characters_stay_apart(self):
        self.postgres()
        self.new('review-a'); self.new('review_a')
        dbs = self.dbs()
        self.assertEqual(len(dbs), 2, dbs)
        self.assertIn('main_wt_review_a', dbs)
        self.assertTrue(all(mark.startswith('wt-dev ') for mark in dbs.values()), dbs)
        self.assertNotIn('drop database', open(self.pg + '.log').read())

    def test_postgres_database_without_this_lanes_mark_is_never_dropped(self):
        for mark in ('', 'wt-dev 000000000000 p1'):
            with self.subTest(mark=mark):
                self.postgres({'main_wt_p1': mark})
                r = self.wt('new', 'p1', check=False)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("main_wt_p1 exists and is not this lane's", r.stderr)
                self.assertEqual(self.dbs(), {'main_wt_p1': mark})
                self.assertFalse(os.path.exists(self.path('p1')))

    def test_postgres_leftover_of_this_lane_is_replaced_and_rm_drops_it(self):
        self.postgres()
        self.new('p2')
        mark = self.dbs()['main_wt_p2']
        subprocess.run(['git', '-C', self.repo, 'worktree', 'remove', '--force', self.path('p2')], check=True)
        self.new('p2')  # the leftover carries this lane's mark
        self.assertEqual(self.dbs(), {'main_wt_p2': mark})
        self.wt('rm', 'p2'); self.names.remove('p2')
        self.assertEqual(self.dbs(), {})

    def test_rm_keeps_a_database_marked_as_another_lanes(self):
        self.postgres()
        self.new('p3')
        json.dump({'main_wt_p3': 'wt-dev 000000000000 p3'}, open(self.pg, 'w'))
        out = self.wt('rm', 'p3').stdout; self.names.remove('p3')
        self.assertIn("not dropping database main_wt_p3", out)
        self.assertEqual(self.dbs(), {'main_wt_p3': 'wt-dev 000000000000 p3'})

    def test_legacy_rm_never_runs_the_lanes_drop_command(self):
        self.write('.wt-dev.conf', textwrap.dedent('''\
            WT_DEV_DB_CLONE='echo "mongodb://localhost/app_$WT_DEV_NAME"'
            WT_DEV_DB_DROP='true'
        '''))
        self.commit()
        self.write('.env', 'DATABASE_URL=mongodb://localhost/app\n')
        self.new('h4')
        for f in ('h4.dbvar', 'h4.dbdrop'):  # what an older wt-dev left behind
            os.remove(os.path.join(self.home, '.cache/wt-dev/repo', f))
        self.write('.wt-dev.conf', "WT_DEV_INSTALL=''\n")  # main no longer sets a drop command
        self.commit()
        lane = self.path('h4')
        open(os.path.join(lane, '.wt-dev.conf'), 'w').write("WT_DEV_DB_DROP='touch \"$(dirname \"$WT_DEV_WORKTREE\")/dropped-edited\"'\n")
        subprocess.run(['git', '-C', lane, '-c', 'user.name=t', '-c', 'user.email=user@example.invalid', 'commit', '-qam', 'edit'], check=True)
        self.wt('rm', 'h4'); self.names.remove('h4')
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(lane), 'dropped-edited')))

    def test_rm_keeps_a_database_whose_owner_cannot_be_checked(self):
        self.postgres()
        self.new('p4')
        open(self.pg + '.down', 'w').close()
        out = self.wt('rm', 'p4').stdout; self.names.remove('p4')
        self.assertIn('not dropping database main_wt_p4: could not check', out)
        self.assertIn('main_wt_p4', self.dbs())

    # Two repos with the same folder name (review 2026-10-06, 1A)
    def other_repo(self):
        other = os.path.join(self.tmp.name, 'elsewhere', 'repo')
        os.makedirs(other)
        subprocess.run(['git', 'init', '-q', '-b', 'main', other], check=True)
        return other

    def wt_in(self, cwd, *a):
        return subprocess.run(['wt-dev', *a], cwd=cwd, env=self.env, capture_output=True, text=True, timeout=60)

    def test_repos_with_the_same_folder_name_get_separate_lanes(self):
        self.commit()
        other = self.other_repo()
        mine, theirs = self.wt('path', 'a').stdout.strip(), self.wt_in(other, 'path', 'a').stdout.strip()
        self.assertEqual(mine, self.path('a'))
        self.assertNotEqual(mine, theirs)
        self.assertRegex(theirs, r'/worktrees/repo-[0-9a-f]{8}/a$')
        self.assertEqual(self.wt_in(other, 'path', 'a').stdout.strip(), theirs)

    def test_claim_of_a_moved_repo_is_taken_over_but_a_live_one_is_kept(self):
        self.commit()
        claim = os.path.join(self.home, '.cache/wt-dev/repo/.repo')
        os.makedirs(os.path.dirname(claim), exist_ok=True)
        open(claim, 'w').write(os.path.join(self.tmp.name, 'moved-away', 'repo') + '\n')
        self.assertEqual(self.wt('path', 'a').stdout.strip(), self.path('a'))
        open(claim, 'w').write(self.other_repo() + '\n')
        self.assertRegex(self.wt('path', 'a').stdout.strip(), r'/worktrees/repo-[0-9a-f]{8}/a$')

    def test_a_claim_leaves_no_lock_behind(self):
        self.commit()
        self.wt('path', 'a')
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, '.cache/wt-dev/repo'))), ['.repo'])

    def test_unclaimed_lane_folder_stays_with_the_repo_whose_lanes_it_holds(self):
        self.write('.wt-dev.conf', 'WT_DEV_INSTALL=""\n')
        self.commit()
        self.new('n1')
        os.remove(os.path.join(self.home, '.cache/wt-dev/repo/.repo'))  # lanes made before claims existed
        theirs = self.wt_in(self.other_repo(), 'path', 'n1').stdout.strip()
        self.assertNotEqual(theirs, self.path('n1'))
        self.assertEqual(self.wt('path', 'n1').stdout.strip(), self.path('n1'))


class DevRestart(unittest.TestCase):
    """devrestart stops only this checkout's server, never another project's (real processes)."""
    LISTEN = 'import http.server, sys; http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), http.server.SimpleHTTPRequestHandler).serve_forever()'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='devrestart-test-')
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.home, self.repo, self.other = (os.path.join(t, d) for d in ('home', 'repo', 'other'))
        shims = os.path.join(t, 'shims')
        for d in (self.home, self.repo, self.other, shims):
            os.makedirs(d)
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        self.repo = subprocess.run(['git', '-C', self.repo, 'rev-parse', '--show-toplevel'], capture_output=True, text=True).stdout.strip()
        import socket
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0)); self.port = str(s.getsockname()[1])
        # pnpm dev: a child that must die with the group, then the listener.
        with open(os.path.join(shims, 'pnpm'), 'w') as f:
            f.write(f'#!/bin/sh\nsleep 300 &\necho $! >"{t}/child.$$"\n[ -z "${{IGNORE_TERM:-}}" ] || trap "" TERM\nexec python3 -c \'{self.LISTEN}\' {self.port}\n')
        os.chmod(os.path.join(shims, 'pnpm'), 0o755)
        self.env = dict(os.environ, HOME=self.home, PATH=shims + ':' + BIN + ':' + os.environ['PATH'])
        self.procs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in self.procs:
            if p.poll() is None: p.kill(); p.wait()
        rec = os.path.join(self.home, '.claude/state/logs/repo/devserver.pid')
        if os.path.exists(rec):
            subprocess.run(['kill', '-KILL', '-' + self.read(rec).splitlines()[0]], capture_output=True)
        for f in os.listdir(self.tmp.name):
            if f.startswith('child.'):
                subprocess.run(['kill', '-KILL', self.read(os.path.join(self.tmp.name, f))], capture_output=True)

    def read(self, path):
        with open(path) as f:
            return f.read().strip()

    def spawn(self, *args, cwd):
        p = subprocess.Popen(args, cwd=cwd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def restart(self):
        return subprocess.run(['devrestart', self.port], cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=60)

    def alive(self, pid):
        return subprocess.run(['kill', '-0', str(pid)], capture_output=True).returncode == 0

    def test_only_this_checkouts_server_is_stopped(self):
        other = self.spawn('python3', '-c', self.LISTEN, self.port, cwd=self.other)
        for _ in range(50):
            if subprocess.run(['lsof', '-tiTCP:' + self.port, '-sTCP:LISTEN'], capture_output=True).stdout.strip(): break
            time.sleep(0.1)
        r = self.restart()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('outside', r.stdout)
        self.assertIsNone(other.poll(), 'another project\'s server on the port was killed')
        other.kill(); other.wait()
        turbo = self.spawn('python3', '-c', 'import time; time.sleep(300)', 'turbo dev', cwd=self.other)
        r = self.restart()
        self.assertIn('up :' + self.port, r.stdout)
        rec = os.path.join(self.home, '.claude/state/logs/repo/devserver.pid')
        first = self.read(rec).splitlines()[0]
        child = self.read(os.path.join(self.tmp.name, 'child.' + first))
        self.assertTrue(self.alive(first) and self.alive(child))
        r = self.restart()
        self.assertIn('up :' + self.port, r.stdout)
        second = self.read(rec).splitlines()[0]
        self.assertNotEqual(first, second)
        time.sleep(0.5)
        self.assertFalse(self.alive(first), 'the old server was left running')
        self.assertFalse(self.alive(child), 'the old server\'s child was left running')
        self.assertIsNone(turbo.poll(), 'an unrelated "turbo dev" process was killed')

    def test_a_stray_listener_in_the_checkout_that_ignores_term_is_not_killed(self):
        stray = self.spawn('python3', '-c', 'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); ' + self.LISTEN, self.port, cwd=self.repo)
        for _ in range(50):
            if subprocess.run(['lsof', '-tiTCP:' + self.port, '-sTCP:LISTEN'], capture_output=True).stdout.strip(): break
            time.sleep(0.1)
        r = self.restart()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('still held', r.stdout)
        self.assertIsNone(stray.poll(), 'a listener devrestart did not start was killed')

    def test_a_server_that_ignores_term_is_replaced_not_reported_as_new(self):
        self.env['IGNORE_TERM'] = '1'
        self.assertIn('up :' + self.port, self.restart().stdout)
        rec = os.path.join(self.home, '.claude/state/logs/repo/devserver.pid')
        first = self.read(rec).splitlines()[0]
        self.env.pop('IGNORE_TERM')
        r = self.restart()
        self.assertIn('up :' + self.port, r.stdout)
        time.sleep(0.5)
        self.assertFalse(self.alive(first), 'the old server still answers on the port')
        listener = subprocess.run(['lsof', '-tiTCP:' + self.port, '-sTCP:LISTEN'], capture_output=True, text=True).stdout.split()
        self.assertEqual(listener, [self.read(rec).splitlines()[0]])


if __name__ == '__main__':
    unittest.main()
