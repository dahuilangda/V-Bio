"""Mol-1 上游基线回归：相对位置编码与 AF3 原公式逐位一致。

Mol-1 源码保持上游原始态（环肽环绕编码只在 protenix fork，
backend/tests/test_protenix_cyclic_relp.py 覆盖）。本文件钉住基线：
若有人改动 Mol-1 的 embedder，这里会偏离 AF3 bin 而失败。

运行环境需要 mol 依赖（torch/biotite）；完整运行：
    docker run --rm --entrypoint= -v /data/V-Bio:/workspace/vbio \\
        -e PYTHONPATH=/workspace/Mol-1 \\
        vbio-mol1-runtime:1.0.3 \\
        python3.12 -m pytest /workspace/vbio/capabilities/mol1/tests -v
"""
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("mol.model.modules.embedders")

from mol.model.modules.embedders import RelativePositionEncoding  # noqa: E402

R_MAX, S_MAX = 32, 2
POS_WIDTH = 2 * (R_MAX + 1)  # a_rel_pos 在 relp 拼接中的列宽


def feature_dict(n_tokens_by_chain):
    asym, res, ent, tok = [], [], [], []
    for c, n in n_tokens_by_chain:
        asym += [c] * n
        ent += [c] * n
        res += list(range(1, n + 1))
        tok += list(range(len(tok), len(tok) + n))
    return {
        "asym_id": torch.tensor(asym).long(),
        "residue_index": torch.tensor(res).long(),
        "entity_id": torch.tensor(ent).long(),
        "token_index": torch.tensor(tok).long(),
        "sym_id": torch.ones(len(asym)).long(),
    }


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


class TestAF3Baseline:
    def test_mixed_chains_matches_af3(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 15), (2, 120), (3, 8)])
        for index in ("residue", "token"):
            assert torch.equal(bins_of(model, x, index), reference_bins(x, index))

    def test_same_residue_token_bins_masked(self):
        """token bin 只在同链同残基内有值（共价修饰场景的 AF3 语义）。"""
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 15), (2, 120)])
        assert torch.equal(bins_of(model, x, "token"), reference_bins(x, "token"))

    def test_batch_dimension(self):
        model = RelativePositionEncoding(r_max=R_MAX, s_max=S_MAX)
        x = feature_dict([(1, 10), (2, 30)])
        for key in x:
            x[key] = x[key].unsqueeze(0).expand(2, *x[key].shape).contiguous()
        out = model.generate_relp(x)["relp"]
        assert out.shape[:3] == (2, 40, 40)
        for item in (0, 1):
            xi = {k: v[item] for k, v in x.items()}
            assert torch.equal(
                out[item][..., :POS_WIDTH].argmax(-1), reference_bins(xi, "residue"))
