"""
tests/test_world_stats_thread_guard.py

Regression test for a real crash reported after v0.6.7:

    QThread: Destroyed while thread '' is still running
    Aborted (core dumped)

Root cause: WorldTab._start_stats_worker() unconditionally created a new
QThread and assigned it to self._stats_thread, even when a previous
world-size scan's QThread was still alive (isRunning() True). load(), the
Refresh button, and post-backup/swap/reset/wipe refreshes can all reach
_start_stats_worker() in quick succession (e.g. clicking Refresh twice, or
a completed operation refreshing the tab while the tab-load scan was still
running on a large GTNH-scale world), so dropping the last Python reference
to a still-running QThread was reachable by ordinary use, not just a rare
race.

Fix: _start_stats_worker() now queues the request in self._stats_pending
and returns early if self._stats_thread is still running; the queued
request is only started from _stats_thread_finished(), which only runs
after the QThread's own `finished` signal has fired (i.e. after it has
actually stopped).

This is a static/textual guard (matching the style of
test_thread_lifecycle_safety.py) so it does not require PyQt6 or a live
QApplication in the test environment.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORLD_TAB = REPO_ROOT / "crucible" / "ui" / "tabs" / "world_tab.py"


class WorldStatsThreadGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.src = WORLD_TAB.read_text(encoding="utf-8")

    def test_start_stats_worker_never_overwrites_a_running_thread(self) -> None:
        self.assertIn(
            "if self._stats_thread is not None and self._stats_thread.isRunning():",
            self.src,
        )
        self.assertIn("self._stats_pending = (world_root, pre_swap_dirs)", self.src)

    def test_finished_handler_resumes_the_queued_refresh(self) -> None:
        self.assertIn("if self._stats_pending is not None:", self.src)
        self.assertIn(
            "world_root, pre_swap_dirs = self._stats_pending",
            self.src,
        )

    def test_guard_appears_before_any_new_thread_is_constructed(self) -> None:
        guard_index = self.src.index(
            "if self._stats_thread is not None and self._stats_thread.isRunning():"
        )
        start_worker_index = self.src.index("def _start_stats_worker(")
        self.assertGreater(guard_index, start_worker_index)
        # The next QThread() construction after _start_stats_worker's def
        # must come AFTER the guard, not before it.
        thread_ctor_index = self.src.index("thread = QThread()", start_worker_index)
        self.assertLess(guard_index, thread_ctor_index)


if __name__ == "__main__":
    unittest.main()
