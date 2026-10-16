"""Compatibility between private remote-ci and public remote-ci clients and host kits."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
REMOTE = HERE / "remote"
NATIVE_PREFIX = "REMOTE_CI_"  # Intentionally changed by tools/export-public.
PRIVATE_PREFIX = "M" + "INI_CI_"
PUBLIC_PREFIX = "R" + "EMOTE_CI_"
NATIVE_TOOL = "remote-ci"  # Intentionally changed by tools/export-public.
PRIVATE_TOOL = "m" + "ini-ci"
PUBLIC_TOOL = "r" + "emote-ci"
PRIVATE_INTERNAL = "m" + "ini_ci"
PUBLIC_INTERNAL = "r" + "emote_ci"


def opposite(value, private, public):
    return public if value == private else private


def opposite_kit_text(text):
    replacements = (
        (NATIVE_TOOL, opposite(NATIVE_TOOL, PRIVATE_TOOL, PUBLIC_TOOL)),
        (NATIVE_PREFIX, opposite(NATIVE_PREFIX, PRIVATE_PREFIX, PUBLIC_PREFIX)),
        (PRIVATE_INTERNAL if NATIVE_PREFIX == PRIVATE_PREFIX else PUBLIC_INTERNAL,
         PUBLIC_INTERNAL if NATIVE_PREFIX == PRIVATE_PREFIX else PRIVATE_INTERNAL),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def shell_quote(path):
    import shlex
    return shlex.quote(str(path))


class SchedulerCompatibility(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="scheduler-compat-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith((PRIVATE_PREFIX, PUBLIC_PREFIX))}
        self.env.update(HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")

    def shell(self, script, env=None):
        return subprocess.run(["/bin/bash", "-c", script], env=env or self.env,
                              text=True, capture_output=True, timeout=20)

    def write_conf(self, name, text):
        path = self.home / name / "conf"
        path.parent.mkdir(parents=True)
        path.write_text(text)
        return path

    def load(self, library, project, conf, output, env=None):
        return self.shell(f'''source {shell_quote(library)}
pg_profile_configure() {{ :; }}
load_project {project} {shell_quote(conf)} || exit
printf '%s' "{output}"
''', env)

    def test_private_kit_reads_public_config_and_bootstrap(self):
        alternate = opposite(NATIVE_PREFIX, PRIVATE_PREFIX, PUBLIC_PREFIX)
        conf = self.write_conf("alternate", f"{alternate}CHECK='alternate task'\n{alternate}SLOTS=3\n")
        env = dict(self.env, **{alternate + "BOOTSTRAP": "1"})
        call = self.load(REMOTE / "lib.sh", "public", conf,
                         f'${NATIVE_PREFIX}CHECK|${NATIVE_PREFIX}SLOTS|${NATIVE_PREFIX}BOOTSTRAP', env)
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertEqual(call.stdout, "alternate task|3|1")

    def test_second_load_discards_first_load_defaults(self):
        alternate = opposite(NATIVE_PREFIX, PRIVATE_PREFIX, PUBLIC_PREFIX)
        first = self.write_conf("first", f"{NATIVE_PREFIX}CHECK='first task'\n")
        second = self.write_conf("second", f"{alternate}CHECK='second task'\n{alternate}SLOTS=4\n")
        call = self.shell(f'''source {shell_quote(REMOTE / "lib.sh")}
pg_profile_configure() {{ :; }}
load_project first {shell_quote(first)} || exit
load_project second {shell_quote(second)} || exit
printf '%s|%s' "${NATIVE_PREFIX}CHECK" "${NATIVE_PREFIX}SLOTS"
''')
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertEqual(call.stdout, "second task|4")

    def test_explicit_canonical_setting_wins(self):
        alternate = opposite(NATIVE_PREFIX, PRIVATE_PREFIX, PUBLIC_PREFIX)
        conf = self.write_conf("both", f"{NATIVE_PREFIX}CHECK='canonical'\n{alternate}CHECK='alternate'\n")
        call = self.load(REMOTE / "lib.sh", "both", conf, f'${NATIVE_PREFIX}CHECK')
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertEqual(call.stdout, "canonical")

    def test_opposite_transformation_reads_native_config(self):
        kit = self.home / "opposite-kit"
        shutil.copytree(REMOTE, kit)
        for path in kit.rglob("*"):
            if path.is_file():
                try:
                    path.write_text(opposite_kit_text(path.read_text()))
                except UnicodeDecodeError:
                    pass
        opposite_prefix = opposite(NATIVE_PREFIX, PRIVATE_PREFIX, PUBLIC_PREFIX)
        conf = self.write_conf("native", f"{NATIVE_PREFIX}CHECK='native task'\n{NATIVE_PREFIX}SLOTS=2\n")
        call = self.load(kit / "lib.sh", "native", conf,
                         f'${opposite_prefix}CHECK|${opposite_prefix}SLOTS')
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertEqual(call.stdout, "native task|2")

    def test_real_receive_hook_accepts_both_names_and_rejects_old(self):
        protocol = self.home / "ci/kit/PROTOCOL"
        protocol.parent.mkdir(parents=True)
        protocol.write_text("2\n")
        source = self.home / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "config", "user.name", "Fixture")
        self.git(source, "config", "user.email", "you@example.invalid")
        (source / "file").write_text("one\n")
        self.git(source, "add", "file")
        self.git(source, "commit", "-qm", "one")
        for index, tool in enumerate((PRIVATE_TOOL, PUBLIC_TOOL)):
            bare = self.home / f"host-{index}.git"
            self.git(self.home, "init", "--bare", "-q", bare)
            shutil.copy2(REMOTE / "pre-receive.sh", bare / "hooks/pre-receive")
            (bare / "hooks/pre-receive").chmod(0o755)
            self.git(bare, "config", "receive.advertisePushOptions", "true")
            call = self.git(source, "push", "-q", "-o", tool + "=2", str(bare),
                            "HEAD:refs/heads/main", check=False, env=self.env)
            self.assertEqual(call.returncode, 0, call.stderr)
        old = self.home / "old.git"
        self.git(self.home, "init", "--bare", "-q", old)
        shutil.copy2(REMOTE / "pre-receive.sh", old / "hooks/pre-receive")
        (old / "hooks/pre-receive").chmod(0o755)
        self.git(old, "config", "receive.advertisePushOptions", "true")
        call = self.git(source, "push", "-q", "-o", PUBLIC_TOOL + "=1", str(old),
                        "HEAD:refs/heads/main", check=False, env=self.env)
        self.assertNotEqual(call.returncode, 0)
        self.assertIn("needs protocol 2", call.stderr)

    def test_opposite_transformed_hook_accepts_native_name(self):
        protocol = self.home / "ci/kit/PROTOCOL"
        protocol.parent.mkdir(parents=True)
        protocol.write_text("2\n")
        source = self.home / "opposite-source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "config", "user.name", "Fixture")
        self.git(source, "config", "user.email", "you@example.invalid")
        (source / "file").write_text("opposite\n")
        self.git(source, "add", "file")
        self.git(source, "commit", "-qm", "opposite")
        bare = self.home / "opposite-host.git"
        self.git(self.home, "init", "--bare", "-q", bare)
        hook = bare / "hooks/pre-receive"
        hook.write_text(opposite_kit_text((REMOTE / "pre-receive.sh").read_text()))
        hook.chmod(0o755)
        self.git(bare, "config", "receive.advertisePushOptions", "true")
        call = self.git(source, "push", "-q", "-o", NATIVE_TOOL + "=2", str(bare),
                        "HEAD:refs/heads/main", check=False, env=self.env)
        self.assertEqual(call.returncode, 0, call.stderr)

    def git(self, cwd, *args, check=True, env=None):
        return subprocess.run(["/usr/bin/git", *map(str, args)], cwd=cwd, env=env,
                              text=True, capture_output=True, check=check, timeout=20)


if __name__ == "__main__":
    unittest.main()
