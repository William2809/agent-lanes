#!/usr/bin/env python3
# ABOUTME: wt-dev helpers that are awkward in shell: per-worktree SQLite and MySQL/MariaDB copies,
# ABOUTME: .env rewriting, and running a dev server with a .env loaded (no dotenv-cli needed).
# Usage (called by wt-dev; never prints database URLs, they hold passwords):
#   wtdev_db.py clone sqlite|mysql REPO WT APP NAME VAR   -> stdout: "label<TAB>state"; may write WT/.env.wt-dev.new
#   wtdev_db.py drop sqlite|mysql REPO WT STATE VAR
#   wtdev_db.py env-set SRC DST VAR < VALUE               -> copy SRC to DST (mode 600) with VAR=VALUE (stdin)
#   wtdev_db.py env-exec ENVFILE -- CMD...                -> exec CMD with ENVFILE's vars (existing env wins)
import hashlib, os, re, shutil, sqlite3, subprocess, sys, tempfile
from urllib.parse import urlsplit, urlunsplit, unquote

sys.dont_write_bytecode = True


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def read_env(path):
    """KEY=VALUE lines of a dotenv file: comments, blank lines, `export ` and matching quotes handled."""
    out = []
    try:
        lines = open(path, encoding='utf-8').read().splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if not s or s.startswith('#') or '=' not in s:
            out.append((None, line, None, ''))
            continue
        k, v = s.split('=', 1)
        k = k.strip()
        if k.startswith('export '):
            k = k[7:].strip()
        v = v.strip()
        q = ''
        if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
            q, v = v[0], v[1:-1]
            if q == '"':
                v = v.replace('\\n', '\n')
        elif ' #' in v:
            v = v.split(' #', 1)[0].rstrip()
        out.append((k, line, v, q))
    return out


def env_value(path, var):
    for k, _, v, _ in read_env(path):
        if k == var:
            return v
    return ''


def write_env(src, dst, change):
    """Copy dotenv SRC to DST (mode 600), passing each (key, value) through change(); None keeps the line."""
    lines = []
    for k, line, v, q in read_env(src):
        new = change(k, v) if k else None
        lines.append(line if new is None else f'{k}={q}{new}{q}')
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(lines) + '\n')


def suffix(name):
    """Per-lane suffix; names that lose characters get a hash, so a-b and a_b never share a copy."""
    s = re.sub(r'[^a-z0-9]', '_', name.lower())
    return s if s == name else f'{s}_{hashlib.sha1(name.encode()).hexdigest()[:6]}'


def inside(path, root):
    """True when PATH (symlinks resolved) is strictly inside ROOT: deletes never follow a link out."""
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp.startswith(rr + os.sep)


# --- SQLite -------------------------------------------------------------------------------------
def sqlite_path(url):
    """Path of an SQLite URL: sqlite:///rel.db, sqlite:////abs.db, sqlite3:db/x.sqlite3, file:./dev.db, or a bare path."""
    u = url.split('?', 1)[0]
    m = re.match(r'^(sqlite3?|file):(.*)$', u)
    if m:
        scheme, rest = m.groups()
        if scheme == 'file':  # file:./dev.db, file:/abs.db, file:///abs.db (empty host)
            return rest[2:] if rest.startswith('//') else rest
        if rest.startswith('///'):  # SQLAlchemy/dj-database-url: sqlite:///rel.db, sqlite:////abs.db
            return rest[3:]
        return rest[2:] if rest.startswith('//') else rest
    return u if re.search(r'\.(db|sqlite3?)$', u) else None


def sqlite_copy(src, dst):
    con = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
    out = sqlite3.connect(dst)
    try:
        con.backup(out)
        count = "select count(*) from sqlite_master where type = 'table'"
        if con.execute(count).fetchone() != out.execute(count).fetchone():
            raise RuntimeError('table counts differ')
    finally:
        con.close(); out.close()


