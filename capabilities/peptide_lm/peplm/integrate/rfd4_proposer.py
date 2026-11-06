"""RFD4-Proteina 提案器：以全原子生成模型为多肽强化学习环提供候选群。

与 ESM3Proposer 同一 propose() 契约——输入残基池/精英行/数量，输出
(sequence, modifications, cys_anchors, group) 四元组。差异在候选来源：
RFD4 按靶点 pocket 条件化直接生成 binder 结构，序列从生成的结构里提取，
天然带 NCAA 词表能力（147 类词表含 100 NCAA）。

RL 融合点：精英行（上一世代按 ipSAE/refine 分数选出的优胜者）以 C_SEQ/
C_CRD 条件化回注给 RFD4——把上一代最优骨架作为 motif，让 RFD4 在其邻域
重采样，GRPO 的组相对优势因此作用于 RFD4 生成分布。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from pathlib import Path

_IMAGE = "rfd4-proteina-engine:cu13"


def _run_rfd4(spec_payload: dict, out_dir: Path, seed: int,
              gpu_id: int, timeout_min: float = 30.0) -> list[Path]:
    """在引擎容器里跑一次 rfd4-generate，返回生成的 cif 路径列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_path = out_dir / "rfd4_spec.yaml"
    import yaml

    spec_path.write_text(yaml.safe_dump(spec_payload, sort_keys=False))
    cmd = [
        "docker", "run", "--rm", "--runtime", "nvidia",
        "--gpus", f"device={gpu_id}",
        "-e", "HF_HOME=/workspace/hf_cache",
        "-e", "HF_ENDPOINT=https://hf-mirror.com",
        "-v", f"{out_dir}:/workspace/runs",
        "-w", "/workspace",
        "--entrypoint", "python3",
        _IMAGE, "/app/rfd4_generate.py",
        "--spec", f"/workspace/runs/{spec_path.name}",
        "--out-dir", f"/workspace/runs/{out_dir.name}",
        "--num-designs", "1",
        "--seed", str(seed),
        "--timeout-min", str(int(timeout_min)),
    ]
    proc = subprocess.run(cmd, timeout=timeout_min * 60)
    if proc.returncode != 0:
        raise RuntimeError(f"rfd4-generate 退出码 {proc.returncode}")
    return sorted(out_dir.rglob("*.cif"))


def _sequence_from_cif(cif_path: Path) -> tuple[str, list[int]]:
    """从生成的 binder cif 提取最短聚合物链的序列与 Cys 锚点（0 基）。"""
    import gemmi

    st = gemmi.read_structure(str(cif_path))
    st.setup_entities()
    st.remove_alternative_conformations()
    polymers = [c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3]
    if not polymers:
        raise RuntimeError(f"RFD4 产物无聚合物链: {cif_path}")
    chain = min(polymers, key=lambda c: sum(1 for r in c if r.het_flag != "H"))
    aa3_to_aa1 = {
        "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
        "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
        "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
        "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    }
    seq = "".join(aa3_to_aa1.get(r.name.strip().upper(), "A")
                  for r in chain if r.het_flag != "H")
    anchors = [idx for idx, ch in enumerate(seq) if ch == "C"]
    return seq, anchors


class RFD4Proposer:
    """RFD4 引擎提案器：候选由全原子生成模型按 pocket/motif 条件化产出。"""

    def __init__(
        self,
        *,
        receptor_sequence: str,
        pocket_positions: list[int],
        gpu_id: int = 0,
        binder_len_lo: int = 10,
        binder_len_hi: int = 15,
        work_dir: str = "/tmp/rfd4_proposer",
        seed: int = 0,
        log=None,
    ) -> None:
        self.receptor_sequence = receptor_sequence
        self.pocket_positions = pocket_positions
        self.gpu_id = gpu_id
        self.binder_len_lo = binder_len_lo
        self.binder_len_hi = binder_len_hi
        self.work_dir = Path(work_dir)
        self.seed = seed
        self._log = log or (lambda msg: None)

    def propose(
        self,
        natural_pool: list[str],
        unnatural_pool: list[dict],
        elite_rows: list[dict],
        n: int,
        plddt_hint: float | None = None,
    ) -> list[tuple[str, list[dict], list[int], str]]:
        """按 RL 契约产出 n 个候选。

        精英融合：有精英行时，把最强精英的骨架作 motif 条件化重采样
        （RFD4 版的 semigreedy），其余预算走 pocket 条件化的 de novo。
        """
        self._log(f"RFD4 propose: n={n}, elites={len(elite_rows)}")
        proposals: list[tuple[str, list[dict], list[int], str]] = []
        seen: set[str] = set()
        batch_dir = self.work_dir / f"propose_{uuid.uuid4().hex[:8]}"

        specs = []
        # de novo：pocket 条件化 binder，长度窗口内
        lo, hi = self.binder_len_lo, self.binder_len_hi
        pocket = ",".join(str(p) for p in self.pocket_positions)
        specs.append({
            "binder": {
                "sequence": self.receptor_sequence,
                "chain_name": "A",
                "binder_len": f"{lo}-{hi}",
                "pocket_residues": pocket,
            }
        })
        # 精英邻域：把最强精英序列固定为 motif（C_SEQ），骨架重采样
        if elite_rows:
            elite = elite_rows[0]
            specs.append({
                "elite_motif": {
                    "sequence": self.receptor_sequence,
                    "chain_name": "A",
                    "motif_sequence": elite["sequence"],
                }
            })

        per_batch = max(1, (n + len(specs) - 1) // len(specs))
        idx = 0
        for si, spec_payload in enumerate(specs):
            group = "rfd4_denovo" if si == 0 else "rfd4_elite"
            cifs = _run_rfd4(spec_payload, batch_dir / f"batch_{si}",
                             self.seed + si * 1000, self.gpu_id)
            for cif in cifs:
                if len(proposals) >= n:
                    break
                try:
                    seq, anchors = _sequence_from_cif(cif)
                except RuntimeError as exc:
                    self._log(f"跳过 {cif.name}: {exc}")
                    continue
                if len(seq) < lo or len(seq) > hi or seq in seen:
                    continue
                seen.add(seq)
                # 双环拓扑由下游锚点盖章流程按布局规则补 Cys；
                # 这里保留 RFD4 原生序列与锚点
                proposals.append((seq, [], anchors, group))
                idx += 1

        # 预算缺口：按残基池半随机补齐（保持 RL 预算恒定）
        import random

        rng = random.Random(self.seed + idx)
        while len(proposals) < n:
            seq = "".join(rng.choice(natural_pool) for _ in range(self.binder_len_hi))
            if seq in seen:
                continue
            seen.add(seq)
            proposals.append((seq, [], [], "rfd4_fallback"))
        return proposals[:n]
