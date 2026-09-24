"""Unit tests for the ESM3 RL loop's math: interface metrics, relative
reward, per-residue credit, GRPO credit weighting, weak-position
selection, and oracle staging. No GPU/docker required."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from peplm.generate.masked_grpo import MaskedGRPOUpdater, Rollout
from peplm.models.esm3_policy import ESM3Policy
from peplm.score.interface import InterfaceMetrics, compute_interface_metrics
from peplm.score.peptide_reward import (
    RelativeReward,
    residue_credit,
    sequence_is_eligible,
)


# ------------------------------------------------------------ interface.py
def _synth_full_data(tmp_path, pep_len=4, rec_len=20,
                     interface_pae=3.0, bulk_pae=25.0,
                     plddt_pep=None, plddt_rec=None):
    """Minimal full_data json: receptor tokens 0..rec_len-1, peptide after."""
    n = rec_len + pep_len
    pae = np.full((n, n), bulk_pae, dtype=float)
    np.fill_diagonal(pae, 0.2)
    pae[rec_len:, :rec_len] = interface_pae
    pae[:rec_len, rec_len:] = interface_pae
    plddt_pep = plddt_pep if plddt_pep is not None else [0.9] * pep_len
    plddt_rec = plddt_rec if plddt_rec is not None else [0.95] * rec_len
    atom_plddt, atom2tok = [], []
    for tok, v in enumerate([*plddt_rec, *plddt_pep]):
        for _ in range(3):                      # 3 atoms per token
            atom_plddt.append(v)
            atom2tok.append(tok)
    data = {
        "token_pair_pae": pae.tolist(),
        "token_asym_id": [0] * rec_len + [1] * pep_len,
        "contact_probs": (0.5 * (pae < 10)).tolist(),
        "atom_plddt": atom_plddt,
        "atom_to_token_idx": atom2tok,
    }
    p = tmp_path / "full.json"
    p.write_text(json.dumps(data))
    return p


def test_interface_metrics_dense_signal_discriminates(tmp_path):
    """Two poses of one sequence: the confident interface must win on iPAE."""
    good = compute_interface_metrics(
        _synth_full_data(tmp_path / "g" if False else tmp_path, interface_pae=2.0),
        peptide_len=4)
    bad = compute_interface_metrics(
        _synth_full_data(tmp_path, interface_pae=18.0), peptide_len=4)
    assert good.mean_interface_pae < bad.mean_interface_pae
    assert good.min_interface_pae < bad.min_interface_pae
    assert good.ipsae > bad.ipsae


def test_interface_metrics_plddt_scale_is_0_100(tmp_path):
    m = compute_interface_metrics(
        _synth_full_data(tmp_path, plddt_pep=[0.30] * 4), peptide_len=4)
    assert 29.0 < m.binder_plddt < 31.0
    assert len(m.per_residue_plddt) == 4


def test_ipsae_d0_floor_short_interfaces():
    """Short interfaces clamp n at 27: d0 never shrinks below ~1.04."""
    from peplm.score.interface import _calc_d0
    small = _calc_d0(1)
    assert small == pytest.approx(_calc_d0(27), abs=1e-9)
    assert small > 1.0
    assert _calc_d0(200) > _calc_d0(50) > small


# ---------------------------------------------------------- peptide_reward
def _metrics(iPAE, plddt, per_plddt, ipsae=0.0):
    m = InterfaceMetrics()
    m.mean_interface_pae = iPAE
    m.min_interface_pae = iPAE
    m.ipsae = ipsae
    m.binder_plddt = plddt
    m.lowq_plddt = min(per_plddt)
    n = len(per_plddt)
    m.per_residue_plddt = list(per_plddt)
    m.per_residue_ipae = [iPAE] * n
    m.per_residue_contact = [0.0] * n
    m.n_peptide_tokens = n
    return m


def test_relative_reward_has_gradient_when_all_bad():
    """The old absolute gates zeroed everything; relative must not."""
    records = [
        {"metrics": _metrics(15.0, 30.0, [30, 31, 29, 30]), "nonlocal_contacts": 0},
        {"metrics": _metrics(16.0, 31.0, [31, 30, 30, 31]), "nonlocal_contacts": 0},
        {"metrics": _metrics(17.0, 29.0, [29, 30, 28, 29]), "nonlocal_contacts": 0},
        {"metrics": _metrics(18.0, 28.0, [28, 29, 27, 28]), "nonlocal_contacts": 0},
    ]
    RelativeReward().score_round(records)
    rewards = [r["rewards"][0] for r in records]
    assert max(rewards) > 0 > min(rewards)          # spread, both signs
    assert records[0]["rewards"][0] > records[3]["rewards"][0]


def test_relative_reward_failed_docks_below_worst():
    records = [
        {"metrics": _metrics(15.0, 30.0, [30, 31, 29, 30]), "nonlocal_contacts": 0},
        {"metrics": _metrics(16.0, 31.0, [31, 30, 30, 31]), "nonlocal_contacts": 0},
        {"metrics": _metrics(17.0, 29.0, [29, 30, 28, 29]), "nonlocal_contacts": 0},
        {"metrics": None, "error": "timeout"},
    ]
    RelativeReward().score_round(records)
    failed = records[3]["rewards"]
    ok_min = min(min(r["rewards"]) for r in records[:3])
    assert all(f < ok_min for f in failed)
    assert len(records[3]["credit"]) == 0


def test_residue_credit_blames_the_worst_positions():
    m = _metrics(10.0, 50.0, [80, 20, 75, 15])
    m.per_residue_ipae = [5.0, 18.0, 6.0, 20.0]
    c = residue_credit(m)
    assert c[1] > c[0] and c[3] > c[2]
    assert c.max() <= 1.0 and c.min() >= 0.0


def test_eligibility_filters_degenerate_sequences():
    assert not sequence_is_eligible("TTTTTTTTTTTTTT")   # homopolymer
    assert not sequence_is_eligible("AKAKAKAKAKAKAK")   # low entropy
    assert sequence_is_eligible("ACDEFGHIKLMNPQR")


# ------------------------------------------------------------- weak_positions
def test_weak_positions_relative_not_absolute():
    """All-bad credit (the cold-policy regime) still spares the best."""
    credit = [0.6, 0.9, 0.7, 0.95, 0.65, 0.8]
    weak = ESM3Policy.weak_positions(credit, max_frac=0.34)
    assert 1 in weak and 3 in weak          # the two worst
    assert 0 not in weak and 2 not in weak


def test_weak_positions_never_empties_a_peptide():
    weak = ESM3Policy.weak_positions([0.9, 0.9], max_frac=1.0)
    assert weak == [0]                      # keep >= 1 anchor context


# ------------------------------------------------------------- credit wiring
def _rollout(credit, pep_start, revealed, adv_sign):
    ro = Rollout(states=[np.zeros(2)] * len(revealed),
                 revealed=revealed,
                 old_logprob=[-1.0] * len(revealed),
                 temps=[1.0] * len(revealed),
                 rewards=(adv_sign,), group="g",
                 credit=list(credit), pep_start=pep_start)
    return ro


def test_flatten_events_credit_direction():
    """Negative advantage -> blame-weighted; positive -> (1-blame)-weighted."""
    credit = [1.0, 0.0]
    revealed = [(10, 5), (11, 6)]           # pep_start=10, credit[0], credit[1]

    events = MaskedGRPOUpdater._flatten_events(
        [_rollout(credit, 10, revealed, -1.0)], [-1.0])
    w = [e[5] / -1.0 for e in events]       # token weight = event adv / seq adv
    assert w[0] > w[1]                      # bad sequence: blamed pos absorbs more

    events = MaskedGRPOUpdater._flatten_events(
        [_rollout(credit, 10, revealed, 1.0)], [1.0])
    w = [e[5] / 1.0 for e in events]
    assert w[1] > w[0]                      # good sequence: credit the strong pos


def test_flatten_events_weights_mean_one():
    """Credit redistributes gradient; it must not rescale the update."""
    credit = [1.0, 0.5, 0.0, 0.25]
    revealed = [(7, 1), (8, 2), (9, 3), (10, 4)]
    events = MaskedGRPOUpdater._flatten_events(
        [_rollout(credit, 7, revealed, -1.0)], [-2.0])
    ws = [e[5] / -2.0 for e in events]
    assert abs(sum(ws) / len(ws) - 1.0) < 1e-9


def test_token_weight_strict_outside_peptide():
    """Out-of-range credit lookup is a wiring bug, not a silent default."""
    ro = _rollout([1.0, 1.0], 10, [(10, 1), (11, 2)], 1.0)
    assert ro.token_weight(10) == 1.0       # inside, full blame
    with pytest.raises(ValueError, match="outside the peptide span"):
        ro.token_weight(3)                  # misaligned pep_start


def test_token_weight_uniform_without_credit():
    """No credit vector (e.g. failed dock) -> uniform weights."""
    import types
    ro = Rollout(states=[], revealed=[(10, 1)], old_logprob=[-1.0],
                 temps=[1.0], rewards=(0.0, 0.0, 0.0), group="g",
                 credit=[], pep_start=10)
    assert ro.token_weight(10) == 1.0
    assert ro.token_weight(3) == 1.0


# --------------------------------------------------------------- GRPO groups
def test_group_advantage_zero_for_singleton():
    up = MaskedGRPOUpdater.__new__(MaskedGRPOUpdater)   # no policy needed
    assert up._group_advantage([0.7]) == [0.0]
    advs = up._group_advantage([0.2, 0.6])
    assert advs[1] > 0 > advs[0] and abs(sum(advs)) < 1e-12


# ------------------------------------------------------------ oracle staging
def _write_template(path, pep_slots=6):
    import gemmi

    def _res(name, seq, x):
        res = gemmi.Residue()
        res.name = name
        res.seqid = gemmi.SeqId(seq, " ")
        at = gemmi.Atom()
        at.name = "CA"
        at.element = gemmi.Element("C")
        at.pos = gemmi.Position(x, 0.0, 0.0)
        res.add_atom(at)
        return res

    st = gemmi.Structure()
    model = gemmi.Model("1")
    rec = gemmi.Chain("A")
    for i in range(30):
        rec.add_residue(_res("GLY", i + 162, i * 3.8))   # author numbering
    pep = gemmi.Chain("B")
    for i in range(pep_slots):
        pep.add_residue(_res("GLY", i + 1, 100.0 + i * 3.8))
    model.add_chain(rec)
    model.add_chain(pep)
    st.add_model(model)
    st.setup_entities()
    st.write_pdb(str(path))
    return path


def test_stage_complex_trims_instead_of_padding(tmp_path):
    from peplm.oracle.esm3_oracle import stage_complex
    tpl = _write_template(tmp_path / "tpl.pdb", pep_slots=6)
    out = tmp_path / "staged.pdb"
    n = stage_complex(tpl, "ACDE", out, peptide_chain="B")
    assert n == 4
    import gemmi
    st = gemmi.read_structure(str(out))
    pep = next(c for c in st[0] if c.name == "B")
    assert len(pep) == 4
    assert [r.name for r in pep] == ["ALA", "CYS", "ASP", "GLU"]
    rec = next(c for c in st[0] if c.name == "A")
    assert rec[0].seqid.num == 162           # numbering untouched


def test_stage_complex_rejects_too_long(tmp_path):
    from peplm.oracle.esm3_oracle import stage_complex
    tpl = _write_template(tmp_path / "tpl.pdb", pep_slots=4)
    with pytest.raises(ValueError, match="exceeds"):
        stage_complex(tpl, "ACDEF", tmp_path / "s.pdb", peptide_chain="B")


# ------------------------------------------------------- loop dry-run (no GPU)
class _StubPolicy:
    """Interface-compatible ESM3Policy stand-in (no model, no GPU)."""
    import types as _types
    toks = _types.SimpleNamespace()

    def __init__(self, pep_len=8):
        import types
        self.types = types
        vocab = {c: i + 4 for i, c in enumerate("ACDEFGHIKLMNPQRSTVWY")}
        vocab["<cls>"] = 0
        vocab["<eos>"] = 2
        self.toks.sequence = types.SimpleNamespace(vocab=vocab)
        self.pep_len = pep_len
        self.saved = 0

    def _traj(self, seq, group_tag):
        import torch
        ids = [self.toks.sequence.vocab[c] for c in seq]
        states = [torch.zeros(1, len(ids) + 2) for _ in seq]
        revealed = [(2 + i, t) for i, t in enumerate(ids)]
        return self.types.SimpleNamespace(
            peptide_tokens=ids, states=states, revealed=revealed,
            old_logprob=[-1.0] * len(seq), temps=[0.9] * len(seq))

    def sample_batch(self, receptor, pep_len, n, keep, temperature=0.9,
                     num_steps=0, strategy="random"):
        import random as _r
        _StubPolicy.calls = getattr(_StubPolicy, "calls", 0) + 1
        rng = _r.Random(_StubPolicy.calls * 104729 + num_steps * 1000 + n)
        out = []
        for _ in range(keep):
            seq = "".join(rng.choice("ACDEFGHIKLMNPQRSTVWY")
                          for _ in range(pep_len))
            out.append(self._traj(seq, "denovo"))
        return out

    def refill(self, receptor, parent_tokens, remask, num_steps=4,
               temperature=0.7):
        import random as _r
        import torch
        _StubPolicy.calls = getattr(_StubPolicy, "calls", 0) + 1
        rng = _r.Random(_StubPolicy.calls * 7919
                        + (hash(tuple(parent_tokens)) & 0xFFFF))
        new = list(parent_tokens)
        for j in remask:
            aa = "ACDEFGHIKLMNPQRSTVWY"
            new[j] = self.toks.sequence.vocab[rng.choice(aa)]
        if new == list(parent_tokens):
            return None
        states = [torch.zeros(1, len(new) + 2) for _ in remask]
        revealed = [(2 + j, new[j]) for j in remask]
        return self.types.SimpleNamespace(
            peptide_tokens=new, states=states, revealed=revealed,
            old_logprob=[-1.0] * len(remask), temps=[temperature] * len(remask))

    def peptide_start(self, receptor):
        return 1 + len(receptor)

    def save_adapter(self, path):
        self.saved += 1

    def adapter_parameters(self):
        import torch
        return [torch.nn.Parameter(torch.zeros(1))]


def _fake_dock(results_spec):
    """Build a dock_batch stub whose metrics depend on the sequence."""
    from peplm.oracle.esm3_oracle import OracleResult

    def dock_batch(sequences, cfg, run_root, gpus, seed, parents=None,
                   log=print):
        out = []
        for i, seq in enumerate(sequences):
            if sequences.count(seq) > 1:
                out.append(OracleResult(seq, None, error="duplicate"))
                continue
            quality = (sum(ord(c) for c in seq) % 100) / 100.0
            m = InterfaceMetrics()
            m.mean_interface_pae = 20.0 - 10.0 * quality
            m.min_interface_pae = m.mean_interface_pae
            m.ipsae = quality
            m.binder_plddt = 30.0 + 40.0 * quality
            m.lowq_plddt = m.binder_plddt - 5
            m.per_residue_plddt = [30.0 + 40.0 * quality * ((i % 3) + 1) / 3
                                   for i in range(len(seq))]
            m.per_residue_ipae = [m.mean_interface_pae] * len(seq)
            m.per_residue_contact = [0.0] * len(seq)
            m.n_peptide_tokens = len(seq)
            out.append(OracleResult(seq, m, min_clash_dist=3.5,
                                    nonlocal_contacts=2))
        return out
    return dock_batch


def test_esm3_loop_round_wiring(tmp_path, monkeypatch):
    import types

    import peplm.loop.esm3_loop as loop_mod
    from peplm.loop.esm3_loop import ESM3Loop, LoopBudget
    from peplm.oracle.esm3_oracle import OracleConfig

    pol = _StubPolicy(pep_len=8)
    cfg = OracleConfig(template=tmp_path / "t.pdb", pocket="A:1")
    loop = ESM3Loop(pol, cfg, "MKTLLILAVF", tmp_path / "run",
                    pep_len=8, budget=LoopBudget(oracle_batch=8, oversample=16,
                                                 parents_kept=2,
                                                 refills_per_parent=3),
                    gpus=(0,), seed=7)

    captured: list = []          # rollouts the updater actually received
    def _capture_update(rollouts, log=print):
        captured.clear()
        captured.extend(rollouts)
        return {"stub": len(rollouts)}
    loop.updater = types.SimpleNamespace(update=_capture_update)
    monkeypatch.setattr(loop_mod, "dock_batch", _fake_dock(None))

    s1 = loop.round(1)
    # round 1: pure de novo group, every rollout carries the scored 3-tuple
    assert "skipped" not in s1 and s1["docked"] >= 6
    assert {ro.group for ro in captured} == {"denovo"}
    assert all(len(ro.rewards) == 3 and len(ro.credit) == 8
               for ro in captured)
    assert max(ro.rewards[0] for ro in captured) > 0    # reward reached GRPO
    assert len(s1["parents"]) == 2
    assert all(len(p["credit"]) == 8 for p in s1["parents"])
    hist = [json.loads(l) for l in
            (tmp_path / "run" / "history.jsonl").read_text().splitlines()]
    assert hist[-1]["round"] == 1 and len(hist[-1]["parents"]) == 2

    captured.clear()
    s2 = loop.round(2)
    # round 2: refill groups whole (>=2 members), de novo group alive,
    # budget never exceeded, rewards present on every rollout
    assert "skipped" not in s2
    group_sizes = {}
    for ro in captured:
        group_sizes[ro.group] = group_sizes.get(ro.group, 0) + 1
    assert group_sizes.get("denovo", 0) >= 2
    assert all(v >= 2 for k, v in group_sizes.items() if k != "denovo")
    assert len(captured) <= 8
    assert all(len(ro.rewards) == 3 for ro in captured)
    assert len(s2["parents"]) == 2
    assert loop.reward.memory.exact                  # registry populated


# ------------------------------------------- chain identification (audit #12-15)
def _full_data_with_chains(tmp_path, sizes, peptide_at, plddt_by_chain):
    """asym layout per ``sizes``; ``peptide_at`` indexes the true peptide."""
    n = sum(sizes)
    asym, plddt = [], []
    for ci, sz in enumerate(sizes):
        asym += [ci] * sz
        plddt += [plddt_by_chain[ci]] * sz
    pae = np.full((n, n), 20.0)
    np.fill_diagonal(pae, 0.2)
    atom_plddt, atom2tok = [], []
    for tok, v in enumerate(plddt):
        atom_plddt += [v, v]
        atom2tok += [tok, tok]
    data = {
        "token_pair_pae": pae.tolist(),
        "token_asym_id": asym,
        "contact_probs": np.zeros((n, n)).tolist(),
        "atom_plddt": atom_plddt,
        "atom_to_token_idx": atom2tok,
    }
    p = tmp_path / f"fd{sizes}{peptide_at}.json"
    p.write_text(json.dumps(data))
    return p


def test_chain_id_receptor_never_wins_on_length_collision(tmp_path):
    """Receptor (13 tokens) colliding with peptide_len must not be picked
    as the peptide (audit case: silent metric inversion)."""
    # receptor chain 0 (13 tokens, pLDDT 20), peptide chain 1 (13, pLDDT 95)
    f = _full_data_with_chains(tmp_path, [13, 13], peptide_at=1,
                               plddt_by_chain=[0.20, 0.95])
    m = compute_interface_metrics(f, peptide_len=13)
    assert m is not None and m.binder_plddt > 80.0   # peptide, not receptor


def test_chain_id_rejects_length_mismatch_loudly(tmp_path):
    """Staged length disagreeing with the caller returns None, not garbage
    (audit case: smallest-asym fallback picking the receptor)."""
    f = _full_data_with_chains(tmp_path, [13, 20], peptide_at=1,
                               plddt_by_chain=[0.20, 0.95])
    assert compute_interface_metrics(f, peptide_len=25) is None
    assert compute_interface_metrics(f, peptide_len=0) is None


def test_chain_id_multimer_receptor_pools_others(tmp_path):
    """Peptide staged LAST even with a two-chain receptor; others pool."""
    f = _full_data_with_chains(tmp_path, [30, 13, 4], peptide_at=2,
                               plddt_by_chain=[0.9, 0.9, 0.5])
    m = compute_interface_metrics(f, peptide_len=4)
    assert m is not None
    assert 45.0 < m.binder_plddt < 55.0     # the LAST chain, not a receptor
    assert m.n_peptide_tokens == 4
    # a middle chain matching the length is NOT the peptide
    assert compute_interface_metrics(f, peptide_len=13) is None


def test_chain_id_receptor_length_collision_is_loud(tmp_path):
    """Receptor coincidentally matching peptide_len must not be picked
    when the staged-last peptide has a different length (audit #13)."""
    f = _full_data_with_chains(tmp_path, [13, 20], peptide_at=1,
                               plddt_by_chain=[0.20, 0.95])
    assert compute_interface_metrics(f, peptide_len=13) is None


# ------------------------------------------------- clash-aware scoring (RANKL)
def _rec(ipae, plddt_list, clash, pairs, contacts=2):
    m = _metrics(ipae, sum(plddt_list) / len(plddt_list), plddt_list)
    m.per_residue_ipae = [ipae] * len(plddt_list)
    return {"metrics": m, "min_clash_dist": clash, "pairs_deep": pairs,
            "nonlocal_contacts": contacts}


def test_buried_pose_loses_to_clean_despite_better_pae():
    """RANKL smoke regression: 0.99 A overlap with PAE 3.6 must NOT outrank
    a clean pose at PAE 6.0."""
    buried = _rec(3.6, [70, 72, 71, 70], 0.99, 142)   # smoke sample 0
    clean = _rec(6.0, [70, 72, 71, 70], 3.5, 12)
    ok = _rec(9.0, [70, 72, 71, 70], 3.8, 8)
    RelativeReward().score_round([buried, clean, ok])
    assert clean["rewards"][0] > buried["rewards"][0]


def test_all_clashing_round_keeps_order_and_gradient():
    """Every candidate clashes (the cold-start regime): ordering still
    discriminates and the round is not starved."""
    a = _rec(4.0, [70, 72, 71, 70], 1.9, 90)
    b = _rec(5.0, [70, 72, 71, 70], 1.2, 130)
    c = _rec(6.0, [70, 72, 71, 70], 0.9, 150)
    RelativeReward().score_round([a, b, c])
    rb = [r["rewards"][0] for r in (a, b, c)]
    assert rb[0] > rb[1] > rb[2]           # less clash -> higher
    assert max(rb) > 0 > min(rb)


def test_best_sample_prefers_clean_tier(tmp_path, monkeypatch):
    """Clash-tier beats PAE within a tier when selecting the sample."""
    import peplm.oracle.esm3_oracle as O
    import types
    clean = O.OracleResult("AAAA", None, min_clash_dist=3.0)
    dirty = O.OracleResult("AAAA", None, min_clash_dist=0.99)
    # emulate the sample loop: fake glob over two samples
    class P:
        def __init__(self, stem): self.stem = stem
        def __lt__(self, other): return self.stem < other.stem
    m_dirty = InterfaceMetrics(); m_dirty.mean_interface_pae = 3.6
    m_clean = InterfaceMetrics(); m_clean.mean_interface_pae = 6.0
    fake_files = [P("x_full_data_sample_0"), P("x_full_data_sample_1")]
    import numpy as _np
    fake_metrics = {0: m_dirty, 1: m_clean}
    fake_geo = {0: {"min_clash_dist": 0.99, "pairs_deep": 142,
                    "nonlocal_contacts": 24, "caca_min": 1.9},
                1: {"min_clash_dist": 3.00, "pairs_deep": 12,
                    "nonlocal_contacts": 20, "caca_min": 3.7}}
    import re as _re
    class FakePath:
        def glob(self, pat):
            m = _re.search(r"_sample_(\d)", pat)
            idx = m.group(1) if m else None
            return iter([f for f in fake_files
                         if idx is None or f.stem.endswith(f"sample_{idx}")])
    monkeypatch.setattr(O, "compute_interface_metrics",
                        lambda full, peptide_len: fake_metrics[int(full.stem[-1])])
    monkeypatch.setattr(O, "_audit_geometry",
                        lambda cif, ch: fake_geo[int(cif.stem[-1])])
    pred = FakePath()
    best = O._best_sample("AAAA", pred, 4, "B")
    assert best.metrics.mean_interface_pae == 6.0    # clean tier won


def test_best_sample_prefers_stereo_clean_tier(tmp_path, monkeypatch):
    """Audit gap: the stereo tier (chir_mixed/omega_bad) had no test -- a
    broken-stereo sample at better ipae must lose to a clean one."""
    import peplm.oracle.esm3_oracle as O
    import types

    class P:
        def __init__(self, stem):
            self.stem = stem
        def __lt__(self, other):
            return self.stem < other.stem
    import re as _re
    m_dirty = type("M", (), {"mean_interface_pae": 3.0})()
    m_clean = type("M", (), {"mean_interface_pae": 6.0})()
    fake_files = [P("x_full_data_sample_0"), P("x_full_data_sample_1")]
    fake_metrics = {0: m_dirty, 1: m_clean}
    fake_geo = {
        0: {"min_clash_dist": 3.0, "pairs_deep": 0, "nonlocal_contacts": 20,
            "caca_min": 3.6, "chir_mixed": 2, "omega_bad": 1},
        1: {"min_clash_dist": 3.0, "pairs_deep": 0, "nonlocal_contacts": 18,
            "caca_min": 3.5, "chir_mixed": 0, "omega_bad": 0},
    }
    monkeypatch.setattr(O, "compute_interface_metrics",
                        lambda full, peptide_len: fake_metrics[int(full.stem[-1])])
    monkeypatch.setattr(O, "_audit_geometry",
                        lambda cif, ch: fake_geo[int(cif.stem[-1])])

    class FakePath:
        def glob(self, pat):
            m = _re.search(r"_sample_(\d)", pat)
            idx = m.group(1) if m else None
            return iter([f for f in fake_files
                         if idx is None or f.stem.endswith(f"sample_{idx}")])
    best = O._best_sample("AAAA", FakePath(), 4, "B")
    # clean stereo (sample 1) must win despite the worse ipae
    assert best.metrics.mean_interface_pae == 6.0
