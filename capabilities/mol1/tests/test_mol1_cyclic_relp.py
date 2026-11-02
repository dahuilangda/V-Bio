"""Mol-1 环肽相对位置编码与 cyclic 特征检测的单元测试。

覆盖两处改动：
  1. RelativePositionEncoding.generate_relp 的环绕偏移（残距 + token 双路）：
     非环输入与 AF3 原公式逐位一致（回归保证），环内偏移按环最短路径改写。
  2. Featurizer.get_cyclic_features 的首尾键检测。

运行环境需要 mol 依赖（torch/biotite）；完整运行：
    docker run --rm --entrypoint= -v /data/V-Bio:/workspace/vbio \\
        -e PYTHONPATH=/workspace/Mol-1 \\
        vbio-mol1-runtime:1.0.3 \\
        python3.12 -m pytest /workspace/vbio/capabilities/mol1/tests -v
"""
import numpy as np
import pytest

torch = pytest.importorskip("torch")
biotite_atom_array = pytest.importorskip("biotite.structure")
pytest.importorskip("mol.model.modules.embedders")

from mol.data.core.featurizer import Featurizer  # noqa: E402
from mol.data.tokenizer import Token, TokenArray  # noqa: E402
from mol.model.modules.embedders import RelativePositionEncoding  # noqa: E402

R_MAX, S_MAX = 32, 2
POS_WIDTH = 2 * (R_MAX + 1)  # a_rel_pos 在 relp 拼接中的列宽
TOK_SLICE = slice(POS_WIDTH, 2 * POS_WIDTH)  # a_rel_token 的列区间


def feature_dict(n_tokens_by_chain, cyclic_flags_by_chain=None):
    asym, res, ent, tok = [], [], [], []
    for c, n in n_tokens_by_chain:
        asym += [c] * n
        ent += [c] * n
        res += list(range(1, n + 1))
        tok += list(range(len(tok), len(tok) + n))
    d = {
        "asym_id": torch.tensor(asym).long(),
        "residue_index": torch.tensor(res).long(),
        "entity_id": torch.tensor(ent).long(),
        "token_index": torch.tensor(tok).long(),
        "sym_id": torch.ones(len(asym)).long(),
    }
    if cyclic_flags_by_chain is not None:
        flags = []
        for c, n in n_tokens_by_chain:
            flags += [cyclic_flags_by_chain.get(c, 0)] * n
        d["cyclic"] = torch.tensor(flags).long()
    return d


def reference_bins(x, index):
    """AF3 原公式的 bin（index: "residue" 或 "token"）。"""
    asym, ri, ti = x["asym_id"], x["residue_index"], x["token_index"]
    same_chain = (asym[..., :, None] == asym[..., None, :]).long()
    if index == "residue":
        d = torch.clip(ri[..., :, None] - ri[..., None, :] + R_MAX, 0, 2 * R_MAX)
        mask = same_chain
    else:
        same_res = (ri[..., :, None] == ri[..., None, :]).long()
        d = torch.clip(ti[..., :, None] - ti[..., None, :] + R_MAX, 0, 2 * R_MAX)
        mask = same_chain * same_res
    return (d * mask + (1 - mask) * (2 * R_MAX + 1)).long()


def bins_of(model, x, index):
    out = model.generate_relp(dict(x))["relp"]
    lo = 0 if index == "residue" else POS_WIDTH
    return out[..., lo:lo + POS_WIDTH].argmax(dim=-1)


class TestRegression:
    def test_no_cyclic_flag_matches_af3(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 15), (2, 120), (3, 8)])
        for index in ("residue", "token"):
            assert torch.equal(bins_of(model, x, index), reference_bins(x, index))

    def test_zero_cyclic_flag_matches_af3(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 15), (2, 120)], cyclic_flags_by_chain={1: 0, 2: 0})
        for index in ("residue", "token"):
            assert torch.equal(bins_of(model, x, index), reference_bins(x, index))


