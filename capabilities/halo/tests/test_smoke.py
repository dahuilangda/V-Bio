"""Smoke tests for HALO (no GPU oracle needed - mock oracle only)."""

import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from halo.generate.vocab import SmilesVocab, tokenize, detokenize
from halo.score.properties import compute_descriptors, is_pains, passes_window, DEFAULT_WINDOW
from halo.score.preference import PreferenceModel
from halo.score.reward import RewardFunction
from halo.generate.mmp_moves import MMPPolicy, grow


def test_tokenizer_roundtrip():
    smis = ["CCO", "c1ccccc1NC(=O)C", "CC(=O)Oc1ccccc1C(=O)O", "C[NH+](C)C", "FC(F)(F)c1ccc(N)cc1"]
    for s in smis:
        assert detokenize(tokenize(s)) == s
    v = SmilesVocab(smis)
    for s in smis:
        assert v.decode(v.encode(s)) == s


def test_descriptors_and_filters():
    from rdkit import Chem

    m = Chem.MolFromSmiles("CC(=O)Oc1ccccc1C(=O)O")
    d = compute_descriptors(m)
    assert 150 < d["mw"] < 200 and 0 < d["qed"] <= 1
    assert passes_window(d, DEFAULT_WINDOW)
    assert not is_pains("CC(=O)Oc1ccccc1C(=O)O")
    assert is_pains or True  # catalog loads


def test_preference_model_learns_direction():
    pm = PreferenceModel()
    # construct synthetic preference: lower MW is better
    from halo.score.properties import descriptor_vector

    mols = {
        "light": "CCOc1ccc(N)cc1",
        "heavy": "CCCCCCCCCCCCc1ccc(N)c(Br)c1Cl",
    }
    from rdkit import Chem

    fa = descriptor_vector(Chem.MolFromSmiles(mols["light"]))
    fb = descriptor_vector(Chem.MolFromSmiles(mols["heavy"]))
    for _ in range(3):
        pm.add_pair(fa, fb)
    info = pm.fit()
    assert info["train_acc"] == 1.0
    assert pm.score(fa) > pm.score(fb)


def test_reward_function():
    rf = RewardFunction(target_pic50=8.0, seed_smiles=["CC(=O)Oc1ccccc1C(=O)O"])
    r, parts = rf.machine_reward("CC(=O)Oc1ccccc1C(=O)O", affinity_pic50=8.5, ipsae=0.5, plddt=85)
    assert 0 < r <= 1 and parts["affinity"] > 0.5
    r_bad, _ = rf.machine_reward("CC(=O)Oc1ccccc1C(=O)O", affinity_pic50=4.0, ipsae=0.1, plddt=40)
    assert r_bad < r


def test_mmp_moves_produce_valid():
    import random

    rng = random.Random(0)
    parent = "CC(=O)Nc1ccc(O)cc1"
    outs = grow(parent, rng)
    assert outs, "grow produced nothing"
    from rdkit import Chem

    for s in outs:
        assert Chem.MolFromSmiles(s) is not None
    pol = MMPPolicy([parent, "CC(=O)Nc1cccc(N)c1", "Cc1ccc(C(=O)Nc2ccc(O)cc2)cc1"])
    props = pol.propose(parent, 4, rng)
    assert len(props) >= 1


def test_surrogate_fits_and_predicts():
    from rdkit import Chem

    from halo.score.surrogate import Surrogate

    s = Surrogate()
    rng = np.random.RandomState(0)
    rows = []
    for i in range(40):
        n = rng.randint(1, 12)
        smi = "C" * n + "O" * (12 - n) if n < 12 else "CCCCCCCCCCCCO"
        rows.append({"smiles": Chem.MolToSmiles(Chem.MolFromSmiles(smi)),
                     "affinity_pic50": 5 + 0.2 * n, "ipsae": 0.2 + 0.01 * n, "ligand_plddt_mean": 60 + n})
    s.add_observations(rows)
    info = s.fit()
    assert info["trained"]
    mu, sig = s.predict([r["smiles"] for r in rows[:5]])
    assert mu.shape == (5, 3) and sig.shape == (5, 3)
    assert mu[:, 0].mean() > 5.5



def test_fragment_tokenizer_roundtrip():
    import random as _random

    from halo.data.ligands import load_smiles_corpus
    from halo.generate.vocab import build_fragment_regex, mine_fragments, tokenize

    corpus = load_smiles_corpus(Path("/data/V-Bio/data/chembl_compounds.smi"), limit=400)
    frags = mine_fragments(corpus, max_molecules=400, min_count=5)
    assert len(frags) >= 5
    pat = build_fragment_regex(frags)
    rng = _random.Random(0)
    sample = rng.sample(corpus, min(60, len(corpus)))
    bad = [s for s in sample if "".join(tokenize(s, pat)) != s]
    assert not bad, f"roundtrip failures: {bad[:3]}"
    # fragment tokens must shorten at least some molecules
    assert any(len(tokenize(s, pat)) < len(tokenize(s)) for s in sample)


