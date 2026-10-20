"""GRPO learn 的 KL 锚、熵奖励、多 epoch 与零优势跳过。

用最小桩 policy 端到端驱动 _learn: 梯度必须真实流过可训练参数, KL 项
必须把训练后的 logprob 拉向禁用 adapter 的基线, 零优势行必须被丢弃。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import torch
import torch.nn as nn

PEPLM_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PEPLM_ROOT), str(PEPLM_ROOT / "integrate")):
    if p not in sys.path:
        sys.path.insert(0, p)


class _StubModel(nn.Module):
    """一个可训练参数生成全部 logits 的最小模型——梯度流可断言。"""

    def __init__(self, vocab: int = 24, width: int = 4):
        super().__init__()
        self.vocab = vocab
        self.embed = nn.Embedding(vocab, width)
        self.head = nn.Linear(width, vocab)
        self.disable_adapter_calls = 0

    def disable_adapter(self):
        outer = self

        class _Ctx:
            def __enter__(self):
                outer.disable_adapter_calls += 1
                return None

            def __exit__(self, *exc):
                return False

        return _Ctx()

    def parameters(self):
        return list(self.embed.parameters()) + list(self.head.parameters())


class _StubPolicy:
    def __init__(self, n_ctx: int = 18, vocab: int = 24):
        self.device = "cpu"
        self.dtype = torch.float32
        self.vocab = vocab
        self.n_ctx = n_ctx
        self.model = _StubModel(vocab)
        self._token_of_letter = {aa: 4 + i for i, aa in enumerate("ACDEFGHIKLMNPQRSTVWY")}

    def encode_peptide(self, sequence: str, pep_len: int):
        if len(sequence) != pep_len:
            return None
        ids = [self._token_of_letter.get(aa) for aa in sequence]
        if any(v is None for v in ids):
            return None
        return ids

    def encode_context(self, receptor: str, pep_len: int, *, peptide_first=False):
        return torch.zeros(1, self.n_ctx, dtype=torch.long)

    def peptide_start(self, receptor: str, pep_len: int, *, peptide_first=False):
        return self.n_ctx - pep_len

    def encode_ss8(self, receptor: str, n: int, ss_profile: str, *, peptide_first=False):
        return None

    def _forward_sequence_logits(self, tokens: torch.Tensor, ss8=None):
        # logits 依赖可训练参数 → 梯度可断言; 形状 [1, T, vocab]
        import torch.nn.functional as _F
        return _F.linear(self.model.embed(tokens), self.model.head.weight)

    def save_adapter(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)


@pytest.fixture()
def learn_env(monkeypatch, tmp_path):
    """加载 worker 模块并注入桩 policy 与隔离的 adapter 根。"""
    import importlib

    # 桩掉 torch 之外的宿主依赖: worker 模块顶层只依赖 torch/peplm 路径
    monkeypatch.setenv("ESM3_ADAPTER_ROOT", str(tmp_path / "adapters"))
    spec = importlib.util.spec_from_file_location(
        "esm3_gpu_worker_under_test",
        PEPLM_ROOT / "peplm" / "integrate" / "_esm3_gpu_worker.py")
    mod = importlib.util.module_from_spec(spec)
    # _esm3_gpu_worker 顶层 import 了 peplm.integrate.ss_modes 与 peplm.models.esm3_policy,
    # 后者顶层依赖 torch/numpy(可用), 但会拼 ESM3 权重路径——只导入不实例化, 安全。
    spec.loader.exec_module(mod)

    policy = _StubPolicy()
    monkeypatch.setattr(mod, "policy", policy)
    monkeypatch.setattr(mod, "_ensure_adapter_loaded", lambda key: None)
    return mod, policy


def _rollouts(groups: dict[str, list[tuple[str, float]]]) -> list[dict]:
    out = []
    for group, members in groups.items():
        for i, (seq, reward) in enumerate(members):
            out.append({
                "sequence": seq,
                "reward": reward,
                "group": group,
                "plddts": [0.9 - 0.02 * j for j in range(len(seq))],
            })
    return out


SEQ_A = "ACDEFGHIKL"
SEQ_B = "MNPSTVRWYL"


def test_learn_takes_gradient_and_uses_kl(learn_env, tmp_path):
    mod, _ = learn_env
    req = {
        "rollouts": _rollouts({
            "g1": [(SEQ_A, 0.8), (SEQ_B, 0.2)],
            "g2": [(SEQ_B, 0.6), (SEQ_A, 0.1)],
        }),
        "receptor_sequence": "MKVL",
        "adapter_key": "peptide-l-test",
        "new_adapter_dir": str(tmp_path / "adapter-out"),
        "epochs": 2,
    }
    result = mod._learn(req)
    assert "skipped" not in result
    assert result["steps"] >= 2  # 4 rollouts / micro_batch 4 × 2 epochs
    assert result["beta_kl"] == pytest.approx(0.04)


def test_learn_skips_when_all_advantages_zero(learn_env):
    mod, _ = learn_env
    req = {"rollouts": _rollouts({"g1": [(SEQ_A, 0.5), (SEQ_B, 0.5)]})}
    result = mod._learn(req)
    assert result == {"skipped": "tokenization failed or all-zero advantage"}


def test_learn_requires_group_of_two(learn_env):
    mod, _ = learn_env
    req = {"rollouts": _rollouts({"solo": [(SEQ_A, 0.9)]})}
    assert mod._learn(req) == {"skipped": "no group with >= 2 members"}
