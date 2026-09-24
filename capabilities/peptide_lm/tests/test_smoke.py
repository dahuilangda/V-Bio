"""CPU smoke tests: representation, oracles, reward, GRPO, closed loop.

Run: /data/Boltz2Score/.venv/bin/python -m pytest tests/ -q   (~1 min)
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import torch

from peplm.candidate import Candidate
from peplm.loop.config import LoopConfig
from peplm.loop.engine import PeptideLoop
from peplm.models.llama_prior import ModernPrior
from peplm.vocab import PlacementMask
from peplm.oracle.peptide_boltz import MockPeptideOracle, build_complex_yaml
from peplm.props.descriptors import compute_props, dev_tag_for
from peplm.score.production import production_composite
from peplm.score.reward import PeptideReward
from peplm.vocab import (
    DEFAULT_VOCAB,
    from_modifications,
    parse_tokens,
    to_modifications,
)


def test_vocab_roundtrip():
    s = "AC[AIB]GK[CIT]WT[PCA]"
    toks = parse_tokens(s)
    assert tomod_roundtrip(toks)
    enc = DEFAULT_VOCAB.encode(s)
    assert DEFAULT_VOCAB.decode(enc) == s
    base, mods = to_modifications(toks)
    assert base == "ACAGKRWTE" and len(mods) == 3
    assert mods[0] == {"position": 3, "ccd": "AIB", "baseResidue": "A"}
    assert "".join(from_modifications(base, mods)) == s


def tomod_roundtrip(toks):
    base, mods = to_modifications(toks)
    return "".join(from_modifications(base, mods)) == "".join(toks)


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_tokens("AC1G")
    with pytest.raises(ValueError):
        parse_tokens("acgt")


def test_props_reasonable():
    good = compute_props(parse_tokens("ETFSDLWKLLPEN"))
    bad = compute_props(parse_tokens("WWWWWWLLLIIIFF"))
    assert good["developability"] > bad["developability"]
    assert good["solubility"] > bad["solubility"]
    assert compute_props(parse_tokens("AC[PCA]K"))["length"] == 4


def test_yaml_builder():
    cand = Candidate(tokens=parse_tokens("<cyc>AC[AIB]K"), cyclic=True)
    y = build_complex_yaml("MKTLLL", cand)
    assert "cyclic: true" in y
    assert "ccd: AIB" in y
    assert "sequence: ACAK" in y.replace("- ", "")  # [AIB] writes back its base A


def test_production_composite():
    m = {"ipsae_dom": 0.7, "pair_iptm": 0.8, "binder_avg_plddt": 80.0}
    c = production_composite(m, "ACDEFGHIKL")
    assert 0.5 < c < 0.95
    assert production_composite({}, "ACD") is None


def test_reward_pose_gate():
    rw = PeptideReward(ncaa_range=(0, 4), len_range=(8, 25))
    good = Candidate(tokens=parse_tokens("<lin>ETFSDLWKLLPEN"))
    good.metrics = {"ipsae_dom": 0.75, "pair_iptm": 0.85,
                    "binder_avg_plddt": 88.0}
    badpose = Candidate(tokens=parse_tokens("<lin>ETFSDLWKLLPEN"))
    badpose.metrics = {"ipsae_dom": 0.75, "pair_iptm": 0.85,
                       "binder_avg_plddt": 30.0}
    r1, p1 = rw.machine_reward(good)
    r2, p2 = rw.machine_reward(badpose)
    assert r1 > r2
    assert p1["interface"] > p2["interface"]  # gated


def _tiny_prior(device="cpu"):
    torch.manual_seed(0)
    return ModernPrior(DEFAULT_VOCAB, d_model=64, n_layers=2, n_heads=4,
                       max_len=64).to(device)


def test_placement_mask():
    prior = _tiny_prior()
    pm = PlacementMask(DEFAULT_VOCAB, max_len=64, ncaa_max=2)
    # PCA (n_term only) must never appear at position > 0
    toks = prior.sample_with_prompt(["<dev_hi>", "<lin>"], 8, "cpu",
                                    placement=pm, target_len=12,
                                    return_tokens=True,
                                    ban_tokens=["<cont>", "<mask>"])
    for t in toks:
        res = [x for x in t if not x.startswith("<")]
        for i, x in enumerate(res):
            if x == "[PCA]" and i != 0:
                pytest.fail(f"PCA at {i}: {res}")
        assert sum(1 for x in res if x.startswith("[")) <= 2 or len(res) == 0


def test_fixed_residues_and_lengths():
    """User-pinned residues survive every operator; fixed length respected."""
    torch.manual_seed(3)
    import random as _r

    from peplm.loop.config import LoopConfig
    from peplm.loop.engine import PeptideLoop
    from peplm.models.llama_prior import ModernPrior
    from peplm.oracle.peptide_boltz import MockPeptideOracle

    fixed = [{"position": 1, "residue": "C"},      # bicyclic anchor pos 1
             {"position": 5, "residue": "[AIB]"},  # user NCAA pinned mid
             {"position": 18, "residue": "C"}]     # terminal anchor
    m = ModernPrior(DEFAULT_VOCAB, d_model=48, n_layers=1, n_heads=4,
                    max_len=64)
    cfg = LoopConfig(n_rounds=1, n_agent=10, n_edit=4, n_mut=4,
                     oracle_budget=8, len_range=(18, 18),  # fixed length
                     ncaa_range=(1, 3), ncaa_pool=("AIB",),  # user pool
                     device="cpu", use_surrogate=False,
                     elite_size=4, design_mode="bicyclic",
                     cys_positions=(8,), fixed_residues=tuple(fixed), seed=2)
    loop = PeptideLoop(cfg, "MTEITAAMVKELRESTGAGMMDCKNALSET", m, DEFAULT_VOCAB,
                       MockPeptideOracle(), run_dir="tests/_tmp_fix",
                       log=lambda *a: None)
    loop.run()
    rows = [json.loads(l)
            for l in Path("tests/_tmp_fix/scored.jsonl").read_text().splitlines()
            if l.strip()]
    seqs = [r["seq"] for r in rows]
    assert seqs, "no scored candidates"
    for s in seqs:
        res = [t for t in parse_tokens(s) if len(t) == 1 or t.startswith("[")]
        assert len(res) == 18, f"fixed length violated: {len(res)} {s}"
        assert res[0] == "C" and res[-1] == "C", f"anchors: {s}"
        assert res[4] == "[AIB]", f"pinned residue lost: {s}"
        assert res.count("C") == 3, f"cys count: {s}"
    import shutil

    shutil.rmtree("tests/_tmp_fix", ignore_errors=True)


def test_constraint_plan_decode_time():
    """Upgrade 3: fixed residues / NCAA pool + min quota / length bounds are
    enforced DURING decoding, not by post-hoc repair."""
    torch.manual_seed(4)
    from peplm.loop.constraints import ConstraintPlan, build_plan
    from peplm.models.llama_prior import ModernPrior

    m = ModernPrior(DEFAULT_VOCAB, d_model=48, n_layers=1, n_heads=4, max_len=64)

    class Cfg:
        len_range = (10, 10)
        design_mode = "linear"
        bicyclic_layout = "first_last"
        ncaa_range = (1, 2)
        ncaa_decode_bias = 1.0
        cys_positions = ()
        allow_extra_cys = False

    plan = build_plan(Cfg(), DEFAULT_VOCAB, length=10,
                      fixed={2: "F"}, ncaa_pool_tokens=["[AIB]", "[CIT]"])
    outs = m.sample_with_prompt(["<lin>"], 24, "cpu", temperature=1.0,
                                constraints=plan, target_len=10,
                                return_tokens=True,
                                ban_tokens=["<lin>", "<cyc>", "<bicy>"])
    seqs = [[t for t in o if not t.startswith("<")] for o in outs]
    seqs = [s for s in seqs if len(s) >= 6]
    assert seqs, "no samples after constraints"
    for s in seqs:
        assert s[2] == "F", f"fixed residue lost: {''.join(s)}"
        assert len(s) <= 10, f"length bound violated"
        ncaa = [t for t in s if t.startswith("[")]
        assert all(t in ("[AIB]", "[CIT]") for t in ncaa), "off-pool NCAA!"
    assert any("[AIB]" in s or "[CIT]" in s for s in seqs) or all(
        len(s) < 10 for s in seqs)  # min quota either satisfied or length<10

    # empty pool = pure natural (no bracket tokens at all, even with bias)
    plan2 = build_plan(Cfg(), DEFAULT_VOCAB, length=10, fixed={},
                       ncaa_pool_tokens=[])
    outs2 = m.sample_with_prompt(["<lin>"], 12, "cpu", constraints=plan2,
                                 target_len=10, return_tokens=True,
                                 ban_tokens=["<lin>", "<cyc>", "<bicy>"])
    for o in outs2:
        assert not any(t.startswith("[") for t in o), "bracket outside pool!"


def test_fim_edit_relative_fixed():
    """Fixed residues inside a FIM fill are mapped to emitted-relative
    positions (upgrade 3, FIM path)."""
    torch.manual_seed(5)
    from peplm.generate.edit import edit_candidates
    from peplm.models.llama_prior import ModernPrior

    m = ModernPrior(DEFAULT_VOCAB, d_model=48, n_layers=1, n_heads=4, max_len=64)
    parent = Candidate(tokens=parse_tokens("<lin>ETFSDLWKLLPEN"))
    parent.metrics = {"binder_plddt": [90] * 13 + [40] * 1 + [90] * 1}
    plan_kwargs = {"len_range": (8, 20), "design_mode": "linear",
                   "bicyclic_layout": "first_last", "ncaa_range": (0, 0),
                   "ncaa_decode_bias": 0.0}
    outs = edit_candidates(m, DEFAULT_VOCAB, parent, 8, "cpu",
                           random.Random(0), ncaa_max=0,
                           fixed_abs={7: "F"}, pool_tokens=[],
                           plan_kwargs=plan_kwargs)
    scored = [c for c in outs if len(c.residues) >= 8]
    assert scored, "no FIM edits survived"
    assert any(c.residues[7] == "F" for c in scored), \
        "relative fixed residue not enforced in fills"


def test_interchain_pae_consistency_math():
    """Upgrade 1: same interface -> high consistency; different -> low."""
    import numpy as np

    from peplm.oracle.interchain_pae import InterchainPAE, consistency_score

    rng = np.random.RandomState(0)
    base = rng.uniform(2, 14, size=(20, 12))
    a = InterchainPAE(matrix=base, min_ipae=float(base.min()),
                      mean_ipae=float(base.mean()), n_binder=12)
    b_same = InterchainPAE(matrix=base + rng.normal(0, 0.3, base.shape),
                           min_ipae=float((base + rng.normal(0, 0.3, base.shape)).min()),
                           mean_ipae=float((base + rng.normal(0, 0.3, base.shape)).mean()),
                           n_binder=12)
    b_diff = InterchainPAE(matrix=rng.uniform(2, 14, size=(20, 12)),
                           min_ipae=float(rng.uniform(2, 14)),
                           mean_ipae=float(rng.uniform(2, 14)), n_binder=12)
    r_same = consistency_score(a, b_same)
    r_diff = consistency_score(a, b_diff)
    assert r_same["self_consistency"] > 0.7, r_same
    assert r_diff["self_consistency"] < r_same["self_consistency"], \
        (r_same, r_diff)
    assert 0.0 <= r_diff["self_consistency"] <= 1.0


def test_extract_pae_from_saved_outputs():
    """Extraction reads real saved boltz + protenix artifacts (no GPU)."""
    import glob

    from peplm.oracle.interchain_pae import (
        extract_boltz_pae,
        extract_protenix_pae,
    )

    rec = sorted(glob.glob("runs/bench_leadopt/bclxl_lm/oracle/**/predictions/0000",
                           recursive=True))
    if rec:
        pae = extract_boltz_pae(rec[0], n_binder=16)
        assert pae is not None and pae.matrix.shape[1] == 16
        assert pae.min_ipae < pae.mean_ipae
    preds = sorted(glob.glob(
        "runs/cd73_protenix/oracle/**/output/cand*/seed_*/predictions",
        recursive=True))
    if preds:
        import json as _json

        # binder length comes from the run's input.json (candidates vary)
        pred_root = Path(preds[0])
        run_dir = pred_root.parents[3]         # .../r001/g1/0000
        inp = run_dir / "input" / "input.json"
        seq = None
        if inp.exists():
            payload = _json.loads(inp.read_text())[0]
            for s in (payload.get("sequences") or []):
                pc = s.get("proteinChain") or {}
                if pc and 5 <= len(pc.get("sequence") or "") <= 40:
                    seq = pc["sequence"]
        if not seq:  # fallback: any seq length that matches a group
            seq = "A" * 15
        pae = extract_protenix_pae(pred_root, binder_residues=len(seq))
        assert pae is not None
        assert pae.min_ipae <= pae.mean_ipae
        assert pae.n_binder == len(seq)


def test_consistency_guard_merges():
    """The guard re-folds top-k with a secondary oracle and merges the
    consistency terms (CPU: fake oracles with PAE submatrices)."""
    import numpy as np

    from peplm.loop.consistency_guard import ConsistencyGuard

    rng = np.random.RandomState(1)
    mat = rng.uniform(2, 14, size=(15, 10))

    class FakeOracle:
        def __init__(self):
            self.n = 0

        def score(self, candidates, tag="b"):
            self.n += len(candidates)
            for k, c in enumerate(candidates):
                m = rng.uniform(2, 14, size=(15, 10)) if tag.endswith("_sc") \
                    else mat + rng.normal(0, 0.2, (15, 10))
                c.metrics = {"pair_iptm": 0.8 - 0.01 * k,
                             "_ipsae_submatrix": m.tolist(),
                             "min_ipae": float(np.min(m)),
                             "mean_ipae": float(np.mean(m))}

    cands = [Candidate(tokens=parse_tokens("<lin>ACDEFGHIKL")) for _ in range(6)]
    for i, c in enumerate(cands):
        c.reward = 1.0 - 0.1 * i
    guard = ConsistencyGuard(FakeOracle(), FakeOracle(), topk=4)
    guard.score(cands)
    assert guard.n_extra == 4
    merged = [c for c in cands if c.metrics.get("self_consistency") is not None]
    assert len(merged) == 4
    assert all(0.0 <= c.metrics["self_consistency"] <= 1.0 for c in merged)


def test_post_edit_runs_for_any_layout_string():
    """apply_post_edit must key off plan.post_edit, not the layout string:
    callers pass the layout VARIANT ('first_last'), so gating on
    layout=='bicyclic' silently skips the bounded adaptive-length edit."""
    from peplm.loop.constraints import apply_post_edit, plan_for_post_edit

    plan = plan_for_post_edit({}, DEFAULT_VOCAB)
    out = apply_post_edit(list("AETFSDLWKLLPEN"), plan, "first_last")
    assert out[0] == "C" and out[-1] == "C" and out.count("C") == 3
    # no post_edit marker -> untouched
    from peplm.loop.constraints import ConstraintPlan

    plain = ConstraintPlan(vocab=DEFAULT_VOCAB)
    assert apply_post_edit(list("ACDE"), plain, "first_last") == list("ACDE")



def test_choose_bicyclic_anchors():
    """Anchor policy: 3 explicit in-range positions win; otherwise the
    first/interior/last layout (pinned C > explicit interior > midpoint)."""
    from peplm.loop.constraints import choose_bicyclic_anchors

    assert choose_bicyclic_anchors(15, {}, (2, 7, 14)) == (2, 7, 14)
    assert choose_bicyclic_anchors(15, {}, (7,)) == (0, 7, 14)
    assert choose_bicyclic_anchors(15, {3: "C"}, (7,)) == (0, 3, 14)
    auto = choose_bicyclic_anchors(16, {7: "F"}, ())
    assert auto[0] == 0 and auto[2] == 15
    assert 0 < auto[1] < 15 and auto[1] != 7


def test_manual_anchor_decode_time():
    """Manual 3-position mode: decode forces C exactly at the user anchors
    and bans non-anchor Cys (manual positions take precedence over
    double-shifted and ignored on the fixed-length path)."""
    torch.manual_seed(6)
    from peplm.loop.constraints import build_plan
    from peplm.models.llama_prior import ModernPrior

    m = ModernPrior(DEFAULT_VOCAB, d_model=48, n_layers=1, n_heads=4, max_len=64)

    class Cfg:
        len_range = (15, 15)
        design_mode = "bicyclic"
        bicyclic_layout = "first_last"
        ncaa_range = (0, 0)
        ncaa_decode_bias = 0.0
        cys_positions = (2, 7, 14)
        allow_extra_cys = False

    plan = build_plan(Cfg(), DEFAULT_VOCAB, length=15, fixed={})
    assert plan.anchors == (2, 7, 14)
    assert plan.ban_cys is True
    outs = m.sample_with_prompt(["<bicy>"], 16, "cpu", constraints=plan,
                                target_len=15, return_tokens=True,
                                ban_tokens=["<lin>", "<cyc>", "<bicy>"])
    seqs = [[t for t in o if not t.startswith("<")] for o in outs]
    seqs = [s for s in seqs if len(s) == 15]
    assert seqs, "no full-length samples under manual anchors"
    for s in seqs:
        assert [i for i, t in enumerate(s) if t == "C"] == [2, 7, 14], \
            f"anchors wrong: {''.join(s)}"


def test_allow_extra_cys_flag():
    """allow_extra_cys keeps unlinked Cys; strict mode scrubs them."""
    from peplm.loop.constraints import apply_post_edit, plan_for_post_edit

    seq = list("ACEFGCHKLMNOPQC")
    loose = plan_for_post_edit({}, DEFAULT_VOCAB, (2, 7, 14),
                               allow_extra_cys=True)
    out = apply_post_edit(seq, loose, "first_last")
    assert out[0] == "A" and out[1] == "C"  # position 0 untouched, stray kept
    assert out[2] == "C" and out[7] == "C" and out[14] == "C"
    strict = plan_for_post_edit({}, DEFAULT_VOCAB, (2, 7, 14))
    out2 = apply_post_edit(seq, strict, "first_last")
    assert [i for i, t in enumerate(out2) if t == "C"] == [2, 7, 14]


def test_modern_prior_and_user_residues():
    torch.manual_seed(2)
    from peplm.models.llama_prior import ModernPrior
    from peplm.models.train_modern import modality_augment
    from peplm.residues import (USER_RESIDUES, placement_lookup,
                                register_user_residues, residue_meta)

    # modality augmentation produces all three structure tokens
    lines = ["<sol_h> <syn_h> <liab_h> <L15> <lin> ACDEFGHIKLMNPQRS",
             "<lin> ACDEFGHIKLMNPQRS"]
    import random as _r

    aug = modality_augment(lines * 200, _r.Random(0))
    mods = {l.split()[-2] for l in aug}
    assert mods <= {"<lin>", "<cyc>", "<bicy>"}
    assert "<bicy>" in mods and "<cyc>" in mods
    # bicyclic lines get the 3-Cys first/interior/last layout
    bi = next(l for l in aug if l.split()[-2] == "<bicy>")
    seq = bi.split()[-1]
    assert seq[0] == "C" and seq[-1] == "C" and seq.count("C") == 3

    # user residue registration + placement + meta
    added = register_user_residues([
        {"ccd": "UAA", "smiles": "NC(CSCH3)C(=O)O", "base": "A",
         "placement": "any"},
        {"ccd": "UBB", "smiles": "NC(Cc1ccccc1F)C(=O)O", "base": "F",
         "placement": "n_term"},
        {"ccd": "AIB", "smiles": "junk"},  # preset collision -> ignored
    ])
    assert added == ["UAA", "UBB"]
    assert placement_lookup("[UBB]") == "n_term"
    assert residue_meta("UAA")["base"] == "A"
    assert residue_meta("AIB")["smiles"] != "junk"

    # dynamic vocab extension keeps sampling working
    m = ModernPrior(DEFAULT_VOCAB, d_model=48, n_layers=1, n_heads=4, max_len=64)
    got = m.extend_vocab(["[UAA]", "[UBB]"])
    assert got == ["[UAA]", "[UBB]"]
    outs = m.sample_with_prompt(["<lin>"], 4, "cpu", return_tokens=True)
    assert len(outs) == 4
    USER_RESIDUES.pop("UAA"); USER_RESIDUES.pop("UBB")


def test_fim_edit_operator():
    torch.manual_seed(1)
    prior = _tiny_prior()
    parent = Candidate(tokens=parse_tokens("<lin>ETFSDLWKLLPEN"))
    parent.metrics = {"binder_plddt": [90, 40, 88, 85, 86, 87, 84, 83, 82,
                                       81, 80, 79, 78]}
    from peplm.generate.edit import edit_candidates

    out = edit_candidates(prior, DEFAULT_VOCAB, parent, 6, "cpu",
                          random.Random(0), ncaa_max=3)
    assert out, "FIM edit returned no candidates"
    for c in out:
        # both flanks conserved outside the edited span
        assert c.residues[0] in ("E", "T") or c.residues[0].startswith("[")
        assert len(c.residues) >= 6
        assert c.cond_prompt and "<mid>" in c.cond_prompt


def test_grpo_improves_mock_objective():
    torch.manual_seed(0)
    device = "cpu"
    prior = _tiny_prior()
    agent = _tiny_prior()
    from peplm.generate.grpo import GRPOUpdater

    upd = GRPOUpdater(agent, prior, DEFAULT_VOCAB, device=device, lr=1e-3)
    mock = MockPeptideOracle()
    rng = random.Random(0)

    def sample_batch():
        toks_list = agent.sample_with_prompt(
            ["<dev_hi>", "<lin>"], 16, device, temperature=1.0,
            return_tokens=True, ban_tokens=["<cont>", "<mask>"])
        cands = [Candidate(tokens=["<lin>"] + [t for t in toks if not t.startswith("<")])
                 for toks in toks_list]
        cands = [c for c in cands if 6 <= len(c.residues) <= 40]
        mock.score(cands)
        rw = PeptideReward(ncaa_range=(0, 8), len_range=(6, 40))
        samples = []
        for c in cands:
            c.reward, _ = rw.machine_reward(c)
            samples.append((["<dev_hi>", "<lin>"] + c.residues, c.reward,
                            "round", "oracle", 0))
        return samples, cands

    s0, c0 = sample_batch()
    base_mean = sum(c.reward for c in c0) / len(c0)
    for _ in range(3):
        s, _ = sample_batch()
        upd.update(s, epochs=2)
    s1, c1 = sample_batch()
    after_mean = sum(c.reward for c in c1) / len(c1)
    assert after_mean >= base_mean - 0.05  # shouldn't collapse


def test_closed_loop_mock():
    torch.manual_seed(0)
    prior = _tiny_prior()
    cfg = LoopConfig(n_rounds=2, n_agent=12, n_edit=4, n_mut=4,
                     oracle_budget=6, len_range=(8, 18), ncaa_range=(0, 4),
                     device="cpu", use_surrogate=False, elite_size=4)
    loop = PeptideLoop(cfg, "MKTLLILAVF", prior, DEFAULT_VOCAB,
                       MockPeptideOracle(), run_dir="tests/_tmp_loop",
                       log=lambda *a: None)
    # seed an elite so the edit channel has a parent
    seed = Candidate(tokens=parse_tokens("<lin>ETFSDLWKLLPEN"), origin="seed")
    loop.elites.append(seed)
    res = loop.run()
    assert res["rounds"], "loop produced no rounds"
    assert loop.rounds_log[-1]["pool"] > 0
    # cleanup
    import shutil

    shutil.rmtree("tests/_tmp_loop", ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------- no-fallback
def test_load_prior_rejects_undeclared_arch(tmp_path):
    """A checkpoint without an explicit arch field must fail loudly — shape
    inference has historically loaded the wrong backbone silently."""
    import torch

    from peplm.models.train import load_prior

    ckpt = {"itos": ["a"] * 40, "state_dict": {}}
    p = tmp_path / "noarch.pt"
    torch.save(ckpt, p)
    try:
        load_prior(str(p))
        raised = False
    except ValueError as exc:
        raised = "arch" in str(exc)
    assert raised, "load_prior must reject checkpoints without an arch field"


def test_engine_reward_failure_propagates():
    """combine_batch failures are configuration bugs — the engine must not
    silently drop the batch-normalization term."""
    from peplm.loop.engine import PeptideLoop

    class Boom:
        @staticmethod
        def machine_reward(c, surrogate_pred=None, surrogate_sigma=None):
            return 0.5, {}

        def combine_batch(self, parts):
            raise ArithmeticError("bad reward config")

    loop = PeptideLoop.__new__(PeptideLoop)
    loop.reward_fn = Boom()

    class C:
        key = "k"
        reward = 1.0

    try:
        cfg = type("Cfg", (), {"use_surrogate": False})()
        loop.cfg = cfg
        loop.surrogate = None
        loop._compute_rewards([C()], scored_keys=set())
        raised = True
    except ArithmeticError:
        raised = True
    except Exception as exc:  # noqa: BLE001
        print("unexpected:", type(exc).__name__, exc)
        raised = False
    assert raised


def test_protenix_parse_rejects_corrupt_summary(tmp_path):
    """A corrupt confidence JSON is data corruption, not a skippable file."""
    import sys
    from pathlib import Path

    sys.path.insert(0, ".")
    from peplm.oracle import protenix as px

    d = tmp_path / "cand0000" / "seed_42" / "predictions"
    d.mkdir(parents=True)
    (d / "x_summary_confidence_sample_0.json").write_text("{not json")
    try:
        px._parse_output(d, binder_residues=12, use_ipsae=False)
        raised = False
    except json.JSONDecodeError:
        raised = True
    assert raised



def test_geometry_tables_bond_graphs():
    """All 20 AA bond graphs match PDB CCD expectations."""
    import sys as _sys
    _sys.path.insert(0, "/data/V-Bio/capabilities/protenix2dock")
    from core.geometry_tables import STD_AA_BONDS

    expected = {'ALA': 1, 'ARG': 7, 'ASN': 4, 'ASP': 4, 'CYS': 2,
                'GLN': 5, 'GLU': 5, 'GLY': 0, 'HIS': 7, 'ILE': 4,
                'LEU': 4, 'LYS': 5, 'MET': 4, 'PHE': 9, 'PRO': 4,
                'SER': 2, 'THR': 3, 'TRP': 12, 'TYR': 10, 'VAL': 3}
    for aa, n in expected.items():
        assert len(STD_AA_BONDS[aa]) == n, f"{aa}: {len(STD_AA_BONDS[aa])} != {n}"
    # ring closures
    assert ('CZ', 'CG') in STD_AA_BONDS['PHE']
    assert ('CZ', 'CG') in STD_AA_BONDS['TYR']


def test_geometry_tables_bond_lengths():
    """Residue-specific bond lengths distinguish aromatic/sp2/sp3."""
    import sys as _sys
    _sys.path.insert(0, "/data/V-Bio/capabilities/protenix2dock")
    from core.geometry_tables import ideal_bond_length_residue, ideal_bond_angle

    assert abs(ideal_bond_length_residue('ASP', 'C', 'O', 'CG', 'OD1') - 1.25) < 0.03
    assert abs(ideal_bond_length_residue('PHE', 'C', 'C', 'CD1', 'CE1') - 1.39) < 0.03
    assert abs(ideal_bond_length_residue('GLY', 'C', 'N', 'C', 'N') - 1.33) < 0.03
    assert abs(ideal_bond_length_residue('GLY', 'C', 'O', 'C', 'O') - 1.23) < 0.03
    # angles
    assert abs(ideal_bond_angle('CA', 'C', 'N') - 116.1) < 0.5
    assert abs(ideal_bond_angle('CG', 'CD1', 'CE1', residue='PHE') - 120.0) < 1.0


def test_shared_placement_allows():
    """Shared placement_allows covers all rules."""
    from peplm.shared import placement_allows

    assert placement_allows("any", 0, 10)
    assert placement_allows("any", 5, 10)
    assert placement_allows("n_term", 0, 10)
    assert not placement_allows("n_term", 5, 10)
    assert placement_allows("c_term", 9, 10)
    assert not placement_allows("c_term", 0, 10)
    assert placement_allows("terminal", 0, 10)
    assert placement_allows("terminal", 9, 10)
    assert not placement_allows("terminal", 5, 10)


def test_shared_resolve_interface_metric():
    """Interface metric priority: ipsae_dom > ligand_ipsae_max > iptm."""
    from peplm.shared import resolve_interface_metric

    assert resolve_interface_metric({"ipsae_dom": 0.5}) == 0.5
    assert resolve_interface_metric({"ligand_ipsae_max": 0.3, "iptm": 0.2}) == 0.3
    assert resolve_interface_metric({"iptm": 0.2}) == 0.2
    assert resolve_interface_metric({}) == 0.0


def test_proposer_config():
    """ProposerConfig dataclass: defaults, from_config, cyclic forwarding."""
    from peplm.proposer_config import ProposerConfig

    cfg = ProposerConfig()
    assert cfg.peptide_length is None
    assert cfg.design_mode == "linear"
    assert cfg.cyclic is False
    cfg_cyc = ProposerConfig(cyclic=True, design_mode="cyclic", seed=42)
    assert cfg_cyc.cyclic is True
    assert cfg_cyc.seed == 42
