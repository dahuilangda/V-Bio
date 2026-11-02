"""Protenix 环肽相对位置编码与 cyclic 特征检测的单元测试。

覆盖两处改动：
  1. RelativePositionEncoding.generate_relp 的 cyclic offset（AfCycDesign 式
     环绕距离）：非环输入与 AF3 原公式逐位一致（回归保证），环内偏移按环
     最短路径改写，跨链与靶内 bin 不受影响。
  2. Featurizer.get_cyclic_features 的首尾键检测：链首尾残基存在共价键时
     该链所有 token 标记为 cyclic。

运行环境需要 protenix 依赖（optree/biotite/torch）。宿主机缺依赖时自动
跳过；完整运行：

    docker run --rm --entrypoint= \\
        -v /data/V-Bio:/workspace/vbio \\
        -e PYTHONPATH=/workspace/vbio/vendor/protenix-source \\
        vbio-protenix-v2-runtime:2.0.0 \\
        /usr/local/micromamba/envs/protenix/bin/python \\
        -m pytest /workspace/vbio/backend/tests/test_protenix_cyclic_relp.py -v
"""
import numpy as np
import pytest

torch = pytest.importorskip("torch")
biotite_atom_array = pytest.importorskip("biotite.structure")
pytest.importorskip("protenix.model.modules.embedders")

from protenix.data.core.featurizer import Featurizer  # noqa: E402
from protenix.data.tokenizer import Token, TokenArray  # noqa: E402
from protenix.model.modules.embedders import RelativePositionEncoding  # noqa: E402

R_MAX, S_MAX = 32, 2
POS_WIDTH = 2 * (R_MAX + 1)  # a_rel_pos 在 relp 拼接中的列宽
TOK_SLICE = slice(POS_WIDTH, 2 * POS_WIDTH)  # a_rel_token 的列区间


def make_atom_array(chains):
    """构造带 res_id/asym_id_int 注释的 AtomArray。

    chains: list[(asym_id_int, n_res)]，每残基 4 个原子（N/CA/C/O）。
    """
    atom_array = None
    for c, n in chains:
        res_ids = np.repeat(np.arange(1, n + 1), 4)
        names = ["N", "CA", "C", "O"] * n
        arr = biotite_atom_array.AtomArray(len(res_ids))
        arr.atom_name = names
        arr.res_name = ["ALA"] * len(res_ids)
        arr.element = ["N", "C", "C", "O"] * n
        arr.res_id = res_ids
        arr.set_annotation("asym_id_int", np.full(len(res_ids), c, dtype=np.int32))
        atom_array = arr if atom_array is None else atom_array + arr
    return atom_array


def token_array_one_per_residue(atom_array):
    """蛋白链每残基一个 token，centre_atom_index 指向 CA。"""
    ca_indices = [
        i for i, name in enumerate(atom_array.atom_name) if name == "CA"
    ]
    tokens = [
        Token(k, centre_atom_index=idx) for k, idx in enumerate(ca_indices)
    ]
    return TokenArray(tokens)


def featurizer_for(chains, bonds=()):
    """直接装配 Featurizer 的两个输入（绕过完整 tokenizer 流程）。"""
    atom_array = make_atom_array(chains)
    if bonds:
        atom_array.bonds = biotite_atom_array.BondList(
            len(atom_array), np.array(bonds, dtype=np.int32)
        )
    feat = Featurizer.__new__(Featurizer)
    feat.cropped_atom_array = atom_array
    feat.cropped_token_array = token_array_one_per_residue(atom_array)
    return feat


def feature_dict(n_tokens_by_chain, cyclic_flags_by_chain=None):
    """构造 generate_relp 输入：链列表 (asym, n)，residue_index 从 1 起。"""
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


def reference_relp_bins(input_feature_dict):
    """AF3 原公式的 d_residue bin，作为回归基准。"""
    asym = input_feature_dict["asym_id"]
    ri = input_feature_dict["residue_index"]
    same_chain = (asym[..., :, None] == asym[..., None, :]).long()
    d = torch.clip(ri[..., :, None] - ri[..., None, :] + R_MAX, 0, 2 * R_MAX)
    return (d * same_chain + (1 - same_chain) * (2 * R_MAX + 1)).long()


def relp_pos_bins(model, input_feature_dict):
    out = model.generate_relp(dict(input_feature_dict))
    return out["relp"][..., :POS_WIDTH].argmax(dim=-1)


class TestRegression:
    def test_no_cyclic_flag_matches_af3(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 15), (2, 120), (3, 8)])
        bins = relp_pos_bins(model, x)
        assert torch.equal(bins, reference_relp_bins(x))
        # a_rel_pos 之外的块（token/chain/same_entity）也须完整
        full = model.generate_relp(dict(x))["relp"]
        assert full.shape[-1] == 4 * R_MAX + 2 * S_MAX + 7

    def test_zero_cyclic_flag_matches_af3(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 15), (2, 120)], cyclic_flags_by_chain={1: 0, 2: 0})
        bins = relp_pos_bins(model, x)
        assert torch.equal(bins, reference_relp_bins(x))