def sqlite_clone(repo, wt, app, name, var):
    url = env_value(os.path.join(repo, '.env'), var)
    path = sqlite_path(url)
    if not path:
        fail(f'{var} is not an SQLite URL or path')
    env_src, env_new = os.path.join(repo, '.env'), os.path.join(wt, '.env.wt-dev.new')
    if os.path.isabs(path):
        stem, ext = os.path.splitext(path)
        dst = f'{stem}_wt_{suffix(name)}{ext}'
        if dst == path:
            fail('sqlite copy would overwrite the source')
        for f in (dst, dst + '-wal', dst + '-shm'):
            if os.path.exists(f):
                os.remove(f)  # a leftover of a removed worktree with the same name
        # A missing source still gets its own path, so the lane never creates the shared file.
        if os.path.exists(path):
            sqlite_copy(path, dst)
        write_env(env_src, env_new, lambda k, v: url.replace(path, dst) if k == var else None)
        what = f'copy of {os.path.basename(path)}' if os.path.exists(path) else 'new; the source does not exist yet'
        print(f'sqlite {os.path.basename(dst)} ({what})\tabs:{dst}')
        return
    # Relative paths resolve inside the worktree, so the copy goes to the same place there; the URL
    # stays. The worktree still gets a private .env, so a lane's edits never reach the main one.
    write_env(env_src, env_new, lambda k, v: None)
    # Relative paths resolve inside the worktree, so the copy goes to the same place there; the URL stays.
    for base in (app, '.', os.path.join(app, 'prisma'), 'prisma'):
        rel = os.path.normpath(os.path.join(base, path))
        if rel.startswith('..'):
            continue
        dst = os.path.join(wt, rel)
        # A tracked symlink (dev.db -> /shared/app.db), even a dangling one, gives every lane one database.
        if os.path.islink(dst):
            fail(f'sqlite {rel} is a symlink; lanes would share one database')
        parent = os.path.dirname(dst)
        if os.path.dirname(rel) and not inside(parent, wt) and os.path.realpath(parent) != os.path.realpath(wt):
            fail(f'sqlite {rel}: a folder on its path links outside the worktree')
        if os.path.isfile(os.path.join(repo, rel)):
            if os.path.exists(dst):
                print(f'sqlite {rel} (tracked in git; the worktree has its own copy)\t')
                return
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            sqlite_copy(os.path.join(repo, rel), dst)
            print(f'sqlite {rel} (copied into the worktree)\trel:{rel}')
            return
    print(f'sqlite {path} (not found in the main checkout; the worktree starts without it)\t')


def sqlite_drop(wt, state):
    kind, _, path = state.partition(':')
    if kind == 'rel':
        path = os.path.join(wt, path)
        if not inside(path, wt):
            fail(f'refusing to delete {path}: it resolves outside the worktree')
    elif kind == 'abs':
        if '_wt_' not in os.path.basename(path):
            fail(f'refusing to delete {path}: not a wt-dev copy')
    else:
        return
    for f in (path, path + '-wal', path + '-shm', path + '-journal'):
        if os.path.isfile(f) and not os.path.islink(f):
            os.remove(f)
    print(f'dropped sqlite copy {os.path.basename(path)}')


# --- MySQL / MariaDB ------------------------------------------------------------------------------
def mysql_parts(url):
    p = urlsplit(url)
    return p, unquote(p.path.lstrip('/'))


def mysql_defaults(p):
    """A private client option file, so the password never appears in a process list."""
    fd, path = tempfile.mkstemp(prefix='wtdev-my-', suffix='.cnf')
    with os.fdopen(fd, 'w') as f:
        f.write('[client]\n')
        if p.username: f.write(f'user={unquote(p.username)}\n')
        if p.password: f.write(f'password={unquote(p.password)}\n')
        if p.hostname: f.write(f'host={p.hostname}\n')
        if p.port: f.write(f'port={p.port}\nprotocol=tcp\n')
    return path


def mysql(defaults, sql, db=None):
    cmd = ['mysql', f'--defaults-extra-file={defaults}', '-N', '-B', '-e', sql] + ([db] if db else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError('mysql failed: ' + (r.stderr.strip().splitlines() or ['?'])[-1][:200])
    return r.stdout.strip()


def table_count(defaults, db):
    return mysql(defaults, f"select count(*) from information_schema.tables where table_schema = '{db}'")


def with_db(url, db):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, '/' + db, p.query, p.fragment))


