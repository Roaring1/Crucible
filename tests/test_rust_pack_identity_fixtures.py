"""Shared Python/Rust fixtures for the isolated GTNH pack-name parser pilot."""

from pathlib import Path

from crucible.importers.pack_update import identify_gtnh_name


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "rust"
    / "crucible-core"
    / "fixtures"
    / "pack-name-cases.tsv"
)


def test_python_gtnh_name_parser_matches_shared_fixture():
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

    assert rows, "shared Rust/Python identity fixture must not be empty"
    for line_number, raw, expected_version, expected_variant in rows:
        parsed = identify_gtnh_name(raw)
        if expected_version == "-":
            assert parsed is None, f"fixture line {line_number}: {raw!r}"
        else:
            assert parsed is not None, f"fixture line {line_number}: {raw!r}"
            actual_version, actual_variant = parsed
            assert actual_version == expected_version, (
                f"fixture line {line_number}: {raw!r}"
            )
            assert actual_variant == expected_variant, (
                f"fixture line {line_number}: {raw!r}"
            )


def test_python_only_unicode_version_name_remains_supported():
    parsed = identify_gtnh_name("GT_New_Horizons_٢.٨.٤_Server_Java_8.zip")
    assert parsed == ("٢.٨.٤", "java8")


def test_python_only_unicode_regex_features_remain_supported():
    # Python re.IGNORECASE folds U+017F LONG S to ASCII 's'.
    assert identify_gtnh_name("GT_New_Horizonſ_2.8.4_Server_Java_8.zip") == (
        "2.8.4",
        "java8",
    )

    # Python's Unicode-aware \\d accepts non-ASCII decimal digits here too.
    assert identify_gtnh_name("GT_New_Horizons_2.8.4_Server_Java_17-٢٥.zip") == (
        "2.8.4",
        "java17_2X",
    )


def test_python_only_unicode_i_casefold_and_strip_behavior_remains_supported():
    # Python re.IGNORECASE folds dotted/dotless I to ASCII "i" in Horizons.
    assert identify_gtnh_name("GT_New_Horİzons_2.8.4_Server_Java_8.zip") == (
        "2.8.4",
        "java8",
    )
    assert identify_gtnh_name("GT_New_Horızons_2.8.4_Server_Java_8.zip") == (
        "2.8.4",
        "java8",
    )

    # Python str.strip() removes U+001C around the captured version.
    assert identify_gtnh_name(
        "GT_New_Horizons_\x1c2.8.4\x1c_Server_Java_8"
    ) == ("2.8.4", "java8")
