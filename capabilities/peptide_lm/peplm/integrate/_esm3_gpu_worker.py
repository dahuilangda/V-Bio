"""GPU subprocess for the ESM3 HTTP service — owns the CUDA context.

The pool's admission gate rejects any GPU with >200MiB of compute memory,
and a torch CUDA context in a long-lived process holds ~400-500MB that
cannot be released without ending the process. So ALL CUDA work lives in
this subprocess: the HTTP parent stays GPU-free, spawns one worker per
acquired card, and on idle kills it — the context dies with the process and
the card returns truly empty for the next claimant.

Protocol: one JSON request per line on stdin, one JSON response per line on
stdout. Stderr is free-form diagnostics (forwarded to the service log).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PEPLM_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PEPLM_ROOT))

import torch

from peplm.integrate.ss_modes import normalize_mode, resolve_ss_profile
from peplm.models.esm3_policy import ESM3Policy

# GRPO training state, keyed by adapter key ("peptide-l"/"peptide-d"/...):
# - persistent AdamW per key (state survives between learn calls)
# - which adapter currently weights the live policy (L/D tasks hot-swap)
_ADAPTERS_ROOT = Path(os.environ.get(
    "ESM3_ADAPTER_ROOT", str(PEPLM_ROOT / "adapters")))
_adapter_optimizers: dict = {}
_opt_last_use: dict = {}
_adapter_dirty = False
_MAX_LIVE_OPTIMIZERS = max(1, int(os.environ.get(
    "ESM3_MAX_LIVE_OPTIMIZERS", "4")))
_loaded_adapter_key: str | None = None
policy: ESM3Policy | None = None


def _log(message: str) -> None:
    print(f"[esm3-gpu] {message}", file=sys.stderr, flush=True)


def _ensure_adapter_loaded(key: str) -> None:
    global _loaded_adapter_key
    if _loaded_adapter_key == key:
        return
    adapter_path = _ADAPTERS_ROOT / key
    if adapter_path.exists():
        policy.load_adapter(adapter_path)
    _loaded_adapter_key = key


def _sync_adapter(key: str, adapter_dir: str | None = None) -> None:
    """保证常驻 policy 的权重 == 磁盘上 key 的 adapter(加载即权威)。

    - key 变化(跨任务)或 learn 刚保存过(dirty)→ 从磁盘原位重载;
    - 重载走 set_peft_model_state_dict(原位复制, 参数对象身份不变,
      既有 optimizer 的引用保持有效);
    - 跨任务残留因此不可能进入计算路径: 任何请求先经过本函数。
    """
    global _loaded_adapter_key, _adapter_dirty
    if _loaded_adapter_key == key and not _adapter_dirty:
        return
    path = Path(adapter_dir) if adapter_dir else (_ADAPTERS_ROOT / key)
    if path.exists():
        from peft import load_peft_weights
        from peft.utils import set_peft_model_state_dict
        weights = load_peft_weights(str(path), device=str(policy.device))
        set_peft_model_state_dict(policy.model, weights)
        for p_ in policy.model.parameters():
            if p_.requires_grad:
                p_.data = p_.data.to(policy.device)
    _loaded_adapter_key = key
    _adapter_dirty = False


def _optimizer_for(key: str) -> torch.optim.AdamW:
    opt = _adapter_optimizers.get(key)
    _opt_last_use[key] = time.time()
    if opt is None:
        # weight_decay=0: 整组奖励相同的代际产生全零优势与全零梯度, opt.step()
        # 仍按 lr*wd 腐蚀 adapter 权重。LoRA 微调惯例置 0。
        opt = torch.optim.AdamW(
            [p for p in policy.model.parameters() if p.requires_grad],
            lr=1e-5, weight_decay=0.0)
        _adapter_optimizers[key] = opt
    # per-target 键会随靶点数增长: 只在 GPU 上保留最近使用的优化器状态,
    # 淘汰项下次使用时重建(动量丢失可接受, adapter 权重在磁盘)。
    while len(_adapter_optimizers) > _MAX_LIVE_OPTIMIZERS:
        oldest = min(_opt_last_use, key=lambda k: _opt_last_use.get(k, 0.0))
        if oldest == key:
            break
        del _adapter_optimizers[oldest]
        del _opt_last_use[oldest]
        torch.cuda.empty_cache()
    return opt


def _conditioning(req: dict) -> dict:
    """Sampling-time conditioning shared by every mode so the policy family
    never shifts between propose and learn. structure_mode resolves to an
    ss8 template AT EACH CALL'S ACTUAL peptide length."""
    mode = req.get("structure_mode")
    return {
        "structure_mode": normalize_mode(mode) if mode else None,
        "ss_profile": req.get("ss_profile") or None,
        "peptide_first": bool(req.get("peptide_first", False)),
    }


