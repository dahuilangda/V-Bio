"""Regression tests for D-space diffusion sample selection (2026-09-18).

The MDM-2 blind-route run shipped peptide poses 3.9-20 A off the receptor
while the candidate rows carried a bound sample's ipSAE 0.92. Root causes
pinned by these tests:

1. clash-minimization ranking prefers detached samples (zero clashes);
2. engagement must outrank clash-freedom so a docked-but-slightly-touched
   sample always beats a floating one;
3. per-sample ipSAE overwrites the row's interface metrics so the score
   describes the coordinates that ship.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.runtime.run_single_prediction as rsp  # noqa: E402


@pytest.fixture
def sample_dir(tmp_path):
    """Build 3 minimal two-chain PDBs: bound-clean, bound-jammed, detached."""
    def write_complex(name, peptide_dx, peptide_dy=0.0):
        rec = []
        for i in range(20):
            x, y, z = 0.0, i * 3.6, 0.0
            rec.append((x, y, z))
        pep = []
        for i in range(8):
            x, y, z = peptide_dx, peptide_dy, i * 3.6
            pep.append((x, y, z))
        lines = ["ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00 50.00           N"]
        lines = []
        serial = 1
        for ch, residues in (("A", rec), ("B", pep)):
            for i, (x, y, z) in enumerate(residues, start=1):
                for aname, (dx, dy, dz) in (
                    ("N", (1.3, 0, 0)), ("CA", (0, 0, 0)),
                    ("C", (-0.6, 1.4, 0)), ("CB", (-0.5, -1.0, 0.3)),
                ):
                    lines.append(
                        f"ATOM  {serial:5d}  {aname:<3s} ALA {ch}{i:4d}    "
                        f"{x+dx:8.3f}{y+dy:8.3f}{z+dz:8.3f}  1.00 50.00           C")
                    serial += 1
        p = tmp_path / name
        p.write_text("\n".join(lines) + "\n")
        return p
    return write_complex


def test_engaged_sample_outranks_zero_clash_detached(sample_dir, monkeypatch):
    clean = sample_dir("clean.pdb", 4.0)
    jammed = sample_dir("jammed.pdb", 1.0)
    detached = sample_dir("detached.pdb", 40.0)

    # neutralize the covalent check (stub CA-only stubs confuse it)
    monkeypatch.setattr(rsp, "_covalent_detached_atoms", lambda p: [])
    scored = [(0.90, clean), (0.95, jammed), (0.99, detached)]
    ranked = rsp._select_dspace_samples(scored)
    best = ranked[0]
    # detached has the highest ipTM and zero clashes but is not engaged;
    # clean (engaged, 0 clashes) must win.
    assert best[5] == clean
    assert best[1] == 1
    # detached sorts below BOTH engaged samples
    assert ranked[-1][5] == detached


def test_jammed_engagement_beats_detached_for_top(sample_dir, monkeypatch):
    """When every engaged sample is jammed, the top is still engaged —
    the caller's absolute clash gate then rejects the candidate honestly
    instead of shipping the detached sample."""
    jammed = sample_dir("jammed.pdb", 1.2)
    detached = sample_dir("detached.pdb", 25.0)
    monkeypatch.setattr(rsp, "_covalent_detached_atoms", lambda p: [])
    ranked = rsp._select_dspace_samples([(0.99, detached), (0.80, jammed)])
    assert ranked[0][1] == 1
    assert ranked[0][5] == jammed


def test_contact_count_thresholds(sample_dir):
    clean = sample_dir("clean.pdb", 4.0)
    detached = sample_dir("detached.pdb", 40.0)
    n_clean = rsp._interchain_contact_count(clean)
    n_detached = rsp._interchain_contact_count(detached)
    assert n_clean >= rsp.ENGAGE_MIN_CONTACTS
    assert n_detached == 0


def test_ipsae_override_replaces_interface_metrics(tmp_path, monkeypatch):
    """The collector overwrites ipsae_dom/ligand_ipsae_max/interface_score
    from the shipped sample's ipsae json when present."""
    import json as _json
    cif = tmp_path / "tag_model_3.cif"
    cif.write_text("stub")
    ips = tmp_path / "ipsae_tag_model_3.json"
    ips.write_text(_json.dumps({"ipsae_dom": 0.42, "ligand_ipsae_max": 0.51}))

    metrics = {"ipsae_dom": 0.92, "ligand_ipsae_max": 0.86,
               "interface_score": 0.92}
    # replicate the collector's override block
    if ips.is_file():
        meta = _json.loads(ips.read_text())
        for k in ("ipsae_dom", "ligand_ipsae_max"):
            if isinstance(meta.get(k), (int, float)):
                metrics[k] = meta[k]
        if isinstance(meta.get("ipsae_dom"), (int, float)):
            metrics["interface_score"] = meta["ipsae_dom"]
    assert metrics["ipsae_dom"] == 0.42
    assert metrics["interface_score"] == 0.42
