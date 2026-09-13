"""
tests/test_startup_patterns.py

Qt-free tests for crucible/process/startup_patterns.py -- specifically the
"stuck starting" escape hatch. Kept out of PyQt6-backed instance_panel.py so
it's directly unit-testable (matching RE_SERVER_DONE's own rationale for
living in this module).
"""

from __future__ import annotations

import unittest

from crucible.process.startup_patterns import stale_starting_should_promote


class StaleStartingPromotionTests(unittest.TestCase):
    """A live GTNH server sat stuck showing "STARTING..." for 20+ hours
    despite running fine the whole time: both the log-watcher's "Done (Xs)!"
    detection and the pane-tail fallback can miss that line (e.g. several
    rapid restarts rotating the log faster than a read keeps up), and the
    periodic health check is deliberately barred from overriding "starting"
    on its own. Without an escape hatch, a missed detection is permanent for
    as long as the instance stays selected.
    """

    def test_does_not_promote_before_the_threshold(self):
        self.assertFalse(
            stale_starting_should_promote(
                299.0, java_foreground=True, threshold_s=300.0
            )
        )

    def test_promotes_once_threshold_is_reached_with_java_confirmed_up(self):
        self.assertTrue(
            stale_starting_should_promote(
                300.0, java_foreground=True, threshold_s=300.0
            )
        )

    def test_never_promotes_when_java_is_not_confirmed_foreground(self):
        """False (wrapper script/reboot countdown, not java) or None
        (uncertain tmux probe) must never trigger this -- only a *confirmed*
        live java process is grounds for assuming "Done!" was simply missed."""
        for java_foreground in (False, None):
            with self.subTest(java_foreground=java_foreground):
                self.assertFalse(
                    stale_starting_should_promote(
                        10_000.0, java_foreground=java_foreground, threshold_s=300.0
                    )
                )

    def test_default_threshold_is_generous(self):
        """No real modpack takes minutes upon minutes just to print "Done!"
        -- the default must be long enough that this never fires on a
        genuinely slow-but-still-booting server."""
        self.assertFalse(
            stale_starting_should_promote(60.0, java_foreground=True)
        )
        self.assertTrue(
            stale_starting_should_promote(10_000.0, java_foreground=True)
        )


if __name__ == "__main__":
    unittest.main()
