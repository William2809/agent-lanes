"""Brief: fixture waits must tolerate host load without changing product deadlines."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LoadToleranceTests(unittest.TestCase):
    def test_fixture_waits_allow_loaded_host(self):
        # The brief flags limits below 15 s. Check that test-harness policy,
        # independent of the tools and their configured product deadlines.
        for folder in ('tests', 'bin', 'skills'):
            for path in sorted((ROOT / folder).rglob('*.py')):
                if not (path.name.startswith('test') or path.name == 'workflow_fixture.py'):
                    continue
                if 'ui-shots' in path.parts:
                    continue
                tree = ast.parse(path.read_text())
                intentional = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.With) and any(
                        isinstance(item.context_expr, ast.Call)
                        and isinstance(item.context_expr.func, ast.Attribute)
                        and item.context_expr.func.attr == 'assertRaises'
                        and any(isinstance(arg, ast.Attribute) and arg.attr == 'TimeoutExpired'
                                for arg in item.context_expr.args)
                        for item in node.items
                    ):
                        intentional.update(ast.walk(node))
                for node in ast.walk(tree):
                    value = None
                    if isinstance(node, ast.Call):
                        name = getattr(node.func, 'attr', '')
                        # diagnostic(timeout=...) sets the product deadline.
                        if name != 'diagnostic' and node not in intentional:
                            for arg in node.keywords:
                                if arg.arg == 'timeout' and isinstance(arg.value, ast.Constant):
                                    value = arg.value.value
                        if name == 'assertLess' and len(node.args) == 2:
                            lhs, rhs = node.args
                            if isinstance(lhs, ast.BinOp) and isinstance(lhs.op, ast.Sub) and isinstance(rhs, ast.Constant):
                                value = rhs.value
                    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                        lhs, rhs = node.left, node.right
                        if isinstance(lhs, ast.Call) and getattr(lhs.func, 'attr', '') == 'monotonic' and isinstance(rhs, ast.Constant):
                            value = rhs.value
                    if isinstance(value, (int, float)):
                        with self.subTest(path=str(path.relative_to(ROOT)), line=node.lineno):
                            self.assertGreaterEqual(value, 15, 'short fixture wait can fail under host load')