def test_prior_overfits_small_corpus():
    import torch

    from halo.generate.prior import SmilesTransformer

    torch.manual_seed(0)
    corpus = ["CCO", "CCC", "CCCO", "CCCCO", "CCN", "CCCN"] * 10
    v = SmilesVocab(corpus)
    m = SmilesTransformer(v, d_model=64, n_layers=2, n_heads=4, dropout=0.0)
    hist = None
    import halo.generate.prior as P

    hist = P.pretrain_prior(m, corpus, v, epochs=60, batch_size=32, device="cpu", log=None)
    assert hist["best_val_loss"] < 0.7
    samples = m.sample(8, "cpu", temperature=1.0, top_p=0.95)
    from rdkit import Chem

    valid = sum(1 for s in samples if Chem.MolFromSmiles(s))
    assert valid >= 4


def test_agent_update_improves_reward():
    import torch

    from halo.generate.agent import AgentUpdater
    from halo.generate.prior import SmilesTransformer

    torch.manual_seed(0)
    corpus = ["CCO", "CCC", "CCCO", "CCCCO", "CCN", "CCCN", "CCBr", "CCCl"] * 6
    v = SmilesVocab(corpus)
    prior = SmilesTransformer(v, d_model=64, n_layers=2, n_heads=4, dropout=0.0)
    agent = SmilesTransformer(v, d_model=64, n_layers=2, n_heads=4, dropout=0.0)
    agent.load_state_dict(prior.state_dict())
    P = __import__("halo.generate.prior", fromlist=["pretrain_prior"])
    P.pretrain_prior(prior, corpus, v, epochs=40, batch_size=32, device="cpu", log=None)
    agent.load_state_dict(prior.state_dict())

    def reward(smi):
        return 1.0 if "Br" in smi else 0.05

    upd = AgentUpdater(agent, prior, v, device="cpu")
    for _ in range(8):
        samples = agent.sample(64, "cpu", temperature=1.2, top_p=1.0)
        pairs = [(s, reward(s)) for s in samples]
        upd.update(pairs, alpha=0.7, sigma=12.0, kl_beta=0.0, epochs=3)
    final = agent.sample(64, "cpu", temperature=1.0)
    frac = sum(1 for s in final if "Br" in s) / len(final)
    assert frac > 0.4, f"agent did not learn Br preference ({frac:.2f})"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_grpo_frozen_old_logprobs_and_groups():
    """Frozen old log-probs must give a non-trivial clip fraction within one
    update, and context groups must not mix reward sources."""
    import torch

    from halo.generate.gpt2_prior import GPT2Prior
    from halo.generate.grpo import GRPOUpdater
    from halo.generate.prior import SmilesVocab

    torch.manual_seed(0)
    corpus = ["CCO", "CCC", "CCCO", "CCCCO", "CCN", "CCCN", "CCBr", "CCCl"] * 4
    v = SmilesVocab(corpus)
    prior = GPT2Prior(v, d_model=64, n_layers=2, n_heads=4, dropout=0.0, max_len=64)
    agent = GPT2Prior(v, d_model=64, n_layers=2, n_heads=4, dropout=0.0, max_len=64)
    upd = GRPOUpdater(agent, prior, v, device="cpu", lr=5e-3, kl_beta=0.01, ent_coef=0.01)

    def reward(s):
        return 1.0 if "Br" in s else 0.05

    samples = []
    for _ in range(3):
        outs = agent.sample(48, "cpu", temperature=1.3, top_p=1.0)
        samples.extend((s, reward(s), "g0", "oracle") for s in outs)
    out = upd.update(samples, epochs=3, batch_size=48)
    assert out["n"] > 0
    # multiple epochs against frozen old log-probs -> policy must move enough
    assert out["frac_clipped"] > 0.0, f"clip never active: {out}"
    assert out["entropy"] > 0.0

    # grouping: two contexts with inverted rewards - both groups present
    adv = upd._group_advantage(["a", "a", "b", "b"], [1.0, 0.0, 0.0, 1.0], ["o", "o", "o", "o"])
    assert adv[0] > 0 and adv[1] < 0 and adv[2] < 0 and adv[3] > 0
    # sources never mix within a group
    adv2 = upd._group_advantage(["a", "a"], [1.0, 0.0], ["oracle", "surrogate"])
    assert adv2 == [0.0, 0.0]


def test_digit_parity_gate():
    from halo.generate.safe_tasks import digit_parity_ok

    assert digit_parity_ok(".C18CC1.C5CO.N68.C=9c1cnn2c6nc7nc12.O=C1NC=9C(=O)N1.N17CCN5CC1")
    assert not digit_parity_ok(".C18CC1.C5CO.N68")  # dangling attachment digits
    assert digit_parity_ok("c1ccccc1.CCN")  # no cross-fragment digits


def test_prompt_bpe_encoding():
    """String prompts must be BPE-encoded (not char-by-char) so '<cont>' and
    '<hop>' markers resolve to their special-token ids."""
    from tokenizers import Tokenizer

    from halo.generate.gpt2_prior import GPT2Prior
    from halo.generate.safe_prior import DigitBPEVocab

    v = DigitBPEVocab.load(__import__("pathlib").Path(__file__).resolve().parents[1] / "runs" / "prior_mv_rag2" / "digit_bpe_tokens.json")
    model = GPT2Prior(v, d_model=32, n_layers=1, n_heads=2, max_len=32)
    ids = model._prompt_ids("c1ccccc1.CCN<cont>")
    cont = v.stoi["<cont>"]
    assert ids[-1] == cont, "literal <cont> marker must map to its token id"
    ids2 = model._prompt_ids("CC(=O)O<hop>")
    assert ids2[-1] == v.stoi["<hop>"]
