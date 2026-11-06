//! Small, dependency-free Rust primitives for Crucible.
//!
//! This crate is not linked into the Python application yet. Its first slice
//! is a parity pilot for GTNH version parsing and ordering.

mod pack_version;

pub use pack_version::{channel_of, parse_pack_version, Channel, PackVersion};
