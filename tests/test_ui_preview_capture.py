"""The offscreen screenshot helper should render real UI without desktop interaction."""
import os
import subprocess
import sys
from pathlib import Path


def test_capture_ui_previews_writes_expected_pngs(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "capture_ui_previews.py"
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONPATH": str(repo),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, str(script), "--output", str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    expected = (
        "main-empty.png",
        "add-server-menu.png",
        "main-selected.png",
        "create-server.png",
        "import-files.png",
        "install-modpack.png",
    )
    for filename in expected:
        path = tmp_path / filename
        assert path.is_file(), filename
        data = path.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n"), filename
        assert len(data) > 1_000, filename
    assert "Captured 6 UI previews" in result.stdout