class TestCyclicWrap:
    def test_head_tail_pair_becomes_adjacent(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 10)], cyclic_flags_by_chain={1: 1})
        bins = relp_pos_bins(model, x)
        # (1,10): 线性 -9；环上从 10 到 1 前向一步 -> +1
        assert bins[0, 9] == R_MAX + 1
        assert bins[9, 0] == R_MAX - 1

    def test_mid_ring_pairs_unchanged(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 10)], cyclic_flags_by_chain={1: 1})
        bins = relp_pos_bins(model, x)
        # (1,5): 线性 -4 即环上最短路径，保持不变
        assert bins[0, 4] == R_MAX - 4
        # (1,7): 线性 -6，环上 +4
        assert bins[0, 6] == R_MAX + 4

    def test_wrap_pair_count(self):
        """改写对 = |d|>N/2 双向 + 半环平局对（+N/2 映射到 -N/2）。"""
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        n = 10
        x = feature_dict([(1, n)], cyclic_flags_by_chain={1: 1})
        ref = reference_relp_bins(x)
        bins = relp_pos_bins(model, x)
        expected = 2 * sum(n - k for k in range(n // 2 + 1, n)) + n // 2
        assert int((bins != ref).sum()) == expected

    def test_cross_chain_and_target_bins_untouched(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 12), (2, 200)], cyclic_flags_by_chain={1: 1, 2: 0})
        ref = reference_relp_bins(x)
        bins = relp_pos_bins(model, x)
        assert torch.equal(bins[:12, 12:], ref[:12, 12:])
        assert torch.equal(bins[12:, 12:], ref[12:, 12:])
        assert bool((ref[:12, 12:] == 2 * R_MAX + 1).all())

    def test_batch_dimension(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 10), (2, 30)], cyclic_flags_by_chain={1: 1, 2: 0})
        for key in x:
            x[key] = x[key].unsqueeze(0).expand(2, *x[key].shape).contiguous()
        x["cyclic"] = x["cyclic"].clone()
        x["cyclic"][1] = 0  # 第二个样本视为全线性
        out = model.generate_relp(x)["relp"]
        assert out.shape[:3] == (2, 40, 40)
        x0 = {k: v[0] for k, v in x.items()}
        x1 = {k: v[1] for k, v in x.items()}
        # item0 携带环标记 -> 与 AF3 线性 bin 不同；item1 无环 -> 与 AF3 一致
        assert not torch.equal(
            out[0][..., :POS_WIDTH].argmax(-1), reference_relp_bins(x0)
        )
        assert torch.equal(
            out[1][..., :POS_WIDTH].argmax(-1), reference_relp_bins(x1)
        )

    def test_small_and_degenerate_rings(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x3 = feature_dict([(1, 3)], cyclic_flags_by_chain={1: 1})
        assert relp_pos_bins(model, x3)[0, 2] == R_MAX + 1
        x1 = feature_dict([(1, 1)], cyclic_flags_by_chain={1: 1})
        out = model.generate_relp(x1)["relp"]
        assert out.shape == (1, 1, 4 * R_MAX + 2 * S_MAX + 7)

    def test_multi_token_residue_ring_uses_residue_modulus(self):
        """环链含逐原子 token 化的修饰残基（N_tok=18 > N_res=14）时，
        残距环绕必须用残基计数做模数（NCAA 环肽场景）。"""
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 14)], cyclic_flags_by_chain={1: 1})
        # 残基 7 拆成 5 个 token：residue_index 重复，token_index 连续递增
        extra = 5 - 1
        ri = torch.cat([
            x["residue_index"][:6],
            torch.full((5,), 7, dtype=torch.long),
            x["residue_index"][7:],
        ])
        ti = torch.arange(14 + extra)
        asym = torch.full((14 + extra,), 1, dtype=torch.long)
        ent = torch.ones(14 + extra, dtype=torch.long)
        cyc = torch.ones(14 + extra, dtype=torch.long)
        xx = {"asym_id": asym, "residue_index": ri, "entity_id": ent,
              "token_index": ti, "sym_id": torch.ones(len(asym)).long(),
              "cyclic": cyc}
        bins = relp_pos_bins(model, xx)
        n = 14 + extra
        # 首尾残基对：token 0（res1）与 token 13（res14，最后一根）
        # 残距环绕按 N_res=14：d(1,14)=-13 -> 环上 +1 -> bin R_MAX+1
        assert bins[0, n - 1] == R_MAX + 1
        assert bins[n - 1, 0] == R_MAX - 1

    def test_ring_larger_than_r_max_clips(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 200)], cyclic_flags_by_chain={1: 1})
        bins = relp_pos_bins(model, x)
        assert bins[0, 100] == 0  # 环距 -100 clip 到最低 bin


