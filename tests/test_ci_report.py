"""Unit tests for bounded, redacted GitHub Actions diagnostics."""
from scripts.ci_report import _job_failed_steps, failure_context, redact


def test_redact_masks_sensitive_environment_assignments():
    line = "SUPPLEMENTARIES_SAFE_PASSWORD=secretvalue ghp_abcdefghijklmnopqrstuvwxyz012345"
    cleaned = redact(line)
    assert "secretvalue" not in cleaned
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in cleaned
    assert "[REDACTED]" in cleaned
    assert "[REDACTED_GITHUB_TOKEN]" in cleaned


def test_failure_context_shows_error_and_is_bounded():
    lines = [f"line {i}" for i in range(500)]
    lines[247] = "AssertionError: expected value"
    output = failure_context(lines, radius=1, max_lines=8)
    assert "AssertionError: expected value" in output
    assert "line 246" in output
    assert "line 248" in output
    assert len(output.splitlines()) <= 10
    assert len(output) < 2000


def test_failure_context_redacts_values_in_tracebacks():
    output = failure_context([
        "starting",
        "Traceback (most recent call last):",
        "PASSWORD=never-show-this",
        "ValueError: broken",
    ])
    assert "never-show-this" not in output
    assert "[REDACTED]" in output


def test_failed_step_summary_only_lists_failed_steps():
    job = {
        "steps": [
            {"name": "Checkout", "number": 1, "conclusion": "success"},
            {"name": "Install dependencies", "number": 2, "conclusion": "success"},
            {"name": "Run tests", "number": 3, "conclusion": "failure"},
            {"name": "Upload log", "number": 4, "conclusion": "skipped"},
        ]
    }
    assert _job_failed_steps(job) == ["Run tests (step 3)"]