class TestCyclicWrap:
    def test_head_tail_pair_becomes_adjacent(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 10)], cyclic_flags_by_chain={1: 1})
        pos = bins_of(model, x, "residue")
        tok = bins_of(model, x, "token")
        # (1,10)：线性 -9；环上从 10 到 1 前向一步 -> +1
        assert pos[0, 9] == R_MAX + 1 and pos[9, 0] == R_MAX - 1
        assert tok[0, 9] == R_MAX + 1 and tok[9, 0] == R_MAX - 1

    def test_token_and_residue_bins_agree_on_ring(self):
        """肽链一残基一 token，环绕后两路偏移必须逐位一致。"""
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 14)], cyclic_flags_by_chain={1: 1})
        assert torch.equal(bins_of(model, x, "residue"), bins_of(model, x, "token"))

    def test_cross_chain_and_target_bins_untouched(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 12), (2, 200)], cyclic_flags_by_chain={1: 1, 2: 0})
        ref = reference_bins(x, "residue")
        bins = bins_of(model, x, "residue")
        assert torch.equal(bins[:12, 12:], ref[:12, 12:])
        assert torch.equal(bins[12:, 12:], ref[12:, 12:])

    def test_batch_dimension(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 10), (2, 30)], cyclic_flags_by_chain={1: 1, 2: 0})
        for key in x:
            x[key] = x[key].unsqueeze(0).expand(2, *x[key].shape).contiguous()
        x["cyclic"] = x["cyclic"].clone()
        x["cyclic"][1] = 0
        out = model.generate_relp(x)["relp"]
        assert out.shape[:3] == (2, 40, 40)
        # item1 无环 -> 与 AF3 线性 bin 一致
        x1 = {k: v[1] for k, v in x.items()}
        ref1 = reference_bins(x1, "residue")
        assert torch.equal(out[1][..., :POS_WIDTH].argmax(-1), ref1)

    def test_small_and_degenerate_rings(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x3 = feature_dict([(1, 3)], cyclic_flags_by_chain={1: 1})
        assert bins_of(model, x3, "residue")[0, 2] == R_MAX + 1
        x1 = feature_dict([(1, 1)], cyclic_flags_by_chain={1: 1})
        out = model.generate_relp(x1)["relp"]
        assert out.shape == (1, 1, 4 * R_MAX + 2 * S_MAX + 7)

    def test_ring_larger_than_r_max_clips(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 200)], cyclic_flags_by_chain={1: 1})
        assert bins_of(model, x, "residue")[0, 100] == 0


# ------------------------------------------------------------ featurizer 检测

def make_atom_array(chains, mol_type="protein"):
    arr = None
    for c, n in chains:
        res_ids = np.repeat(np.arange(1, n + 1), 4)
        arr_ch = biotite_atom_array.AtomArray(len(res_ids))
        arr_ch.atom_name = ["N", "CA", "C", "O"] * n
        arr_ch.res_name = ["ALA"] * len(res_ids)
        arr_ch.element = ["N", "C", "C", "O"] * n
        arr_ch.res_id = res_ids
        arr_ch.set_annotation("asym_id_int",
                              np.full(len(res_ids), c, dtype=np.int32))
        arr_ch.set_annotation("mol_type", np.array([mol_type] * len(res_ids)))
        arr = arr_ch if arr is None else arr + arr_ch
    return arr


def token_array_one_per_residue(atom_array):
    ca = [i for i, name in enumerate(atom_array.atom_name) if name == "CA"]
    return TokenArray([Token(k, centre_atom_index=i) for k, i in enumerate(ca)])


def featurizer_for(chains, bonds=()):
    atom_array = make_atom_array(chains)
    if bonds:
        atom_array.bonds = biotite_atom_array.BondList(
            len(atom_array), np.array(bonds, dtype=np.int32))
    feat = Featurizer.__new__(Featurizer)
    feat.cropped_atom_array = atom_array
    feat.cropped_token_array = token_array_one_per_residue(atom_array)
    return feat


class TestCyclicFeatureDetection:
    def test_linear_chain_all_zero(self):
        feat = featurizer_for([(1, 5)])
        assert feat.get_cyclic_features()["cyclic"].tolist() == [0] * 5

    def test_head_tail_bond_flags_chain(self):
        # 骨架头尾酰胺契约：N(1)=索引 0 与 C(5)=索引 18
        feat = featurizer_for([(1, 5)], bonds=[(0, 18)])
        assert feat.get_cyclic_features()["cyclic"].tolist() == [1] * 5

    def test_internal_crosslink_not_flagged(self):
        feat = featurizer_for([(1, 6)], bonds=[(5, 13)])  # res2-res4 交联
        assert feat.get_cyclic_features()["cyclic"].tolist() == [0] * 6

    def test_mixed_chains_flagged_independently(self):
        # 链 1 首尾骨架键 N(1)-C(5)（索引 0-18）；链 2 的索引从 20 起
        feat = featurizer_for([(1, 5), (2, 7)], bonds=[(0, 18)])
        assert feat.get_cyclic_features()["cyclic"].tolist() == [1] * 5 + [0] * 7

    def test_no_bond_book(self):
        feat = featurizer_for([(1, 5)])
        feat.cropped_atom_array.bonds = None
        assert feat.get_cyclic_features()["cyclic"].tolist() == [0] * 5
