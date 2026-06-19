"""
crucible/ui/tabs/system_tab.py

"System" tab - at-a-glance CPU and memory use for THIS server versus the
Crucible app itself, so you can answer "how much is my server using?" without
hunting through Activity Monitor / Mission Control / Task Manager.

Numbers match what those tools report (resident memory + CPU%%). The server's
JVM is tagged with -Dcrucible.session=<name> so it is easy to spot there too.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QFrame, QProgressBar, QGridLayout,
)
from PyQt6.QtCore import QTimer

from ...process.resource_monitor import ResourceSampler, system_memory_mb
from .. import theme

_POLL_MS = 2000


class SystemTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance = None
        self._sampler = ResourceSampler()
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._refresh)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        head = QLabel("System usage")
        head.setStyleSheet(f"font-size:15px; font-weight:700; color:{theme.TEXT};")
        lay.addWidget(head)

        sub = QLabel(
            "Live CPU and memory for this server and the Crucible app. In "
            "Activity Monitor / Mission Control / htop, the server JVM is "
            "labeled with \u201ccrucible.session=<name>\u201d.")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        lay.addWidget(sub)

        self._server_card = self._make_card("This server")
        self._gui_card = self._make_card("Crucible app (GUI)")
        lay.addWidget(self._server_card["frame"])
        lay.addWidget(self._gui_card["frame"])

        mframe = QFrame()
        mframe.setStyleSheet(f"background:{theme.SURFACE0}; border-radius:6px;")
        mbox = QVBoxLayout(mframe)
        mbox.setContentsMargins(14, 12, 14, 12)
        title = QLabel("Whole machine memory")
        title.setStyleSheet(f"color:{theme.TEXT}; font-weight:600; font-size:12px;")
        mbox.addWidget(title)
        self._mem_bar = QProgressBar()
        self._mem_bar.setTextVisible(True)
        mbox.addWidget(self._mem_bar)
        lay.addWidget(mframe)
        lay.addStretch()

    def _make_card(self, title: str) -> dict:
        frame = QFrame()
        frame.setStyleSheet(f"background:{theme.SURFACE0}; border-radius:6px;")
        grid = QGridLayout(frame)
        grid.setContentsMargins(14, 12, 14, 12)
        t = QLabel(title)
        t.setStyleSheet(f"color:{theme.TEXT}; font-weight:600; font-size:13px;")
        grid.addWidget(t, 0, 0, 1, 2)
        cpu_l = QLabel("CPU")
        cpu_l.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        mem_l = QLabel("Memory")
        mem_l.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        cpu_v = QLabel("\u2014")
        cpu_v.setStyleSheet(f"color:{theme.ACCENT}; font-size:18px; font-weight:700;")
        mem_v = QLabel("\u2014")
        mem_v.setStyleSheet(f"color:{theme.ACCENT}; font-size:18px; font-weight:700;")
        grid.addWidget(cpu_l, 1, 0)
        grid.addWidget(mem_l, 1, 1)
        grid.addWidget(cpu_v, 2, 0)
        grid.addWidget(mem_v, 2, 1)
        pid_v = QLabel("")
        pid_v.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:10px;")
        grid.addWidget(pid_v, 3, 0, 1, 2)
        return {"frame": frame, "cpu": cpu_v, "mem": mem_v, "pid": pid_v}

    def load(self, instance) -> None:
        self._instance = instance
        self._refresh()

    def showEvent(self, e):
        super().showEvent(e)
        self._timer.start()
        self._refresh()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def _refresh(self) -> None:
        inst = self._instance
        server_pids = self._sampler.find_instance_pids(inst) if inst else []
        gui_pid = self._sampler.gui_pid()
        stats = self._sampler.sample_pids(server_pids + [gui_pid])

        if server_pids:
            s_cpu = sum(stats[p].cpu_pct for p in server_pids if p in stats)
            s_mem = sum(stats[p].rss_mb for p in server_pids if p in stats)
            self._server_card["cpu"].setText(f"{s_cpu:.0f}%")
            self._server_card["mem"].setText(self._fmt_mb(s_mem))
            self._server_card["pid"].setText(
                "PID " + ", ".join(str(p) for p in server_pids))
        else:
            self._server_card["cpu"].setText("\u2014")
            self._server_card["mem"].setText("offline")
            self._server_card["pid"].setText("Server not running")

        g = stats.get(gui_pid)
        if g:
            self._gui_card["cpu"].setText(f"{g.cpu_pct:.0f}%")
            self._gui_card["mem"].setText(self._fmt_mb(g.rss_mb))
            self._gui_card["pid"].setText(f"PID {gui_pid}")

        used, total = system_memory_mb()
        if total > 0:
            self._mem_bar.setMaximum(int(total))
            self._mem_bar.setValue(int(used))
            self._mem_bar.setFormat(
                f"{self._fmt_mb(used)} / {self._fmt_mb(total)}")
        else:
            self._mem_bar.setFormat("unavailable")

    @staticmethod
    def _fmt_mb(mb: float) -> str:
        if mb >= 1024:
            return f"{mb / 1024:.1f} GB"
        return f"{mb:.0f} MB"
