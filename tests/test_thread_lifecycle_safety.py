"""
tests/test_thread_lifecycle_safety.py

Static regression guard for a real crash:

    QThread: Destroyed while thread '' is still running
    Aborted (core dumped)

Root cause: code called `some_thread.wait(<timeout>)` as a bare statement
(discarding the bool it returns) and then unconditionally set the last
Python reference to that QThread to None in the same block. wait(timeout)
can legitimately time out while the thread is still running -- when that
happens, dropping the reference destroys a QThread object while its OS
thread is still alive, which PyQt6 treats as fatal.

This test statically scans the crucible package for that exact
discarded-return-value-then-None anti-pattern so it can't silently
reappear (e.g. via copy/paste into a new lifecycle method), without
requiring PyQt6 or a live QApplication in the test environment.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CRUCIBLE_SRC = REPO_ROOT / "crucible"


def _iter_python_files():
    for path in sorted(CRUCIBLE_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _target_name(expr: ast.expr) -> str | None:
    """Return a stable string key for `self._foo` / `foo` style targets, or
    None if the expression isn't a simple attribute/name chain we can compare."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _target_name(expr.value)
        return None if base is None else f"{base}.{expr.attr}"
    return None


def _find_unguarded_wait_then_none(body: list[ast.stmt], path: Path, violations: list[str]) -> None:
    for i, stmt in enumerate(body):
        # Bare `<target>.wait(<at least one arg>)` expression statement --
        # i.e. its boolean return value is discarded.
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == "wait"
            and len(stmt.value.args) >= 1
        ):
            target = _target_name(stmt.value.func.value)
            if target is None:
                continue
            # Look ahead in the same block for an unconditional `<target> = None`.
            for later in body[i + 1 :]:
                if (
                    isinstance(later, ast.Assign)
                    and len(later.targets) == 1
                    and _target_name(later.targets[0]) == target
                    and isinstance(later.value, ast.Constant)
                    and later.value.value is None
                ):
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{stmt.lineno}: "
                        f"'{target}.wait(...)' return value is discarded, then "
                        f"'{target} = None' unconditionally follows on line "
                        f"{later.lineno}. A timed-out wait() does not mean the "
                        f"thread stopped -- this can destroy a still-running "
                        f"QThread."
                    )
                    break
        # Recurse into nested blocks (if/try/for/while/with/function bodies).
        for field in ("body", "orelse", "finalbody"):
            nested = getattr(stmt, field, None)
            if isinstance(nested, list):
                _find_unguarded_wait_then_none(nested, path, violations)
        for handler in getattr(stmt, "handlers", []):
            _find_unguarded_wait_then_none(handler.body, path, violations)


class ThreadWaitThenNoneTests(unittest.TestCase):
    def test_no_discarded_wait_result_before_nulling_thread_reference(self) -> None:
        violations: list[str] = []
        for path in _iter_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _find_unguarded_wait_then_none(node.body, path, violations)
        self.assertEqual(
            violations,
            [],
            "Unsafe QThread wait()-then-None pattern(s) found:\n" + "\n".join(violations),
        )

    def test_stop_watcher_and_shutdown_check_wait_result(self) -> None:
        """
        Named regression test for the exact reported crash: InstancePanel
        must check the boolean result of _w_thread.wait(...) and
        _wd_thread.wait(...) before nulling those references.
        """
        path = CRUCIBLE_SRC / "ui" / "instance_panel.py"
        src = path.read_text(encoding="utf-8")
        self.assertIn("if not self._w_thread.wait(2000):", src)
        self.assertIn("if not self._wd_thread.wait(2000):", src)


if __name__ == "__main__":
    unittest.main()
