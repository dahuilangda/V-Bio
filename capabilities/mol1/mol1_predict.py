#!/usr/bin/env python3
"""Mol-1 结构预测引擎封装（V-Bio capability）。

在 vbio-mol1-runtime 镜像内运行，职责：
  平台 yaml/JSON 输入 -> Mol-1 推理 JSON -> mol runner 推理 -> cif + 置信度汇总

输入二选一：
  --yaml       平台设计 yaml（constraints.bond / modifications 等平台约定）
  --input-json Mol-1 推理 JSON（AF3 server 风格，顶层为 list）

支持：
  modifications {position, ccd}   -> {ptmType: "CCD_<ccd>", ptmPosition}
  ligand  {ccd: X} / {smiles: S}  -> {"ligand": "CCD_X"} / {"ligand": S}
  ion     {ccd: X}                -> {"ion": X}
  constraints.bond                -> covalent_bonds（含环肽头尾 N(1)-C(L)）
  cyclic: true                    -> 自动生成头尾 N(1)-C(L) bond

MSA：受体链优先用 --msa-cache 下按序列 md5 存放的 a3m（平台共享缓存，
unpaired 语义）；缓存缺失走内置 colabfold 搜索，服务地址可用
--msa-server-url 指向平台自建服务。

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
            count = int(entity["count"]) if entity.get("count") else max(len(ids), 1)
            if kind == "proteinChain":
                seq_entry = {
                    "sequence": entity["sequence"],
                    "count": count,
                    **({"id": ids} if ids else {}),
                    **({"modifications": _to_mol_modifications(entity.get("modifications"))}
                       if entity.get("modifications") else {}),
                }
                out.append({"proteinChain": seq_entry})
            elif kind in ("dnaSequence", "rnaSequence"):
                out.append({kind: {
                    "sequence": entity["sequence"],
                    "count": count,
                    **({"id": ids} if ids else {}),
                }})
            elif kind == "ligand":
                if entity.get("ccd"):
                    raw = entity["ccd"]
                    codes = ([str(c).strip().upper() for c in raw]
                             if isinstance(raw, (list, tuple))
                             else [str(raw).strip().upper()])
                    codes = [c[4:] if c.startswith("CCD_") else c
                             for c in codes if c]
                    lig = f"CCD_{'_'.join(codes)}"
                else:
                    lig = str(entity.get("smiles") or entity.get("ligand") or "").strip()
                    if not lig:
                        raise ValueError(f"ligand entry missing smiles/ccd: {entity}")
                out.append({"ligand": {"ligand": lig,
                                       "count": count,
                                       **({"id": ids} if ids else {})}})
            elif kind == "ion":
                ion = str(entity.get("ccd") or entity.get("ion") or "").strip().upper()
                ion = ion[4:] if ion.startswith("CCD_") else ion
                out.append({"ion": {"ion": ion,
                                    "count": count,
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
    """平台设计 yaml -> Mol-1 jobs。

    cyclic: true 自动生成头尾 N(1)-C(L) bond（Mol-1 原生支持环肽的
    polymer-polymer 共价键）。constraints.bond 的 [chain,pos,atom] 寻址
    换算为 entity 序号。
    """
    data = yaml.safe_load(yaml_path.read_text()) or {}
    sequences = []
    explicit_bonds = []
    used_chain_ids = set()
    auto_letter = 0

    for entry in data.get("sequences") or []:
        if not isinstance(entry, dict) or not entry:
            continue
        kind = str(next(iter(entry))).strip().lower()
        info = entry.get(kind) or {}
        for rid in _to_mol_id_list(info.get("id")):
            used_chain_ids.add(rid)
        if not info.get("id"):
            auto_letter += 1
            k = auto_letter
            while True:
                label = ""
                n = k
                while n > 0:
                    n, r = divmod(n - 1, 26)
                    label = chr(ord("A") + r) + label
                if label not in used_chain_ids:
                    break
                k += 1
            used_chain_ids.add(label)
            info["id"] = [label]
        if kind == "protein":
            kind = "proteinChain"
            if info.get("cyclic"):
                seq = str(info.get("sequence") or "").strip().upper()
                chain_id = info["id"][0]
                explicit_bonds.append({"bond": {
                    "atom1": [chain_id, 1, "N"],
                    "atom2": [chain_id, len(seq), "C"],
                }})
            kind = "proteinChain"
        elif kind == "dna":
            kind = "dnaSequence"
        elif kind == "rna":
            kind = "rnaSequence"
        sequences.append({kind: dict(info)})

    bonds = list(explicit_bonds)
    for c in data.get("constraints") or []:
        if isinstance(c, dict) and "bond" in c:
            bonds.append(c)

    # constraints.bond 的 [chain,pos,atom] 换算成 entity 序号
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
        elif isinstance(bond, dict):
            covalent.append(_to_mol_bonds([bond])[0])
        else:
            raise ValueError(f"unsupported bond form: {bond!r}")

    job = {
        "name": str(data.get("name") or data.get("title") or yaml_path.stem),
        "sequences": _to_mol_sequences(sequences),
        "covalent_bonds": covalent,
    }
    return [job]


# ---------------------------------------------------------------- MSA

def _sanitize_for_msa_key(seq: str) -> str:
    return "".join(ch if ch in "ACDEFGHIKLMNPQRSTVWY" else "A"
                   for ch in seq.strip().upper())


def _seq_md5(seq: str) -> str:
    return hashlib.md5(_sanitize_for_msa_key(seq).encode("utf-8")).hexdigest()


def _cached_a3m_matches(path: Path, seq: str) -> bool:
    try:
        for line in path.read_text(errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith(">"):
                continue
            aligned = "".join(ch for ch in s if not ch.islower())
            return len(aligned) == len(seq)
    except OSError:
        return False
    return False


def attach_cached_msa(jobs: list[dict], msa_cache_dir: str | None) -> None:
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
            if path.exists() and _cached_a3m_matches(path, seq):
                entity["unpairedMsaPath"] = str(path)


def fill_missing_msa(jobs: list[dict], work_dir: Path,
                     msa_server_url: str | None) -> None:
    """为缺 MSA 的受体链跑 colabfold 搜索并回填路径。

    短链（<=25aa）不搜（colabfold 对过短序列报错），回退为自体 a3m
    （仅含查询自身的最小合法 a3m）。
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
                chain_dir = work_dir / "msa" / _seq_md5(seq)
                cached = [p for p in sorted(chain_dir.glob("**/non_pairing.a3m"))
                          if p.stat().st_size > 0 and _cached_a3m_matches(p, seq)]
                if cached:
                    entity["unpairedMsaPath"] = str(cached[0])
                    idx += 1
                    continue
                patch_msa_server(msa_server_url)
                from mol.runner.msa_search import msa_search
                try:
                    res_dirs = msa_search([seq], str(chain_dir))
                    chain_dir_out = res_dirs[0] if res_dirs else None
                except Exception:
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
            self_a3m = a3m_dir / f"self_{idx}.a3m"
            self_a3m.write_text(f">query_0\n{seq}\n")
            entity["unpairedMsaPath"] = str(self_a3m)
            idx += 1


