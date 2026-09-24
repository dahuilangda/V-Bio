"""De novo binder single-sequence MSA seeding (2026-09-19 incident fix).

Design tasks deliberately run de novo peptides single-sequence (no homologs),
but the engine-side resolve_msa falls through to the MSA server on a cache
miss — under GPU contention that fetch waits behind the refine wave itself
(circular hold: refine holds a GPU while waiting for an MSA that needs a
free GPU), burning the 30-min poll timeout per candidate. Seeding a
query-only a3m under the exact engine cache key turns the miss into the
official N_msa=1 contract without touching the server.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.runtime.run_single_prediction as rsp  # noqa: E402


def _norm(seq: str) -> str:
    return "".join(a if a in "ACDEFGHIKLMNPQRSTVWY" else "A"
                   for a in seq.strip().upper())


def test_seeds_query_only_a3m_under_engine_cache_key(tmp_path, monkeypatch):
    monkeypatch.setitem(rsp.MSA_CACHE_CONFIG, "cache_dir", str(tmp_path))
    monkeypatch.setitem(rsp.MSA_CACHE_CONFIG, "enable_cache", True)
    seqs = ["TTFLEYWALLMS", "LNASLYETFLSYWWRLLS"]
    assert rsp._seed_single_sequence_binder_msas(seqs) == 2
    for s in seqs:
        h = hashlib.md5(_norm(s).encode()).hexdigest()
        p = tmp_path / f"msa_{h}_uniref.a3m"
        assert p.exists()
        assert p.read_text() == f">query\n{_norm(s)}\n"


def test_never_overwrites_existing_msa(tmp_path, monkeypatch):
    monkeypatch.setitem(rsp.MSA_CACHE_CONFIG, "cache_dir", str(tmp_path))
    monkeypatch.setitem(rsp.MSA_CACHE_CONFIG, "enable_cache", True)
    seq = "TTFLEYWALLMS"
    rsp._seed_single_sequence_binder_msas([seq])
    h = hashlib.md5(_norm(seq).encode()).hexdigest()
    real = tmp_path / f"msa_{h}_uniref.a3m"
    real.write_text(">q\nTTFLEYWALLMS\n>homolog\nTTFLEYWALLMS\n")
    assert rsp._seed_single_sequence_binder_msas([seq]) == 0
    assert "homolog" in real.read_text()


def test_skips_empty_and_normalizes(tmp_path, monkeypatch):
    monkeypatch.setitem(rsp.MSA_CACHE_CONFIG, "cache_dir", str(tmp_path))
    monkeypatch.setitem(rsp.MSA_CACHE_CONFIG, "enable_cache", True)
    assert rsp._seed_single_sequence_binder_msas(["", "   "]) == 0
    rsp._seed_single_sequence_binder_msas(["TTFL-XU-WALLMS"])
    h = hashlib.md5(_norm("TTFL-XU-WALLMS").encode()).hexdigest()
    assert (tmp_path / f"msa_{h}_uniref.a3m").exists()


def test_disabled_cache_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setitem(rsp.MSA_CACHE_CONFIG, "cache_dir", str(tmp_path))
    monkeypatch.setitem(rsp.MSA_CACHE_CONFIG, "enable_cache", False)
    assert rsp._seed_single_sequence_binder_msas(["TTFLEYWALLMS"]) == 0
    assert list(tmp_path.iterdir()) == []
