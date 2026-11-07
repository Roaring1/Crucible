# crucible-core

This crate is an isolated, dependency-free Rust parity pilot. It is not linked
into Crucible's Python application and has no filesystem, process, Qt, or
network effects.

The first primitives are the GTNH release-version parser/comparator and the
pure server-pack name parser. Their shared fixture files are consumed by Rust
unit tests and Python pytest reference tests:

- `fixtures/pack-version-cases.tsv`
- `fixtures/pack-name-cases.tsv`

The Rust parser deliberately accepts ASCII decimal digits only and uses Rust's
`str::trim()`. Python's existing expression accepts Unicode decimal digits via
`\d` and `str.strip()` also removes U+001C..U+001F controls that Rust's
`trim()` does not remove. Both differences are explicit in Rust-only tests and
a Python-only regression test. Python remains authoritative; do not use the
Rust parser as a production decision source until both edges are resolved or
a verified Python fallback is designed.

Decimal components are stored as canonical strings and compared by length
then lexicographically. This preserves arbitrary-length ordering without a
big-integer dependency. The first pilot makes no performance claim; future
work should benchmark real workflows before prioritizing optimization.

Run from the repository root:

```sh
cargo fmt --manifest-path rust/crucible-core/Cargo.toml -- --check
cargo test --manifest-path rust/crucible-core/Cargo.toml --locked
cargo +1.85.0 test --manifest-path rust/crucible-core/Cargo.toml --locked
cargo clippy --manifest-path rust/crucible-core/Cargo.toml --locked --all-targets -- -D warnings
```