def _ss_profile_for(cond: dict, n: int):
    return resolve_ss_profile(cond.get("structure_mode"),
                              cond.get("ss_profile"), n)


def _excluded_aas(req: dict) -> list[str]:
    """User-excluded residue letters. The worker is layout-blind — fixed
    faces are stamped downstream — so the check runs on the whole decoded
    sequence and a parent carrying a stamped face may over-reject; the
    proposer re-checks the variable region, so over-rejection is safe."""
    raw = req.get("excluded_aas") or []
    return sorted({a.upper() for a in raw
                   if isinstance(a, str) and len(a) == 1 and a.isalpha()})


def _propose(req: dict) -> dict:
    import math
    from collections import Counter
    receptor = req["receptor_sequence"]
    pep_len = int(req.get("peptide_length", 14))
    n = int(req.get("n_samples", 48))
    keep = int(req.get("n_keep", 16))
    excluded = _excluded_aas(req)
    _sync_adapter(str(req.get("adapter_key") or "default"), req.get("adapter_dir"))
    cond = _conditioning(req)
    CHUNK = 8
    seqs = []
    sampled = 0
    # 补采上限 4n: 名单越宽单轮存活率越低, 以多轮补采换满批次
    for _round in range(4):
        remaining = max(0, n - len(seqs))
        if remaining <= 0:
            break
        with torch.no_grad():
            while remaining > 0 and sampled < 4 * n:
                batch = min(CHUNK, remaining)
                trajs = policy.sample_batch(
                    receptor=receptor, pep_len=pep_len, n=batch,
                    keep=batch,
                    temperature=float(req.get("temperature", 0.9)),
                    num_steps=8, strategy="entropy",
                    ss_profile=_ss_profile_for(cond, pep_len),
                    peptide_first=cond["peptide_first"])
                sampled += batch
                for t in trajs:
                    s = policy.decode_tokens(t.peptide_tokens)
                    if not s or len(s) != pep_len:
                        continue
                    if any(aa in s for aa in excluded):
                        continue
                    freq = Counter(s)
                    h = -sum((c / pep_len) * math.log2(c / pep_len) for c in freq.values())
                    if h < 1.5:
                        continue
                    seqs.append({"sequence": s, "logprob": t.mean_logprob})
                remaining -= batch
                if remaining > 0:
                    torch.cuda.empty_cache()
    seqs.sort(key=lambda x: -x["logprob"])
    return {"sequences": seqs[:keep]}


def _refill(req: dict) -> dict:
    """Resample only the specified positions of a parent sequence."""
    parent = req.get("parent_sequence", "")
    positions = req.get("remask_positions", [])
    receptor = req.get("receptor_sequence", "")
    temperature = float(req.get("temperature", 0.7))
    excluded = _excluded_aas(req)
    # 固定段随请求传入: 违例判定只看可变区, 环锚点/固定面不算违例
    fixed_prefix = str(req.get("fixed_prefix") or "")
    fixed_suffix = str(req.get("fixed_suffix") or "")
    cond = _conditioning(req)
    if not parent or not positions:
        return {"error": "parent_sequence and remask_positions required"}

    _sync_adapter(str(req.get("adapter_key") or "default"), req.get("adapter_dir"))
    parent_tokens = policy.encode_peptide(parent, len(parent))
    if parent_tokens is None:
        return {"error": "failed to tokenize parent"}

    seq = None
    with torch.no_grad():
        # 违例重采: 名单生效时 refill 偶尔采回被排除残基; 重试耗尽后仍
        # 违例则原样返回, 由提案器兜底替换
        for _attempt in range(4):
            traj = policy.refill(
                receptor=receptor,
                parent_tokens=parent_tokens,
                remask_positions=[int(p) for p in positions],
                temperature=temperature,
                ss_profile=_ss_profile_for(cond, len(parent)),
                peptide_first=cond["peptide_first"],
            )
            if traj is None:
                return {"sequence": None, "reason": "no change"}
            seq = policy.decode_tokens(traj.peptide_tokens)
            if not seq:
                continue
            variable = seq
            if fixed_prefix and variable.startswith(fixed_prefix):
                variable = variable[len(fixed_prefix):]
            if fixed_suffix and variable.endswith(fixed_suffix):
                variable = variable[:-len(fixed_suffix)]
            if not any(aa in variable for aa in excluded):
                return {"sequence": seq}
    return {"sequence": seq if seq else None}


