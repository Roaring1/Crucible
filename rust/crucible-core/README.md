# crucible-core

This crate is an isolated, dependency-free Rust parity pilot. It is not linked
into Crucible's Python application and has no filesystem, process, Qt, or
network effects.

The first primitives are the GTNH release-version parser/comparator and the
pure server-pack name parser. Their shared fixture files are consumed by Rust
unit tests and Python pytest reference tests:

- `fixtures/pack-version-cases.tsv`
- `fixtures/pack-name-cases.tsv`

The version parser accepts ASCII decimal components only and uses Rust's
`str::trim()`. The server-name parser also requires an ASCII first digit after
`17-` when identifying the Java 17+ variant. Python's existing regex accepts
Unicode decimal digits via `\d`; Python `str.strip()` also removes U+001C..U+001F
controls that Rust's `trim()` does not remove. These differences are explicit
in Rust-only tests and Python-only regression tests. Python remains
authoritative; do not use either Rust parser as a production decision source
until the differences are resolved or a verified Python fallback is designed.

Decimal components are stored as canonical strings and compared by length
then lexicographically. This avoids fixed-width integer overflow and accepts
the 4,301-digit test case without a big-integer dependency. This is a known
difference from the Python reference: CPython 3.11+ normally caps decimal
`int()` conversions at 4,300 digits, although the runtime limit is configurable
and may be disabled. These pilots make no performance claim; future work
should benchmark real workflows before prioritizing optimization.

A small dependency-free microbenchmark compares these exact fixture inputs in Rust and the unchanged Python reference. It is useful for parser-only context, not an application speedup claim. Run it on an otherwise idle machine with the same release build and Python environment:

```sh
python scripts/benchmark_pack_parsers.py
```

The harness runs 15 timed rounds after warm-up and reports per-input p50/p95 nanoseconds for version parsing, channel classification, and server-pack name identification. The Rust side uses `cargo bench` (release profile); the Python side uses `perf_counter_ns`. This is a quick exploratory harness, not a statistically rigorous benchmark: its rounds share one process, and with 15 samples p95 is effectively the upper tail. For decision-quality Python measurements, use [pyperf](https://pyperf.readthedocs.io/en/stable/run_benchmark.html), which calibrates loops, uses multiple worker processes, checks instability, and records metadata. Record the CPU model, OS, Rust compiler, Python version/build, and power profile with any results. Run at least three complete invocations and compare medians; do not compare CI runners to the owner's desktop or extrapolate these microbenchmarks to ZIP, filesystem, tmux, Qt, or server performance.

Run from the repository root:

```sh
cargo fmt --manifest-path rust/crucible-core/Cargo.toml -- --check
cargo test --manifest-path rust/crucible-core/Cargo.toml --locked
cargo +1.85.0 test --manifest-path rust/crucible-core/Cargo.toml --locked
cargo clippy --manifest-path rust/crucible-core/Cargo.toml --locked --all-targets -- -D warnings
```
