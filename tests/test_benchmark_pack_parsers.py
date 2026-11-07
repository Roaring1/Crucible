from __future__ import annotations

from pathlib import Path

from scripts.benchmark_pack_parsers import load_inputs, percentile


ROOT = Path(__file__).resolve().parents[1]


def test_benchmark_reads_the_shared_parser_fixtures() -> None:
    versions = load_inputs(ROOT / "rust/crucible-core/fixtures/pack-version-cases.tsv")
    names = load_inputs(ROOT / "rust/crucible-core/fixtures/pack-name-cases.tsv")

    assert len(versions) >= 30
    assert len(names) >= 15
    assert versions[0] == "2.8.4"
    assert names[0].startswith("GT_New_Horizons_")


def test_benchmark_percentile_uses_nearest_rank() -> None:
    samples = list(range(1, 21))

    assert percentile(samples, 50, 100) == 10
    assert percentile(samples, 95, 100) == 19
