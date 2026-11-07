"""Shared fixture parity checks for the isolated Rust parser pilot."""

import sys
from pathlib import Path

import pytest

from crucible.importers.pack_update import channel_of, parse_pack_version


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "rust"
    / "crucible-core"
    / "fixtures"
    / "pack-version-cases.tsv"
)


def test_python_pack_version_parser_matches_shared_fixture():
    rows = []
    for line_number, line in enumerate(
        FIXTURE_PATH.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        assert len(fields) == 3, f"fixture line {line_number} must have three fields"
        rows.append((line_number, fields[0], fields[1], fields[2]))

    assert rows, "shared Rust/Python version fixture must not be empty"
    for line_number, raw, expected_key, expected_channel in rows:
        parsed = parse_pack_version(raw)
        if expected_key == "-":
            assert parsed is None, f"fixture line {line_number}: {raw!r}"
        else:
            assert parsed is not None, f"fixture line {line_number}: {raw!r}"
            major, minor, patch, rank, number = parsed
            actual_key = f"{major}.{minor}.{patch}|{rank}|{number}"
            assert actual_key == expected_key, f"fixture line {line_number}: {raw!r}"
        assert channel_of(raw) == expected_channel, (
            f"fixture line {line_number}: {raw!r}"
        )

def test_python_only_unicode_version_inputs_remain_supported():
    # Python's existing regex uses Unicode-aware \d and str.strip().
    # The isolated Rust pilot intentionally does not match these yet.
    parsed = parse_pack_version("٢.٨.٤")
    assert parsed == (2, 8, 4, 3, 0)
    assert channel_of("٢.٨.٤") == "stable"

    prerelease_number = "2.8.4-rc-٢"
    assert parse_pack_version(prerelease_number) == (2, 8, 4, 2, 2)
    assert channel_of(prerelease_number) == "rc"

    control_whitespace = "\x1c2.8.4\x1c"
    parsed = parse_pack_version(control_whitespace)
    assert parsed == (2, 8, 4, 3, 0)
    assert channel_of(control_whitespace) == "stable"


def test_python_reference_retains_cpython_decimal_conversion_limit():
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    if get_limit is None:
        pytest.skip("This interpreter does not expose CPython's decimal conversion limit.")
    limit = get_limit()
    if limit == 0:
        pytest.skip("The runtime explicitly disabled the decimal conversion limit.")
    raw = f"{'9' * (limit + 1)}.1.0"
    with pytest.raises(ValueError, match="Exceeds the limit"):
        parse_pack_version(raw)
