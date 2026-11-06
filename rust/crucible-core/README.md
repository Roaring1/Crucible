# crucible-core

This crate is an isolated, dependency-free Rust parity pilot. It is not linked
into Crucible's Python application and has no filesystem, process, Qt, or
network effects.

The first primitive is the GTNH release-version parser/comparator. Its shared
fixture file is consumed by Rust unit tests and a Python pytest test:
`fixtures/pack-version-cases.tsv`.

The Rust parser deliberately accepts ASCII decimal digits only. Python's
existing regular expression accepts Unicode decimal digits as well. That
difference is recorded rather than hidden: Python remains authoritative, and
the Rust parser must not become a production decision source until this
compatibility edge is resolved or a Python fallback is designed.

Decimal components are stored as canonical strings and compared by length
then lexicographically. This preserves arbitrary-length ordering without a
big-integer dependency. The first pilot makes no performance claim; future
work should benchmark real workflows before prioritizing optimization.

Run from the repository root:

```sh
cargo fmt --manifest-path rust/crucible-core/Cargo.toml -- --check
cargo test --manifest-path rust/crucible-core/Cargo.toml --locked
cargo clippy --manifest-path rust/crucible-core/Cargo.toml --locked --all-targets -- -D warnings
```
