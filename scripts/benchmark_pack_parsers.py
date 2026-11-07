#!/usr/bin/env python3
"""Compare the isolated Rust parser pilot with its unchanged Python references.

Run from any directory with:
    python scripts/benchmark_pack_parsers.py

This is a microbenchmark of parser CPU only. It does not measure application
startup, Python/Rust bridge overhead, filesystem access, ZIP parsing, update
planning, or end-to-end server management.
"""

from __future__ import annotations

import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crucible.importers.pack_update import (  # noqa: E402
    channel_of,
    identify_gtnh_name,
    parse_pack_version,
)

FIXTURES = ROOT / "rust" / "crucible-core" / "fixtures"
ROUNDS = 15
ITERATIONS_PER_ROUND = 2_000
WARMUP_ITERATIONS = 100


def load_inputs(path: Path) -> list[str]:
    rows = [
        line.split("\t", 1)[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    if not rows:
        raise RuntimeError(f"fixture has no cases: {path}")
    return rows


def percentile(sorted_values: list[int], numerator: int, denominator: int) -> int:
    index = (len(sorted_values) * numerator + denominator - 1) // denominator - 1
    return sorted_values[max(0, min(index, len(sorted_values) - 1))]


def measure(label: str, inputs: list[str], parser: Callable[[str], object]) -> None:
    for _ in range(WARMUP_ITERATIONS):
        for value in inputs:
            parser(value)

    samples: list[int] = []
    for _ in range(ROUNDS):
        started = time.perf_counter_ns()
        for _ in range(ITERATIONS_PER_ROUND):
            for value in inputs:
                parser(value)
        operations = ITERATIONS_PER_ROUND * len(inputs)
        samples.append((time.perf_counter_ns() - started) // operations)

    ordered = sorted(samples)
    print(
        f"{label}: n={len(inputs)} "
        f"p50={statistics.median(ordered):.0f} ns/op "
        f"p95={percentile(ordered, 95, 100)} ns/op "
        f"rounds={ROUNDS} iterations/round={ITERATIONS_PER_ROUND}"
    )


def main() -> int:
    version_inputs = load_inputs(FIXTURES / "pack-version-cases.tsv")
    name_inputs = load_inputs(FIXTURES / "pack-name-cases.tsv")

    try:
        rustc = subprocess.run(
            ["rustc", "--version"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Rust toolchain unavailable: {exc}") from exc

    cpu_model = platform.processor() or "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                cpu_model = line.partition(":")[2].strip()
                break
    except OSError:
        pass

    print(f"Repository: {ROOT}")
    print(f"OS: {platform.platform()}")
    print(f"Architecture: {platform.machine()}")
    print(f"CPU: {cpu_model}")
    print(f"Python: {platform.python_version()} ({sys.implementation.name})")
    print(f"Rust toolchain: {rustc}")
    print("Rust release benchmark follows; first build may add compile time.")
    subprocess.run(
        [
            "cargo",
            "bench",
            "--manifest-path",
            str(ROOT / "rust" / "crucible-core" / "Cargo.toml"),
            "--bench",
            "parser_micro",
            "--locked",
        ],
        cwd=ROOT,
        check=True,
    )

    print("Python reference benchmark follows; timings are parser-only.")
    measure("parse_pack_version (Python)", version_inputs, parse_pack_version)
    measure("channel_of (Python)", version_inputs, channel_of)
    measure("identify_gtnh_name (Python)", name_inputs, identify_gtnh_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
