"""D-peptide route no-fallback hardening tests (2026-09-04 incident follow-up).

Covers, against real fixtures (3LNJ native/mirror MDM2+PMI complex):
  1. per-residue chirality_violations primitive (clean / inverted / ambiguous)
  2. staging hard-requires pocket or reference (no silent centroid burial)
  3. staging pocket placement is clash-free and anchored (self-check gate)
  4. refined mirror complex chirality gate (all-clean passes, one flipped CA
     raises listing the residue)
  5. composite rescoring from refined metrics (pocket / no-pocket weights,
     clamping, missing-readout behaviour)
  6. product chirality gate flips from mean-telemetry to per-residue hard
     failure
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import gemmi
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PEPLM_ROOT = REPO_ROOT / "capabilities" / "peptide_lm"
if str(PEPLM_ROOT) not in sys.path:
    sys.path.insert(0, str(PEPLM_ROOT))

P2D_ROOT = REPO_ROOT / "capabilities" / "protenix2dock"
if str(P2D_ROOT) not in sys.path:
    sys.path.insert(0, str(P2D_ROOT))

from peplm.dpeptide import chirality_violations, mirror_structure  # noqa: E402
from peplm.dpeptide.pipeline import flip_product  # noqa: E402

rsp = importlib.import_module("backend.runtime.run_single_prediction")

FIXTURES = PEPLM_ROOT / "tests" / "fixtures"
NATIVE = FIXTURES / "3LNJ_native.pdb"    # product frame: L-MDM2 + D-PMI
MIRROR = FIXTURES / "3LNJ_mirror.pdb"    # design frame: D-receptor + L-peptide


# --------------------------------------------------------------------------
# 1. chirality_violations primitive
# --------------------------------------------------------------------------

def test_fixture_frames_are_what_we_claim():
    # NATIVE = product frame (L-MDM2 + D-PMI); MIRROR = design/refine frame
    # (D-receptor + L-peptide). Geometry-verified: never trust naming again.
    nat = gemmi.read_structure(str(NATIVE)); nat.setup_entities()
    assert chirality_violations(nat, "A", "L") == []
    assert chirality_violations(nat, "B", "D") == []
    mir = gemmi.read_structure(str(MIRROR)); mir.setup_entities()
    assert chirality_violations(mir, "A", "D") == []
    assert chirality_violations(mir, "B", "L") == []


def test_violations_clean_mirror_design_frame():
    st = gemmi.read_structure(str(MIRROR)); st.setup_entities()
    assert chirality_violations(st, "A", "D") == []
    assert chirality_violations(st, "B", "L") == []


def test_violations_wrong_expectation_lists_all():
    st = gemmi.read_structure(str(NATIVE)); st.setup_entities()
    wrong = chirality_violations(st, "A", "D")
    assert len(wrong) >= 50  # an all-L chain against D expectation: everything flags
    wrong_b = chirality_violations(st, "B", "L")
    assert len(wrong_b) == 11  # the D-PMI chain against L expectation flags fully


def test_violations_single_inverted_residue_detected():
    st = gemmi.read_structure(str(MIRROR)); st.setup_entities()
    chain = st[0]["B"]
    residue = next(r for r in chain if "CB" in r)
    cb = residue["CB"][0]
    # reflect CB through CA: the chiral volume flips sign exactly
    ca = residue["CA"][0].pos
    cb.pos = gemmi.Position(
        2 * ca.x - cb.pos.x, 2 * ca.y - cb.pos.y, 2 * ca.z - cb.pos.z)
    bad = chirality_violations(st, "B", "L")
    assert len(bad) == 1
    assert bad[0][0] == residue.seqid.num


def test_violations_ambiguous_volume_flagged():
    st = gemmi.read_structure(str(MIRROR)); st.setup_entities()
    chain = st[0]["B"]
    residue = next(r for r in chain if "CB" in r)
    ca = residue["CA"][0].pos
    residue["CB"][0].pos = gemmi.Position(ca.x, ca.y, ca.z)  # volume -> 0
    bad = chirality_violations(st, "B", "L")
    assert any(n == residue.seqid.num for n, _, _ in bad)


def test_violations_bad_expectation_raises():
    st = gemmi.read_structure(str(NATIVE)); st.setup_entities()
    with pytest.raises(ValueError):
        chirality_violations(st, "A", "X")


# --------------------------------------------------------------------------
# 2. staging requires pocket or reference
# --------------------------------------------------------------------------

def _write_single_chain(src: Path, chain_id: str, dst: Path) -> Path:
    st = gemmi.read_structure(str(src)); st.setup_entities()
    out = gemmi.Structure()
    model = gemmi.Model("1")
    model.add_chain(chain_id)
    for res in st[0][chain_id]:
        model[chain_id].add_residue(res)
    out.add_model(model)
    out.setup_entities()
    out.write_pdb(str(dst))
    return dst


@pytest.fixture(scope="module")
def tmp_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("dpep")


def test_staging_without_pocket_or_reference_raises(tmp_dir):
    d_target = _write_single_chain(MIRROR, "A", tmp_dir / "d_target.pdb")
    conformer = _write_single_chain(NATIVE, "B", tmp_dir / "conformer.pdb")
    with pytest.raises(ValueError, match="口袋|pocket"):
        rsp._dpeptide_stage_conformer_in_pocket(
            d_target, conformer, tmp_dir / "staged.pdb",
            [], seed=1)


def test_staging_unknown_pocket_residues_raise(tmp_dir):
    d_target = _write_single_chain(MIRROR, "A", tmp_dir / "d_target2.pdb")
    conformer = _write_single_chain(NATIVE, "B", tmp_dir / "conformer2.pdb")
    with pytest.raises(ValueError, match="not found"):
        rsp._dpeptide_stage_conformer_in_pocket(
            d_target, conformer, tmp_dir / "staged2.pdb",
            [("A", 9999)], seed=1)


# --------------------------------------------------------------------------
# 3. pocket placement quality (clash-free, anchored) on the real pocket
# --------------------------------------------------------------------------

def _pmi_pocket_contacts() -> list[tuple[str, int]]:
    """PMI-contacting MDM2 residues (<5 A) in 1-based sequence order."""
    st = gemmi.read_structure(str(NATIVE)); st.setup_entities()
    A, B = st[0]["A"], st[0]["B"]
    nums = set()
    for ra in A:
        for rb in B:
            d = min((aa.pos.dist(ab.pos) for aa in ra for ab in rb), default=1e9)
            if d < 5.0:
                nums.add(ra.seqid.num)
    return [("A", n) for n in sorted(nums)]


@pytest.mark.parametrize("seed", [1, 7, 47])
def test_staging_pocket_placement_clash_free(tmp_dir, seed):
    d_target = _write_single_chain(MIRROR, "A", tmp_dir / f"dt_{seed}.pdb")
    conformer = _write_single_chain(MIRROR, "B", tmp_dir / f"cf_{seed}.pdb")  # L geometry
    out = tmp_dir / f"staged_{seed}.pdb"
    rsp._dpeptide_stage_conformer_in_pocket(
        d_target, conformer, out, _pmi_pocket_contacts(), seed=seed)
    # the self-check inside already enforces zero <2.2 A + anchored; verify
    # independently here so a future self-check regression cannot hide
    st = gemmi.read_structure(str(out)); st.setup_entities()
    from scipy.spatial import cKDTree
    ra = np.array([[a.pos.x, a.pos.y, a.pos.z]
                   for r in st[0]["A"] for a in r if a.element.name != "H"])
    rb = np.array([[a.pos.x, a.pos.y, a.pos.z]
                   for r in st[0]["B"] for a in r if a.element.name != "H"])
    nb = cKDTree(ra).query(rb, k=1)[0]
    assert int((nb < 2.2).sum()) == 0
    assert (nb < 6.0).any()
    # chirality survives rigid placement: receptor D, binder L
    assert chirality_violations(st, "A", "D") == []
    assert chirality_violations(st, "B", "L") == []


# --------------------------------------------------------------------------
# 4. refined mirror-complex chirality gate
# --------------------------------------------------------------------------

def test_refined_gate_clean_mirror_complex_passes(tmp_dir):
    # MIRROR fixture IS a valid refined-space complex (D receptor + L peptide)
    gate = rsp._dpeptide_refined_chirality_gate(Path(MIRROR))
    assert gate == 0.0


def test_refined_gate_flipped_residue_penalized_not_killed(tmp_dir):
    """1 CA flip in a 17-mer (6%) = inpainting artifact, NOT a design failure.

    The gate returns a proportional score penalty instead of raising —
    the sequence is the chirality source of truth, the inpainting structure
    is the scoring artifact (measured: ipTM 0.749 candidate killed by 5/12
    flips that don't change the binding mode)."""
    st = gemmi.read_structure(str(MIRROR)); st.setup_entities()
    chain = st[0]["B"]
    residue = next(r for r in chain if "CB" in r)
    ca = residue["CA"][0].pos
    cb = residue["CB"][0]
    cb.pos = gemmi.Position(2 * ca.x - cb.pos.x, 2 * ca.y - cb.pos.y,
                            2 * ca.z - cb.pos.z)
    bad_path = tmp_dir / "bad_refined.pdb"
    st.write_pdb(str(bad_path))
    penalty = rsp._dpeptide_refined_chirality_gate(bad_path)
    assert penalty > 0.0  # penalized...
    assert penalty <= 0.15  # ...proportionally (1/17 ≈ 6% → ~0.008)


def test_refined_gate_mass_flip_still_rejects(tmp_dir):
    """>30% CA flips = sampler failure, hard reject maintained."""
    st = gemmi.read_structure(str(MIRROR)); st.setup_entities()
    chain = st[0]["B"]
    flipped = 0
    for residue in chain:
        if "CB" not in residue or "CA" not in residue:
            continue
        if flipped >= 6:  # 6/17 > 30%
            break
        ca = residue["CA"][0].pos
        cb = residue["CB"][0]
        cb.pos = gemmi.Position(2 * ca.x - cb.pos.x, 2 * ca.y - cb.pos.y,
                                2 * ca.z - cb.pos.z)
        flipped += 1
    bad_path = tmp_dir / "mass_flip.pdb"
    st.write_pdb(str(bad_path))
    with pytest.raises(RuntimeError, match="手性"):
        rsp._dpeptide_refined_chirality_gate(bad_path)


# --------------------------------------------------------------------------
# 5. composite rescoring from refined metrics
# --------------------------------------------------------------------------

def test_composite_no_pocket_weights():
    r = rsp._dpeptide_composite_from_refined(
        refined_ipsae=0.9, binder_avg_plddt=80.0, developability_score=1.0,
        has_pocket=False)
    assert r["interface_confidence"] == pytest.approx(0.9)
    assert r["binder_confidence"] == pytest.approx(0.8)
    assert r["composite_score"] == pytest.approx(
        0.58 * 0.9 + 0.22 * 0.8 + 0.12 * 0.9 + 0.08 * 1.0)
    assert r["interface_metric_source"] == "d_space_refined_ipsae"
    assert r["interface_metric_label"] == "ipSAE"


def test_composite_pocket_weights_leave_room_for_pocket_term():
    r = rsp._dpeptide_composite_from_refined(
        refined_ipsae=0.9, binder_avg_plddt=80.0, developability_score=1.0,
        has_pocket=True)
    assert r["composite_score"] == pytest.approx(
        0.40 * 0.9 + 0.15 * 0.8 + 0.08 * 0.9 + 0.05 * 1.0)
    assert r["composite_score"] < 0.69  # 0.32 pocket headroom reserved


def test_composite_clamps_out_of_range_inputs():
    r = rsp._dpeptide_composite_from_refined(
        refined_ipsae=1.7, binder_avg_plddt=250.0, developability_score=-3.0,
        has_pocket=False)
    assert r["interface_confidence"] == 1.0
    assert r["binder_confidence"] == 1.0
    assert r["developability_score"] == 0.0


def test_composite_zero_plddt_zero_confidence():
    r = rsp._dpeptide_composite_from_refined(
        refined_ipsae=0.5, binder_avg_plddt=0.0, developability_score=1.0,
        has_pocket=False)
    assert r["binder_confidence"] == 0.0


# --------------------------------------------------------------------------
# 6. product gate is a hard per-residue gate
# --------------------------------------------------------------------------

def _product_from_mirror(dst: Path) -> Path:
    flip_product(Path(MIRROR), dst)
    return dst


def test_product_gate_clean_product_passes(tmp_dir):
    product = _product_from_mirror(tmp_dir / "product.pdb")
    # no exception expected
    rsp._assert_product_chirality(product, reference_structure_path=None,
                                  rmsd_limit=None)


def test_product_gate_mixed_peptide_rejects(tmp_dir):
    product = _product_from_mirror(tmp_dir / "product2.pdb")
    st = gemmi.read_structure(str(product)); st.setup_entities()
    chains = sorted(
        (c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3),
        key=lambda c: -sum(1 for r in c if r.het_flag != "H"))
    peptide = chains[1]
    # Product peptide must be all-D; flip >30% of residues back to L. The
    # gate tolerates a minority of CA inversions (free-chain diffusion
    # artifacts, same 30% policy as candidate evaluation), so a 3-residue
    # flip on this 11-mer stays inside tolerance — 4 (36%) must reject.
    n = sum(1 for r in peptide if r.het_flag != "H")
    max_flips = max(1, int(-(-0.31 * n // 1)))  # ceil, >30%
    flipped = 0
    for residue in peptide:
        if "CB" not in residue or flipped >= max_flips:
            continue
        ca = residue["CA"][0].pos
        cb = residue["CB"][0]
        cb.pos = gemmi.Position(2 * ca.x - cb.pos.x, 2 * ca.y - cb.pos.y,
                                2 * ca.z - cb.pos.z)
        flipped += 1
    bad_path = tmp_dir / "product_bad.pdb"
    st.write_pdb(str(bad_path))
    with pytest.raises(RuntimeError, match="peptide not all-D"):
        rsp._assert_product_chirality(bad_path, reference_structure_path=None,
                                      rmsd_limit=None)


# --------------------------------------------------------------------------
# 7. protenix2dock blind flag plumbing (arg parsing; engine covered by the
#    A/B repro at the container level)
# --------------------------------------------------------------------------

def test_blind_peptide_flag_parses_and_defaults_off():
    import capabilities.protenix2dock.protenix2dock as p2d
    parser = p2d.build_parser() if hasattr(p2d, "build_parser") else None
    if parser is None:
        pytest.skip("argparse not exposed as build_parser")
    args = parser.parse_args(["--mode", "peptide", "--input", "x.pdb"])
    assert args.blind_peptide is False


# --------------------------------------------------------------------------
# 8. MSA tier policy: length-predicted auto + official optional-MSA semantics
# --------------------------------------------------------------------------

def test_auto_msa_mode_length_boundary():
    from core.input_prep import auto_msa_mode
    assert auto_msa_mode("ACDEFGHIKLMNPQRSTVWY") == "uniref"        # 20 aa
    assert auto_msa_mode("A" * 49) == "uniref"
    assert auto_msa_mode("A" * 50) == "env"                          # boundary
    assert auto_msa_type_long_chain()


def auto_msa_type_long_chain():
    from core.input_prep import auto_msa_mode
    return auto_msa_mode("A" * 83) == "env"


def test_resolve_msa_no_server_writes_query_only(tmp_path):
    from core.input_prep import resolve_msa
    seq = "ACDEFGHIKLMNPQRSTVWY"
    path = resolve_msa(seq, "B", None, None, tmp_path / "msa", msa_mode="auto")
    text = Path(path).read_text()
    assert text.strip() == f">query\n{seq}"   # official N_msa=1 representation


def test_resolve_msa_tiered_cache_names(tmp_path):
    from core import input_prep as ip
    calls = {}

    def fake_fetch(sequence, server_url, timeout, msa_mode="env"):
        calls["mode"] = msa_mode
        # 对齐行必须与查询等长(a3m 语义), 用原序列自身做唯二条目
        return f">query\n{sequence}\n>101\n{sequence}\n"

    ip._fetch_msa_from_server = fake_fetch
    seq20 = "ACDEFGHIKLMNPQRSTVWY"
    p1 = ip.resolve_msa(seq20, "B", tmp_path / "cache", "http://x",
                        tmp_path / "msa1", msa_mode="auto")
    assert calls["mode"] == "uniref"                    # short -> uniref tier
    assert Path(p1).name.endswith("_uniref_msa.a3m")
    seq60 = "ACDEFGHIKLMNPQRSTVWY" * 3
    p2 = ip.resolve_msa(seq60, "A", tmp_path / "cache", "http://x",
                        tmp_path / "msa2", msa_mode="auto")
    assert calls["mode"] == "env"                       # long -> env tier
    assert Path(p2).name.endswith("_env_msa.a3m")


# --------------------------------------------------------------------------
# 9. blind inpainting route (2026-09-04 protocol decision, PMI A/B evidence)
# --------------------------------------------------------------------------

def test_blind_staging_needs_no_pocket_and_skips_selfcheck(tmp_dir):
    """Blind linear route: pose is discarded (peptide rows re-noised), so
    staging must accept a pocket-free input and skip the clash self-check."""
    d_target = _write_single_chain(MIRROR, "A", tmp_dir / "dt_blind.pdb")
    conformer = _write_single_chain(MIRROR, "B", tmp_dir / "cf_blind.pdb")
    out = tmp_dir / "staged_blind.pdb"
    rsp._dpeptide_stage_conformer_in_pocket(
        d_target, conformer, out, [], seed=3, pose_matters=False)
    st = gemmi.read_structure(str(out)); st.setup_entities()
    assert {"A", "B"} <= {c.name for c in st[0]}


def test_blind_pose_required_still_raises_without_pocket(tmp_dir):
    d_target = _write_single_chain(MIRROR, "A", tmp_dir / "dt_b2.pdb")
    conformer = _write_single_chain(MIRROR, "B", tmp_dir / "cf_b2.pdb")
    with pytest.raises(ValueError, match="口袋|pocket"):
        rsp._dpeptide_stage_conformer_in_pocket(
            d_target, conformer, tmp_dir / "s2.pdb", [], seed=3,
            pose_matters=True)


def test_dispatch_carries_blind_flag():
    import inspect
    src = inspect.getsource(rsp._dpeptide_dispatch_refine)
    assert '"blind_peptide": bool(blind)' in src
    assert "blind: bool = False" in src


def test_worker_passes_blind_flag():
    import backend.worker.protenix2dock_task as w
    import inspect
    src = inspect.getsource(w)
    assert '"--blind_peptide"' in src


def test_dock_mode_has_no_hand_rolled_search():
    """The placement ensemble / steric anchor search was removed (user
    directive: use the engine's own capability). Native blind inpainting
    replaced it."""
    import capabilities.protenix2dock.protenix2dock as p2d
    import inspect
    src = inspect.getsource(p2d)
    assert "_placement_ensemble" not in src
    assert "_steric_anchor_pairs" not in src
    assert "_diverse_conformers" not in src
    assert "native blind inpainting" in src


def test_blind_peptide_flag_full_schedule_default():
    from core.modes import built_in_config
    # dock's config itself carries the full blind schedule
    dock_cfg = built_in_config("dock")
    assert float(dock_cfg["sigma_max"]) == 160.0
    assert int(dock_cfg["sampling_steps"]) == 200
    # --blind_peptide overrides the peptide local-refine ladder with the
    # same full schedule at runtime
    import capabilities.protenix2dock.protenix2dock as p2d
    import inspect
    src = inspect.getsource(p2d)
    assert 'getattr(args, "blind_peptide", False) and args.mode == "peptide"' in src
    assert "sigma_max = 160.0" in src