def mysql_clone(repo, wt, name, var):
    for tool in ('mysql', 'mysqldump'):
        if not shutil.which(tool):
            fail(f'{tool} missing')
    url = env_value(os.path.join(repo, '.env'), var)
    p, src = mysql_parts(url)
    if not src:
        fail(f'{var} names no database')
    dst = f'{src}_wt_{suffix(name)}'
    if len(dst) > 64:  # MySQL's limit: keep it unique instead of truncating into the source name
        dst = f'{src[:40]}_wt_{hashlib.sha1(dst.encode()).hexdigest()[:12]}'
    if dst == src:
        fail('database copy would overwrite the source')
    defaults = mysql_defaults(p)
    try:
        mysql(defaults, f'drop database if exists `{dst}`; create database `{dst}`')
        dump = ['mysqldump', f'--defaults-extra-file={defaults}', '--single-transaction', '--routines', '--triggers']
        if 'set-gtid-purged' in subprocess.run(['mysqldump', '--help'], capture_output=True, text=True).stdout:
            dump.append('--set-gtid-purged=OFF')  # MySQL only; MariaDB rejects the option
        d = subprocess.Popen(dump + [src], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        r = subprocess.run(['mysql', f'--defaults-extra-file={defaults}', dst], stdin=d.stdout, capture_output=True)
        d.stdout.close()
        if d.wait() or r.returncode:
            raise RuntimeError('dump/restore failed')
        if table_count(defaults, src) != table_count(defaults, dst):
            raise RuntimeError('clone table counts differ from source')
    except RuntimeError as e:
        try: mysql(defaults, f'drop database if exists `{dst}`')
        except RuntimeError: pass
        os.remove(defaults)
        fail(str(e))
    os.remove(defaults)

    def change(k, v):
        if v and '://' in v:
            q, db = mysql_parts(v)
            if db == src and q.scheme.startswith(('mysql', 'mariadb')):
                return with_db(v, dst)
        return None
    write_env(os.path.join(repo, '.env'), os.path.join(wt, '.env.wt-dev.new'), change)
    print(f'database: {dst} (clone of {src})\tmysql:{dst}')


def mysql_drop(repo, state, var):
    db = state.partition(':')[2]
    if not db or '_wt_' not in db:
        return
    p, _ = mysql_parts(env_value(os.path.join(repo, '.env'), var))
    defaults = mysql_defaults(p)
    try:
        mysql(defaults, f'drop database if exists `{db}`')
        print(f'dropped database {db}')
    finally:
        os.remove(defaults)


def main(argv):
    if len(argv) < 2:
        fail(open(__file__).read().split('\n')[3])
    cmd = argv[1]
    if cmd == 'env-exec':
        envfile, rest = argv[2], argv[3:]
        if rest[:1] == ['--']:
            rest = rest[1:]
        for k, _, v, _ in read_env(envfile):
            if k and k not in os.environ:
                os.environ[k] = v
        os.execvp(rest[0], rest)
    if cmd == 'env-set':
        src, dst, var = argv[2:5]
        value = sys.stdin.read().rstrip('\n')
        write_env(src, dst, lambda k, v: value if k == var else None)
        return
    if cmd == 'clone':
        engine, repo, wt, app, name, var = argv[2:8]
        if engine == 'sqlite':
            try: sqlite_clone(repo, wt, app, name, var)
            except (sqlite3.Error, RuntimeError, OSError) as e: fail(f'sqlite copy failed: {e}')
        elif engine == 'mysql':
            mysql_clone(repo, wt, name, var)
        else:
            fail(f'unknown engine {engine}')
        return
    if cmd == 'drop':
        engine, repo, wt, state, var = argv[2:7]
        if engine == 'sqlite':
            sqlite_drop(wt, state)
        elif engine == 'mysql':
            mysql_drop(repo, state, var)
        return
    fail(f'unknown command {cmd}')


if __name__ == '__main__':
    main(sys.argv)