def patch_msa_server(url: str | None) -> None:
    if not url:
        return
    import mol.web_service.colab_request_parser as parser
    parser.COLABFOLD_SERVICE_HOST_URL = url


# ---------------------------------------------------------------- 推理与汇总

def run_inference(input_json: Path, output_dir: Path, seed: int,
                  checkpoint_dir: str, use_msa: bool = True,
                  samples: int = 5) -> None:
    import subprocess

    checkpoint = Path(checkpoint_dir) / "mol-1-base.pt"
    cmd = [
        "python3.12", "-m", "mol.runner.batch_inference",
        "--input", str(input_json),
        "--out_dir", str(output_dir),
        "--seeds", str(seed),
        "--load_checkpoint_path", str(checkpoint),
        "--use_msa", "true" if use_msa else "false",
        "--sample", str(samples),
    ]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"mol inference exited {result.returncode}")


def collect_summaries(output_dir: Path) -> list[dict]:
    summaries = []
    for conf in sorted(output_dir.glob(
            "*/seed_*/predictions/*summary_confidence*.json")):
        data = json.loads(conf.read_text())
        cif = conf.parent / conf.name.replace("_summary_confidence", "")
        summaries.append({
            "seed": conf.parent.parent.name,
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
        attach_cached_msa(jobs, args.msa_cache_dir)
        fill_missing_msa(jobs, work_dir, args.msa_server_url)

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
