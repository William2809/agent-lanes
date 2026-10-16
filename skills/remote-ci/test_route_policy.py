"""Exact, fail-closed command routing policy tests."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


KIT = Path(__file__).resolve().parent
SCRIPT = KIT / "bin/route_policy.py"
UNSET = object()
sys.dont_write_bytecode = True


def module():
    spec = importlib.util.spec_from_file_location("route_policy", SCRIPT)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class RoutePolicy(unittest.TestCase):
    def setUp(self):
        self.policy = module()
        self.base = {
            "version": 1,
            "projects": {
                "shiftread": {
                    "default_needs": ["macos"],
                    "commands": {
                        "pnpm check:full": ["shiftread-portable"],
                        "true": [],
                    },
                },
            },
        }

    def write(self, root, value=UNSET, text=None):
        path = Path(root) / "policy.json"
        path.write_text(text if text is not None else json.dumps(self.base if value is UNSET else value))
        return path

    def call(self, path, project="shiftread", command=None):
        args = [sys.executable, str(SCRIPT), str(path), project]
        if command is not None:
            args.append(command)
        return subprocess.run(args, capture_output=True, text=True, timeout=60)

    def test_exact_command_match_and_empty_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp)
            self.assertEqual(self.policy.needs(path, "shiftread", "pnpm check:full"),
                             ["shiftread-portable"])
            result = self.call(path, command="true")
            self.assertEqual((result.returncode, result.stdout), (0, "\n"))

    def test_compound_whitespace_and_missing_command_use_safe_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp)
            for command in ("pnpm check:full && echo unsafe", " pnpm check:full", None):
                with self.subTest(command=command):
                    result = self.call(path, command=command)
                    self.assertEqual((result.returncode, result.stdout), (0, "macos\n"))

    def test_missing_project_is_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.call(self.write(tmp), project="other", command="pnpm check:full")
            self.assertEqual((result.returncode, result.stdout), (10, ""))

    def test_invalid_or_missing_policy_exits_74(self):
        invalid = [
            None,
            [],
            {"version": True, "projects": {}},
            {"version": 2, "projects": {}},
            {"version": 1, "projects": []},
            {"version": 1, "projects": {"shiftread": []}},
            {"version": 1, "projects": {"shiftread": {"commands": {}}}},
            {"version": 1, "projects": {"shiftread": {
                "default_needs": [], "commands": {}}}},
            {"version": 1, "projects": {"shiftread": {
                "default_needs": ["bad cap"], "commands": {}}}},
            {"version": 1, "projects": {"shiftread": {
                "default_needs": [1], "commands": {}}}},
            {"version": 1, "projects": {"shiftread": {
                "default_needs": ["macos"], "commands": []}}},
            {"version": 1, "projects": {"shiftread": {
                "default_needs": ["macos"], "commands": {"x": "linux"}}}},
            {"version": 1, "projects": {"shiftread": {
                "default_needs": ["macos"], "commands": {"": []}}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            self.assertEqual(self.call(missing).returncode, 74)
            malformed = self.write(tmp, text="{")
            self.assertEqual(self.call(malformed).returncode, 74)
            duplicate = self.write(tmp, text='{"version":1,"version":1,"projects":{}}')
            self.assertEqual(self.call(duplicate).returncode, 74)
            for value in invalid:
                with self.subTest(value=value):
                    self.assertEqual(self.call(self.write(tmp, value=value)).returncode, 74)


if __name__ == "__main__":
    unittest.main()
