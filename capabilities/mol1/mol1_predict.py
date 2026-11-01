#!/usr/bin/env python3
"""Mol-1 结构预测引擎封装（V-Bio capability）。

在 vbio-mol1-runtime 镜像内运行，职责：
  平台 yaml/JSON 输入 -> Mol-1 推理 JSON -> mol runner 推理 -> cif + 置信度汇总

输入二选一：
  --yaml      平台设计 yaml（constraints.bond / modifications 等平台约定）
  --input-json Mol-1 推理 JSON（AF3 server 风格，顶层为 list）

转换规则（平台约定 -> Mol-1）：
  modifications {position, ccd}      -> {ptmType: "CCD_<ccd>", ptmPosition: position}
  ligand  {ccd: X}                   -> {"ligand": "CCD_X"}
  ligand  {smiles: S} / {ligand: S}  -> {"ligand": S}（SMILES 原样）
  ion     {ccd: X}                   -> {"ion": X}
  constraints.bond                   -> covalent_bonds（Mol-1 原生支持环肽头尾酰胺）

MSA：受体链优先用 --msa-cache 下按序列 md5 存放的 a3m（平台共享缓存，
unpaired 语义）；缓存缺失的序列走内置 colabfold 搜索，服务地址可用
--msa-server-url 指向平台自建 colabfold 服务。

输出：<output_dir>/summary.json（置信度与产物路径）+ predictions/*.cif
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import yaml


# ---------------------------------------------------------------- 输入转换

def _to_mol_modifications(mods: list | None) -> list[dict]:
    out = []
    for m in mods or []:
        if not isinstance(m, dict):
            continue
        ccd = str(m.get("ccd") or m.get("ptmType") or "").strip().upper()
        ccd = ccd[4:] if ccd.startswith("CCD_") else ccd
        pos = int(m.get("position") or m.get("ptmPosition") or 0)
        if ccd and pos > 0:
            out.append({"ptmType": f"CCD_{ccd}", "ptmPosition": pos})
    return out


def _to_mol_id_list(raw_id) -> list[str]:
    """Mol-1 要求 id 为 list[str]；平台 yaml 的 id 是纯字符串。"""
    if raw_id is None:
        return []
    if isinstance(raw_id, (list, tuple)):
        return [str(x) for x in raw_id]
    return [str(raw_id)]


def _to_mol_sequences(sequences: list[dict]) -> list[dict]:
    out = []
    for entry in sequences or []:
        for kind, entity in (entry or {}).items():
            ids = _to_mol_id_list(entity.get("id"))
            if kind == "proteinChain":
                out.append({"proteinChain": {
                    "sequence": entity["sequence"],
                    "count": int(entity.get("count") or 1) or len(ids) or 1,
                    **({"id": ids} if ids else {}),
                    **({"modifications": _to_mol_modifications(entity.get("modifications"))}
                       if entity.get("modifications") else {}),
                }})
            elif kind in ("dnaSequence", "rnaSequence"):
                out.append({kind: {
                    "sequence": entity["sequence"],
                    "count": int(entity.get("count") or 1) or len(ids) or 1,
                    **({"id": ids} if ids else {}),
                }})
            elif kind == "ligand":
                lig = entity.get("smiles") or entity.get("ligand") or entity.get("ccd")
                if not lig:
                    raise ValueError(f"ligand entry missing smiles/ccd: {entity}")
                lig = str(lig)
                if not lig.startswith(("CCD_", "FILE_")):
                    # 纯大写短串视为 CCD 代码（与平台 SMILES/CCD 判别约定一致：
                    # CCD 误当 SMILES 会在解析时崩溃，SMILES 误当 CCD 只会查不到）
                    if lig.isupper() and len(lig) <= 5 and lig.isalnum():
                        lig = f"CCD_{lig}"
                out.append({"ligand": {"ligand": lig,
                                       "count": int(entity.get("count") or 1) or len(ids) or 1,
                                       **({"id": ids} if ids else {})}})
            elif kind == "ion":
                ion = str(entity.get("ccd") or entity.get("ion") or "").strip()
                out.append({"ion": {"ion": ion,
                                    "count": int(entity.get("count") or 1) or len(ids) or 1,
                                    **({"id": ids} if ids else {})}})
            else:
                raise ValueError(f"unsupported entity kind: {kind}")
    return out


def _to_mol_bonds(bonds: list[dict] | None) -> list[dict]:
    out = []
    for b in bonds or []:
        if not isinstance(b, dict):
            continue
        out.append({
            "entity1": str(b.get("entity1", "")),
            "copy1": int(b["copy1"]) if b.get("copy1") is not None else 1,
            "position1": str(b.get("position1", "")),
            "atom1": str(b.get("atom1", "")),
            "entity2": str(b.get("entity2", "")),
            "copy2": int(b["copy2"]) if b.get("copy2") is not None else 1,
            "position2": str(b.get("position2", "")),
            "atom2": str(b.get("atom2", "")),
        })
    return out


def yaml_to_mol_jobs(yaml_path: Path) -> list[dict]:
    """平台设计 yaml（boltz 风格键 protein/dna/rna/ligand/ion）-> Mol-1 jobs。

    环肽头尾酰胺键经 constraints.bond 转 covalent_bonds，Mol-1 原生支持
    环肽的 polymer-polymer 共价键（官方文档列明的例外情形）。
    """
    data = yaml.safe_load(yaml_path.read_text()) or {}
    sequences = []
    explicit_bonds = []
    auto_letter = 0
    for entry in data.get("sequences") or []:
        if not isinstance(entry, dict) or not entry:
            continue
        kind = str(next(iter(entry))).strip().lower()
        info = entry.get(kind) or {}
        if not info.get("id"):
            # 无 id 的实体按序分配 A/B/C...，保证 bond 寻址可用
            auto_letter += 1
            info["id"] = [chr(ord("A") + auto_letter - 1)]
        if kind == "protein":
            # cyclic: true -> 头尾 N(1)-C(L) 共价键（Mol-1 原生支持环肽键）
            if info.get("cyclic"):
                seq = str(info.get("sequence") or "")
                chain_id = (info.get("id") or ["A"])[0]
                explicit_bonds.append({"bond": {
                    "atom1": [chain_id, 1, "N"],
                    "atom2": [chain_id, len(seq), "C"],
                }})
            kind = "proteinChain"
        elif kind == "dna":
            kind = "dnaSequence"
        elif kind == "rna":
            kind = "rnaSequence"
        elif kind == "ligand" or kind == "ion":
            # yaml 的 constraints.bond 用 [chain, position, atom] 寻址，
            # ligand/ion 的 yaml 键与 Mol-1 同形
            pass
        sequences.append({kind: dict(info)})

    bonds = list(explicit_bonds)
    for c in data.get("constraints") or []:
        if isinstance(c, dict) and "bond" in c:
            bonds.append(c)

    # constraints.bond 的 atom1/atom2 是 [chain_id, position, atom] 平台写法，
    # 需换算成 Mol-1 的 entity 序号（sequences 列表中链 id 首次出现的 1-based 序号）
    chain_to_entity = {}
    for entity_no, entry in enumerate(sequences, start=1):
        entity = next(iter(entry.values()))
        for cid in entity.get("id") or []:
            chain_to_entity[str(cid).upper()] = entity_no

    covalent = []
    for b in bonds:
        bond = b.get("bond") or b
        a1, a2 = bond.get("atom1") or {}, bond.get("atom2") or {}
        if isinstance(a1, list) and isinstance(a2, list):
            e1 = chain_to_entity.get(str(a1[0]).upper())
            e2 = chain_to_entity.get(str(a2[0]).upper())
            if e1 is None or e2 is None:
                raise ValueError(f"bond references unknown chain: {bond}")
            covalent.append({
                "entity1": str(e1), "copy1": 1,
                "position1": str(a1[1]), "atom1": str(a1[2]),
                "entity2": str(e2), "copy2": 1,
                "position2": str(a2[1]), "atom2": str(a2[2]),
            })
        else:
            covalent.append(_to_mol_bonds([bond])[0])

    job = {
        "name": str(data.get("name") or data.get("title") or yaml_path.stem),
        "sequences": _to_mol_sequences(sequences),
        "covalent_bonds": covalent,
    }
    return [job]


# ---------------------------------------------------------------- MSA 填充

def _seq_md5(seq: str) -> str:
    """与 boltz2score/protenix2dock 共享的缓存键（msa_<md5>.a3m）。"""
    return hashlib.md5(seq.encode("utf-8")).hexdigest()


def attach_cached_msa(jobs: list[dict], msa_cache_dir: str | None,
                      msa_server_url: str | None) -> None:
    """受体链优先挂平台共享 MSA 缓存的 a3m（unpaired 语义）。

    命中缓存 -> unpairedMsaPath；未命中 -> 交给 Mol-1 内置搜索，
    并把 colabfold 服务地址改指平台自建服务（若提供）。
    """
    if not msa_cache_dir:
        return
    cache = Path(msa_cache_dir)
    for job in jobs:
        for entry in job["sequences"]:
            entity = entry.get("proteinChain")
            if not entity:
                continue
            seq = str(entity["sequence"]).strip().upper()
            path = cache / f"msa_{_seq_md5(seq)}.a3m"
            if path.exists():
                entity["unpairedMsaPath"] = str(path)


def patch_msa_server(url: str | None) -> None:
    """把 Mol-1 内置 colabfold 搜索指向平台自建服务。"""
    if not url:
        return
    import mol.web_service.colab_request_parser as parser
    parser.COLABFOLD_SERVICE_HOST_URL = url


def fill_missing_msa(jobs: list[dict], work_dir: Path,
                     msa_server_url: str | None) -> None:
    """为所有蛋白链落实 MSA 路径：受体走缓存/colabfold 搜索，肽链走自体 a3m。

    Mol-1 预处理对缺 MSA 路径的链会触发内置搜索（短肽会被 colabfold
    拒绝且预处理崩溃），因此每条链最终都必须带路径：
      受体 <=25aa 同样按短链处理；搜索失败也回退为自体 a3m。
    自体 a3m = 仅含查询自身的最小合法 a3m（无同源信息的标准表达）。
    """
    a3m_dir = work_dir / "self_msa"
    a3m_dir.mkdir(parents=True, exist_ok=True)
    idx = 0
    for job in jobs:
        for entry in job["sequences"]:
            entity = entry.get("proteinChain")
            if not entity or entity.get("unpairedMsaPath"):
                continue
            seq = str(entity["sequence"]).strip().upper()
            if len(seq) > 25:
                chain_dir = work_dir / "msa" / f"chain{idx}"
                cached = [p for p in sorted(chain_dir.glob("**/non_pairing.a3m"))
                          if p.stat().st_size > 0]
                if cached:  # 上次运行已搜过，直接复用
                    entity["unpairedMsaPath"] = str(cached[0])
                    idx += 1
                    continue
                patch_msa_server(msa_server_url)
                from mol.runner.msa_search import msa_search
                try:
                    res_dirs = msa_search([seq], str(chain_dir))
                    chain_dir_out = res_dirs[0] if res_dirs else None
                except Exception as exc:  # noqa: BLE001 — 单链失败回退自体
                    print(f"[mol1] MSA search failed: {exc}", file=sys.stderr)
                    chain_dir_out = None
                hits = []
                if chain_dir_out:
                    hits = [p for p in sorted(
                        Path(chain_dir_out).glob("**/non_pairing.a3m"))
                        if p.stat().st_size > 0]
                if hits:
                    entity["unpairedMsaPath"] = str(hits[0])
                    idx += 1
                    continue
            # 自体 a3m（无 MSA 的标准表达：MSA 仅含查询自身）
            self_a3m = a3m_dir / f"self_{idx}.a3m"
            self_a3m.write_text(f">query_0\n{seq}\n")
            entity["unpairedMsaPath"] = str(self_a3m)
            idx += 1


# ---------------------------------------------------------------- 推理与汇总

def run_inference(input_json: Path, output_dir: Path, seed: int,
                  checkpoint_dir: str, use_msa: bool = True) -> None:
    """走官方 mol CLI（batch_inference:mol_cli），保持与上游行为一致。"""
    import subprocess

    checkpoint = Path(checkpoint_dir) / "mol-1-base.pt"
    cmd = [
        "python3.12", "-m", "mol.runner.batch_inference",
        "--input", str(input_json),
        "--out_dir", str(output_dir),
        "--seeds", str(seed),
        "--load_checkpoint_path", str(checkpoint),
        "--use_msa", "true" if use_msa else "false",
    ]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"mol inference exited {result.returncode}")


def collect_summaries(output_dir: Path) -> list[dict]:
    """汇总各 seed 的置信度 JSON 与对应 cif。

    置信度与 cif 同在 <job>/seed_<n>/predictions/ 下：
    <name>_summary_confidence_sample_<k>.json 与 <name>_sample_<k>.cif。
    """
    summaries = []
    for conf in sorted(output_dir.glob(
            "*/seed_*/predictions/*summary_confidence*.json")):
        data = json.loads(conf.read_text())
        cif = conf.parent / conf.name.replace("_summary_confidence", "")
        summaries.append({
            "seed": conf.parent.parent.name,
            "sample": data.get("sample"),
            "confidence": data,
            "cif": str(cif) if cif.exists() else None,
        })
    return summaries


def main() -> None:
    ap = argparse.ArgumentParser(description="Mol-1 V-Bio engine wrapper")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--yaml", help="平台设计 yaml")
    src.add_argument("--input-json", help="Mol-1 推理 JSON（顶层 list）")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--seed", type=int, default=101)
    ap.add_argument("--checkpoint-dir", default=os.environ.get(
        "MOL_CHECKPOINT_DIR", "/cache/checkpoint"))
    ap.add_argument("--msa-cache-dir", default=os.environ.get(
        "MOL_MSA_CACHE_DIR", "/data/msa_cache"))
    ap.add_argument("--msa-server-url", default=os.environ.get(
        "MOL_MSA_SERVER_URL", "http://host.docker.internal:8080"))
    ap.add_argument("--no-msa", action="store_true",
                    help="完全不用 MSA（精度损失大，仅诊断用）")
    args = ap.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(args.work_dir or output_dir.parent / "work")
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.yaml:
        jobs = yaml_to_mol_jobs(Path(args.yaml))
    else:
        jobs = json.loads(Path(args.input_json).read_text())
        if isinstance(jobs, dict):
            jobs = [jobs]

    if not args.no_msa:
        attach_cached_msa(jobs, args.msa_cache_dir, args.msa_server_url)
        fill_missing_msa(jobs, work_dir, args.msa_server_url)
    else:
        from mol.config import inference as infer_config
        infer_config.inference_configs["use_msa"] = False

    input_json = work_dir / "mol1_input.json"
    input_json.write_text(json.dumps(jobs, ensure_ascii=False, indent=1))

    run_inference(input_json, output_dir, args.seed, args.checkpoint_dir,
                  use_msa=not args.no_msa)

    summaries = collect_summaries(output_dir)
    summary = {
        "engine": "mol1",
        "input": str(input_json),
        "n_summaries": len(summaries),
        "best": (max(summaries, key=lambda s: s["confidence"].get("ranking_score", 0))
                 if summaries else None),
        "summaries": summaries,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({"ok": True, "n": len(summaries),
                      "best_ranking": (summary["best"] or {}).get(
                          "confidence", {}).get("ranking_score")},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
