"""
tests/test_qt_slot_contracts.py

Static (no-PyQt6-runtime-required) regression guard for a real crash class:
a @pyqtSlot(...) decorator whose declared argument types don't match the
number of parameters the decorated method actually accepts.

PyQt6 raises a runtime TypeError ("decorated slot has no signature
 compatible with <Signal>[...]") when a slot's declared arity doesn't match
the signal it's connected to. This class of bug (a decorator copy-pasted
from a neighboring method) is invisible to plain unit tests because it only
fails inside a live QApplication/connect() call, so we catch it statically
by parsing the source with ast and comparing decorator arity to the actual
function signature for every @pyqtSlot(...)-decorated method in the package.
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


def _pyqtslot_decorator_arity(dec: ast.expr) -> int | None:
    """Return the number of type args passed to @pyqtSlot(...), or None if
    this decorator isn't a pyqtSlot call (e.g. bare @pyqtSlot with no call,
    or an unrelated decorator)."""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if name != "pyqtSlot":
        return None
    # PyQt6 allows a trailing result= / name= keyword; only count positional
    # type arguments toward arity.
    return len(dec.args)


def _required_and_total_param_count(fn: ast.FunctionDef) -> tuple[int, int]:
    """Return (min_required, max_total) parameter counts, excluding self/cls."""
    args = fn.args
    positional = args.args
    if positional and positional[0].arg in ("self", "cls"):
        positional = positional[1:]
    total = len(positional) + len(args.kwonlyargs)
    num_defaults = len(args.defaults)
    required = len(positional) - num_defaults
    required = max(required, 0)
    return required, total


class PyQtSlotArityContractTests(unittest.TestCase):
    def test_pyqtslot_decorator_arity_matches_method_signature(self) -> None:
        """
        Every @pyqtSlot(<types>) decorator's argument count must be within
        the decorated method's [required, total] parameter range (excluding
        self). A mismatch here is exactly the bug class that crashed
        Watchdog.unwatch: it was decorated @pyqtSlot(object, bool) (2 types)
        but only accepts one parameter (instance_id), which PyQt6 rejects at
        connect()-time with a TypeError.
        """
        violations: list[str] = []
        for path in _iter_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    arity = _pyqtslot_decorator_arity(dec)
                    if arity is None:
                        continue
                    required, total = _required_and_total_param_count(node)
                    if not (required <= arity <= total):
                        violations.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                            f"{node.name} decorated @pyqtSlot with {arity} type(s) "
                            f"but accepts {required}..{total} parameter(s) "
                            f"(excluding self)"
                        )
        self.assertEqual(
            violations,
            [],
            "pyqtSlot decorator arity mismatch(es) found:\n" + "\n".join(violations),
        )

    def test_watchdog_unwatch_slot_matches_str_signal(self) -> None:
        """
        Named regression test for the exact reported crash: Watchdog.unwatch
        must be decorated @pyqtSlot(str) to match InstancePanel's
        watchdog_unwatch_requested = pyqtSignal(str) it's connected to.
        """
        path = CRUCIBLE_SRC / "process" / "watchdog.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "unwatch":
                found = True
                arities = [
                    _pyqtslot_decorator_arity(dec) for dec in node.decorator_list
                ]
                arities = [a for a in arities if a is not None]
                self.assertEqual(
                    arities,
                    [1],
                    f"Watchdog.unwatch must be decorated @pyqtSlot(str) (1 type arg), "
                    f"found decorator arity {arities}",
                )
        self.assertTrue(found, "Could not locate Watchdog.unwatch to check")


if __name__ == "__main__":
    unittest.main()
