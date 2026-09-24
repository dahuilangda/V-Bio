"""Tests for the shared MSA cache key (msa_<md5>)."""

from __future__ import annotations

from pathlib import Path

from core.prepare_inputs import (
    _cached_msa_matches_query,
    _sequence_cache_path,
    _sanitize_sequence_for_msa,
)

QUERY = "ACDEFGHIKLMNPQRSTVWY"


def test_cache_key_matches_shared_scheme() -> None:
    import hashlib

    path = _sequence_cache_path(_sanitize_sequence_for_msa(QUERY)[0], Path("/cache"))
    digest = hashlib.md5(QUERY.encode("utf-8")).hexdigest()
    assert path.name == f"msa_{digest}.a3m"


def test_cache_validator_accepts_matching_alignment(tmp_path: Path) -> None:
    a3m = tmp_path / "msa_x.a3m"
    # 小写字母是插入位：剥离后长度必须等于查询序列
    row = QUERY[:3] + "q" + QUERY[3:9] + "z" + QUERY[9:]
    a3m.write_text(f"#1\t{len(QUERY)}\n>101query\n{row}\n")
    assert _cached_msa_matches_query(a3m, QUERY) is True


def test_cache_validator_rejects_mismatched_alignment(tmp_path: Path) -> None:
    a3m = tmp_path / "msa_x.a3m"
    a3m.write_text(">101query\nACDEFG\n")
    assert _cached_msa_matches_query(a3m, QUERY) is False


def test_cache_validator_treats_missing_file_as_miss(tmp_path: Path) -> None:
    assert _cached_msa_matches_query(tmp_path / "absent.a3m", QUERY) is False