def _perplexity(req: dict) -> dict:
    _sync_adapter(str(req.get("adapter_key") or "default"), req.get("adapter_dir"))
    seq = req.get("peptide_sequence", "")
    score = policy.score_sequence(
        req.get("receptor_sequence", ""),
        seq,
        ss_profile=_ss_profile_for(_conditioning(req), len(seq)),
        peptide_first=bool(req.get("peptide_first", False)))
    return {"perplexity": float(-score) if score is not None else 10.0}


def _learn(req: dict) -> dict:
    """Sequence-level GRPO-REINFORCE with safety rails.

    - KL anchor (k3 estimator) to the adapter-disabled base: bounds the
      cross-task drift of the shared policy family when several design
      tasks train the same chirality concurrently.
    - Entropy bonus delays mode collapse over long runs.
    - Multi-epoch micro-batched updates: one gradient step per generation
      at lr 1e-5 barely moves the policy; epochs give the generation enough
      gradient while the KL anchor bounds the drift.
    - Zero-advantage rollouts (identical rewards in a group) are dropped:
      they carry no gradient and would only trigger decay-style drift.
    """
    import random

    data = req.get("rollouts", [])
    cond = _conditioning(req)
    groups: dict[str, list] = {}
    for rd in data:
        groups.setdefault(rd.get("group", "default"), []).append(rd)
    valid = {g: r for g, r in groups.items() if len(r) >= 2}
    if not valid:
        return {"skipped": "no group with >= 2 members"}

    import torch.nn.functional as Fn
    zero_std_groups = 0
    updates = []
    for g, rows in valid.items():
        rewards = [float(r["reward"]) for r in rows]
        m = sum(rewards) / len(rewards)
        # TRL 式组归一化: std + 1e-4 下限(组内全同值时 advantage 恰为 0)
        var = sum((x - m) ** 2 for x in rewards) / len(rewards)
        sd = max(var ** 0.5, 1e-4)
        if var ** 0.5 < 1e-9:
            zero_std_groups += 1
        for r in rows:
            n = len(r.get("sequence") or "")
            if n < 4:
                continue
            tokens = policy.encode_peptide(r["sequence"], n)
            if not tokens:
                continue
            adv = (r["reward"] - m) / sd
            if abs(adv) < 1e-9:
                continue  # 零优势行无梯度信号, 只会触发权重衰减式漂移
            plddts = r.get("plddts") or []
            if plddts and len(plddts) == n:
                credit = [max(1.0 - float(p), 0.05) for p in plddts]
                csum = sum(credit) or 1.0
                credit = [c / csum * n for c in credit]
            else:
                credit = [1.0] * n
            updates.append((tokens, adv, credit))

    if len(updates) < 2:
        return {"skipped": "tokenization failed or all-zero advantage"}

    epochs = max(1, int(req.get("epochs", 2)))
    beta_kl = float(req.get("beta_kl", os.environ.get("PEPLM_BETA_KL", "0.04")))
    entropy_coef = float(
        req.get("entropy_coef", os.environ.get("PEPLM_ENTROPY_COEF", "0.003")))
    micro_batch = max(1, int(req.get("micro_batch", 4)))

    key = str(req.get("adapter_key") or "default")
    _sync_adapter(key, req.get("adapter_dir"))
    opt = _optimizer_for(key)

    rec_seq = req.get("receptor_sequence", "")
    ctx_cache: dict[int, tuple] = {}

    def _context(n: int) -> tuple:
        if n not in ctx_cache:
            ss8_n = _ss_profile_for(cond, n)
            ctx_cache[n] = (
                policy.encode_context(rec_seq, n,
                                      peptide_first=cond["peptide_first"]),
                policy.peptide_start(rec_seq, n,
                                     peptide_first=cond["peptide_first"]),
                policy.encode_ss8(rec_seq, n, ss8_n,
                                  peptide_first=cond["peptide_first"])
                if ss8_n else None,
            )
        return ctx_cache[n]

    steps = 0
    last_loss = 0.0
    for _epoch in range(epochs):
        order = list(range(len(updates)))
        random.shuffle(order)
        for mb_start in range(0, len(order), micro_batch):
            batch = [updates[i] for i in order[mb_start:mb_start + micro_batch]]
            opt.zero_grad()
            mb_loss = torch.tensor(0.0, device=policy.device)
            for tokens, adv, credit in batch:
                n = len(tokens)
                ctx, pep_start, ss8 = _context(n)
                toks = ctx.clone()
                for i, tok_id in enumerate(tokens):
                    toks[0, pep_start + i] = tok_id
                logits = policy._forward_sequence_logits(toks, ss8)
                lp = Fn.log_softmax(
                    logits[0, pep_start:pep_start + n, :], dim=-1)
                tok_tensor = torch.tensor(tokens, device=policy.device)
                scores = lp.gather(1, tok_tensor.unsqueeze(1)).squeeze(1)
                # KL anchor to the adapter-disabled base (k3 estimator, >= 0):
                # the reference is the frozen base, so cross-task interference
                # cannot drag the policy arbitrarily far from its start.
                with policy.model.disable_adapter(), torch.no_grad():
                    ref_logits = policy._forward_sequence_logits(toks, ss8)
                ref_lp = Fn.log_softmax(
                    ref_logits[0, pep_start:pep_start + n, :], dim=-1)
                ref_scores = ref_lp.gather(
                    1, tok_tensor.unsqueeze(1)).squeeze(1)
                delta = ref_scores - scores
                kl = (torch.exp(delta.clamp(-20, 20)) + delta - 1.0).mean()
                # entropy bonus over the peptide positions
                ent = -(lp.exp() * lp).sum(-1).mean()
                credit_t = torch.tensor(
                    credit, device=policy.device, dtype=scores.dtype)
                weighted = (scores * credit_t).sum() / credit_t.sum()
                mb_loss = mb_loss - adv * weighted + beta_kl * kl \
                    - entropy_coef * ent
            mb_loss.backward()
            torch.nn.utils.clip_grad_norm_(
                [p for p in policy.model.parameters() if p.requires_grad], 1.0)
            opt.step()
            steps += 1
            last_loss = float(mb_loss.detach())

    save_dir = Path(req.get("new_adapter_dir") or (_ADAPTERS_ROOT / key))
    save_dir.mkdir(parents=True, exist_ok=True)
    policy.save_adapter(save_dir)
    _loaded_adapter_key = key
    _adapter_dirty = True   # 下一个请求先从磁盘重载, 保证在盘/在存一致
    return {
        "adapter_dir": str(save_dir),
        "n_updates": len(updates),
        "steps": steps,
        "loss": last_loss,
        "beta_kl": beta_kl,
        "entropy_coef": entropy_coef,
        "frac_reward_zero_std": (zero_std_groups / len(valid)) if valid else 0.0,
    }