class TestTokenWrap:
    """a_rel_token 与 a_rel_pos 同规则环绕，两路偏移信号不得相互矛盾。"""

    def token_bins(self, model, x):
        out = model.generate_relp(dict(x))["relp"]
        return out[..., TOK_SLICE].argmax(dim=-1)

    def reference_token_bins(self, x):
        """AF3 原公式的 d_token bin。"""
        asym = x["asym_id"]
        ri = x["residue_index"]
        ti = x["token_index"]
        same_chain = (asym[..., :, None] == asym[..., None, :]).long()
        same_res = (ri[..., :, None] == ri[..., None, :]).long()
        d = torch.clip(ti[..., :, None] - ti[..., None, :] + R_MAX, 0, 2 * R_MAX)
        return (d * same_chain * same_res
                + (1 - same_chain * same_res) * (2 * R_MAX + 1)).long()

    def test_linear_chain_token_bins_match_af3(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 15), (2, 120)], cyclic_flags_by_chain={1: 0, 2: 0})
        assert torch.equal(self.token_bins(model, x), self.reference_token_bins(x))

    def test_head_tail_token_bin_wraps(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 10)], cyclic_flags_by_chain={1: 1})
        bins = self.token_bins(model, x)
        # (1,10)：线性 token 距 -9，环绕后 +1（与残距环绕同向同值）
        assert bins[0, 9] == R_MAX + 1
        assert bins[9, 0] == R_MAX - 1

    def test_mid_ring_token_bins_unchanged(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 10)], cyclic_flags_by_chain={1: 1})
        bins = self.token_bins(model, x)
        assert bins[0, 4] == R_MAX - 4
        assert bins[0, 6] == R_MAX + 4

    def test_token_and_residue_bins_agree_on_ring(self):
        """肽链一残基一 token，环绕后两路偏移必须逐位一致。"""
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 14)], cyclic_flags_by_chain={1: 1})
        pos = relp_pos_bins(model, x)
        tok = self.token_bins(model, x)
        # 对角块（同链）内 a_rel_pos 与 a_rel_token 相同（跨链/异链 bin 值不同）
        assert torch.equal(pos, tok)

    def test_cross_chain_token_bins_untouched(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 12), (2, 200)], cyclic_flags_by_chain={1: 1, 2: 0})
        ref = self.reference_token_bins(x)
        bins = self.token_bins(model, x)
        assert torch.equal(bins[:12, 12:], ref[:12, 12:])
        assert torch.equal(bins[12:, 12:], ref[12:, 12:])


class TestCyclicFeatureDetection:
    def test_linear_chain_all_zero(self):
        feat = featurizer_for([(1, 5)])
        assert feat.get_cyclic_features()["cyclic"].tolist() == [0] * 5

    def test_head_tail_bond_flags_chain(self):
        # 每残基 4 原子：res1 N=0 CA=1 C=2 O=3; res5 N=16 ... C=18
        feat = featurizer_for([(1, 5)], bonds=[(2, 18)])  # C(1)-C(5)
        assert feat.get_cyclic_features()["cyclic"].tolist() == [1] * 5

    def test_head_tail_bond_via_backbone_n(self):
        feat = featurizer_for([(1, 5)], bonds=[(0, 18)])  # N(1)-C(5)
        assert feat.get_cyclic_features()["cyclic"].tolist() == [1] * 5

    def test_mixed_chains_flagged_independently(self):
        chains = [(1, 5), (2, 7)]
        bonds = [(2, 18)]  # 仅链 1 首尾成环
        feat = featurizer_for(chains, bonds=bonds)
        assert feat.get_cyclic_features()["cyclic"].tolist() == [1] * 5 + [0] * 7

    def test_internal_crosslink_not_flagged(self):
        # res2-res4 二硫键式交联不构成首尾闭环
        feat = featurizer_for([(1, 6)], bonds=[(5, 13)])
        assert feat.get_cyclic_features()["cyclic"].tolist() == [0] * 6

    def test_single_residue_chain_not_flagged(self):
        feat = featurizer_for([(1, 1)], bonds=[(0, 2)])
        assert feat.get_cyclic_features()["cyclic"].tolist() == [0]

    def test_no_bond_book(self):
        feat = featurizer_for([(1, 5)])
        feat.cropped_atom_array.bonds = None
        assert feat.get_cyclic_features()["cyclic"].tolist() == [0] * 5


class TestEndToEndWrap:
    """检测与编码两端衔接：真实 AtomArray 检测结果直接驱动 relp 环绕。"""

    def test_flag_drives_wrap(self):
        feat = featurizer_for([(1, 5), (2, 8)], bonds=[(2, 18)])
        cyclic = feat.get_cyclic_features()["cyclic"]
        assert cyclic.tolist() == [1] * 5 + [0] * 8

        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 5), (2, 8)])
        x["cyclic"] = cyclic.long()
        bins = relp_pos_bins(model, x)
        assert bins[0, 4] == R_MAX + 1  # 链 1 首尾相邻
        assert bins[5 + 0, 5 + 7] == R_MAX - 7  # 链 2 仍为线性
