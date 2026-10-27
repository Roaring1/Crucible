#!/usr/bin/env python3
"""Read-only GitHub Actions triage for Crucible.

Examples:
    python scripts/ci_report.py
    python scripts/ci_report.py --run-id 40581792706
    GITHUB_TOKEN=... python scripts/ci_report.py --limit 10

Uses only the Python standard library. Public-repository run/job/log APIs work
without authentication; GITHUB_TOKEN (or GH_TOKEN) improves rate limits.
Failure excerpts are redacted and bounded instead of dumping whole job logs.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Any

DEFAULT_REPO = "Roaring1/Crucible"
API = "https://api.github.com"
TIMEOUT_SECONDS = 20
MAX_LOG_BYTES = 25 * 1024 * 1024
MAX_EXCERPT_CHARS = 10_000

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|API[_-]?KEY|ACCESS[_-]?KEY)"
    r"[A-Z0-9_]*\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")
_FAILURE = re.compile(
    r"##\[error\]|\bFAILED\b|\bERROR\b|Traceback \(most recent call last\)|"
    r"\b(?:ImportError|ModuleNotFoundError|AssertionError|SyntaxError|"
    r"PermissionError|TimeoutError|RuntimeError|ValueError|TypeError):|"
    r"Error:|error:|Process completed with exit code",
    re.IGNORECASE,
)


class ReportError(RuntimeError):
    """A request or response from the GitHub API could not be used."""


def redact(text: str) -> str:
    """Redact common credential formats before any log excerpt is displayed."""
    text = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub(r"\1[REDACTED]", text)
    return _GITHUB_TOKEN.sub("[REDACTED_GITHUB_TOKEN]", text)


def _request(url: str, token: str | None = None) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Crucible-CI-Report/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            payload = response.read(MAX_LOG_BYTES + 1)
    except urllib.error.HTTPError as exc:
        details = exc.read(1000).decode("utf-8", "replace")
        if exc.code in (401, 403):
            hint = " Check token permissions or GitHub API rate limits."
        elif exc.code == 404:
            hint = " Verify the repository, run ID, job ID, and Actions log retention."
        else:
            hint = ""
        raise ReportError(f"GitHub returned HTTP {exc.code}.{hint}\n{details}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReportError(f"Could not reach GitHub: {exc}") from exc
    if len(payload) > MAX_LOG_BYTES:
        raise ReportError(f"GitHub response exceeded the {MAX_LOG_BYTES // (1024 * 1024)} MiB safety limit.")
    return payload


def request_json(url: str, token: str | None = None) -> dict[str, Any]:
    try:
        value = json.loads(_request(url, token).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportError(f"GitHub returned invalid JSON for {url}") from exc
    if not isinstance(value, dict):
        raise ReportError(f"GitHub returned an unexpected JSON shape for {url}")
    return value


def _timestamp(value: str | None) -> str:
    if not value:
        return "unknown time"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return value


def _status(run: dict[str, Any]) -> str:
    if run.get("status") != "completed":
        return str(run.get("status") or "unknown")
    return str(run.get("conclusion") or "completed")


def _workflow_runs(repo: str, token: str | None, limit: int) -> list[dict[str, Any]]:
    data = request_json(f"{API}/repos/{repo}/actions/runs?per_page={limit}", token)
    runs = data.get("workflow_runs", [])
    if not isinstance(runs, list):
        raise ReportError("GitHub response did not include a workflow_runs list.")
    return [item for item in runs if isinstance(item, dict)]


def _get_run(repo: str, run_id: int, token: str | None) -> dict[str, Any]:
    return request_json(f"{API}/repos/{repo}/actions/runs/{run_id}", token)


def _jobs(repo: str, run_id: int, token: str | None) -> list[dict[str, Any]]:
    data = request_json(f"{API}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", token)
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        raise ReportError("GitHub response did not include a jobs list.")
    return [item for item in jobs if isinstance(item, dict)]


def failure_context(lines: list[str], *, radius: int = 2, max_lines: int = 80) -> str:
    """Return small, redacted windows around failure markers."""
    selected: set[int] = set()
    for index, line in enumerate(lines):
        if _FAILURE.search(line):
            selected.update(range(max(0, index - radius), min(len(lines), index + radius + 1)))
    if not selected:
        selected.update(range(max(0, len(lines) - min(20, max_lines)), len(lines)))

    ordered = sorted(selected)
    if len(ordered) > max_lines:
        half = max_lines // 2
        ordered = ordered[:half] + ordered[-(max_lines - half):]

    chunks: list[str] = []
    previous = -2
    for index in ordered:
        if index > previous + 1:
            chunks.append("…")
        chunks.append(f"{index + 1:>5}: {redact(lines[index])}")
        previous = index
    result = "\n".join(chunks)
    if len(result) > MAX_EXCERPT_CHARS:
        result = result[:MAX_EXCERPT_CHARS] + "\n…[excerpt truncated]"
    return result


def _job_failed_steps(job: dict[str, Any]) -> list[str]:
    steps = job.get("steps", [])
    return [
        f"{step.get('name', 'unnamed step')} (step {step.get('number', '?')})"
        for step in steps
        if isinstance(step, dict) and step.get("conclusion") == "failure"
    ]


def _job_log_excerpt(repo: str, job_id: int, token: str | None) -> str:
    url = f"{API}/repos/{repo}/actions/jobs/{job_id}/logs"
    try:
        raw = _request(url, token)
    except ReportError as api_error:
        # GitHub's log-archive endpoint can require repository permissions even
        # when basic run metadata is public. Prefer the user's already-authenticated
        # gh CLI rather than asking them to copy tokens or paste logs.
        gh = shutil.which("gh")
        if not gh:
            raise
        try:
            result = subprocess.run(
                [gh, "run", "view", "--job", str(job_id), "--log", "--repo", repo],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
                env={**os.environ, "GH_PAGER": "cat", "PAGER": "cat"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReportError(f"{api_error}\nGitHub CLI log fallback failed: {exc}") from exc
        if result.returncode != 0 or not result.stdout.strip():
            detail = redact((result.stderr or result.stdout or "no output")[-1500:])
            raise ReportError(f"{api_error}\nGitHub CLI log fallback failed: {detail}") from api_error
        return failure_context(result.stdout.splitlines())
    if not raw.startswith(b"PK"):
        return failure_context(raw.decode("utf-8", "replace").splitlines())
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if not names:
                return "GitHub returned an empty job-log archive."
            sections: list[str] = []
            for name in names[:20]:
                content = archive.read(name).decode("utf-8", "replace")
                sections.append(f"--- {name} ---\n{failure_context(content.splitlines())}")
            return "\n\n".join(sections)[:MAX_EXCERPT_CHARS]
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ReportError(f"Could not read the log archive for job {job_id}: {exc}") from exc


def build_report(repo: str, token: str | None, run_id: int | None, limit: int) -> tuple[str, int]:
    runs = _workflow_runs(repo, token, limit)
    if not runs:
        raise ReportError(f"No workflow runs were returned for {repo}.")
    if run_id is not None:
        run = _get_run(repo, run_id, token)
    else:
        run = runs[0]

    actual_repo = run.get("repository", {}).get("full_name") or repo
    lines = [
        f"Repository: {actual_repo}",
        f"Workflow: {run.get('name', 'unknown')}",
        f"Run: #{run.get('run_number', '?')} (ID {run.get('id', run_id or '?')})",
        f"Status: {_status(run)}",
        f"Branch: {run.get('head_branch', 'unknown')}",
        f"Commit: {str(run.get('head_sha', 'unknown'))[:12]}",
        f"Started: {_timestamp(run.get('run_started_at') or run.get('created_at'))}",
        f"Title: {run.get('display_title', run.get('name', 'unknown'))}",
        f"URL: {run.get('html_url', '')}",
    ]
    run_status = _status(run).lower()
    exit_code = 0 if run_status == "success" else (2 if run.get("status") != "completed" else 1)
    if run.get("status") != "completed":
        lines += ["", "The selected run is not complete yet; job logs may still be changing."]
        return "\n".join(lines), exit_code

    jobs = _jobs(actual_repo, int(run.get("id", run_id or 0)), token)
    failed_jobs = [job for job in jobs if job.get("conclusion") not in ("success", "skipped")]
    lines += ["", f"Jobs: {len(jobs)} total; {len(failed_jobs)} failed or incomplete."]
    for job in jobs:
        lines.append(f"  - {job.get('name', 'unknown')}: {_status(job)}")
        for step in _job_failed_steps(job):
            lines.append(f"      failed step: {step}")

    for job in failed_jobs:
        lines += ["", "=" * 72, f"Failure details: {job.get('name', 'unknown')} (job ID {job.get('id', '?')})"]
        if job.get("id") is not None:
            try:
                lines.append(_job_log_excerpt(actual_repo, int(job["id"]), token))
            except ReportError as exc:
                lines.append(f"Could not retrieve log excerpt: {exc}")
    if not failed_jobs and run_status == "success":
        lines += ["", "All jobs succeeded."]
    return "\n".join(lines), exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO),
                        help="owner/repository (default: %(default)s)")
    parser.add_argument("--run-id", type=int, help="inspect this exact Actions run ID")
    parser.add_argument("--limit", type=int, default=10, help="recent run records to request (default: 10)")
    parser.add_argument("--json", action="store_true", help="print recent run metadata as JSON, without log excerpts")
    args = parser.parse_args(argv)
    if "/" not in args.repo or not args.repo.strip("/"):
        parser.error("--repo must be in owner/repository form")
    if args.limit < 1 or args.limit > 100:
        parser.error("--limit must be between 1 and 100")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    try:
        if args.json and args.run_id is None:
            runs = _workflow_runs(args.repo, token, args.limit)
            print(json.dumps([
                {key: run.get(key) for key in (
                    "id", "run_number", "name", "status", "conclusion", "head_branch",
                    "head_sha", "created_at", "display_title", "html_url"
                )}
                for run in runs
            ], indent=2))
            return 0
        report, code = build_report(args.repo, token, args.run_id, args.limit)
        print(report)
        return code
    except ReportError as exc:
        print(f"CI report failed: {exc}", file=sys.stderr)
        print(
            "For a private repository, set GH_TOKEN to a token with Actions read access. "
            "Never paste the token into a command argument or commit it.",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