HANDLERS = {
    "propose": _propose,
    "refill": _refill,
    "perplexity": _perplexity,
    "learn": _learn,
}


def main() -> None:
    global policy
    # The parent pins CUDA_VISIBLE_DEVICES to the leased card, so the only
    # visible device here is logical cuda:0 — indexing by the physical id
    # would raise "invalid device ordinal".
    device = "cuda:0"
    policy = ESM3Policy(
        checkpoint=PEPLM_ROOT / "models/external/esm3_sm_open_v1.pth",
        device=device,
        dtype=torch.bfloat16,
    )
    _log(f"policy ready on {device} (pid {os.getpid()})")
    print(json.dumps({"event": "ready", "device": device}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit({"ok": False, "error": f"bad request: {exc}"})
            continue
        mode = req.get("mode", "")
        try:
            if mode in ("propose", "refill") and req.get("adapter_key"):
                _sync_adapter(str(req.get("adapter_key") or "default"),
                              req.get("adapter_dir"))
            handler = HANDLERS.get(mode)
            if handler is None:
                resp = {"ok": False, "error": f"unknown mode: {mode}"}
            else:
                resp = handler(req)
                resp["ok"] = True
        except Exception as exc:
            resp = {"ok": False, "error": str(exc)}
        _emit(resp)


def _emit(resp: dict) -> None:
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
