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
import math
import os
import re
import sys
from pathlib import Path

import yaml

from rdkit import Chem


# ---------------------------------------------------------------- 输入转换

def _to_mol_modifications(mods: list | None, seq_len: int = 0) -> list[dict]:
    out = []
    for m in mods or []:
        if not isinstance(m, dict):
            continue
        ccd = str(m.get("ccd") or m.get("ptmType") or "").strip().upper()
        ccd = ccd[4:] if ccd.startswith("CCD_") else ccd
        pos = int(m.get("position") or m.get("ptmPosition") or 0)
        if not ccd or pos <= 0:
            print(f"[mol1] warning: skipping invalid modification {m}",
                  file=sys.stderr)
            continue
        if seq_len and pos > seq_len:
            raise ValueError(
                f"modification position {pos} out of range (1..{seq_len})")
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
                    **({"modifications": _to_mol_modifications(
                            entity.get("modifications"),
                            seq_len=len(str(entity["sequence"]).strip()))}
                       if entity.get("modifications") else {}),
                }})
            elif kind in ("dnaSequence", "rnaSequence"):
                mods = []
                for m in entity.get("modifications") or []:
                    if not isinstance(m, dict):
                        continue
                    ccd = str(m.get("ccd") or m.get("modificationType") or "").strip().upper()
                    ccd = ccd[4:] if ccd.startswith("CCD_") else ccd
                    pos = int(m.get("position") or m.get("basePosition") or 0)
                    if ccd and pos > 0:
                        mods.append({"modificationType": f"CCD_{ccd}",
                                     "basePosition": pos})
                out.append({kind: {
                    "sequence": str(entity["sequence"]).strip().upper(),
                    "count": count,
                    **({"id": ids} if ids else {}),
                    **({"modifications": mods} if mods else {}),
                }})
            elif kind == "ligand":
                # 消歧规则：ccd 字段显式指定才按 CCD（强制 CCD_ 前缀）；
                # ligand 字段仅在本身带 CCD_ 前缀时按 CCD，否则一律按
                # SMILES（CCO/NC=O 等短串都是合法 SMILES，纯大写启发式
                # 会把乙醇误判成 CCD）。
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
                                       "count": int(entity.get("count") or 1) or len(ids) or 1,
                                       **({"id": ids} if ids else {})}})
            elif kind == "ion":
                ion = str(entity.get("ccd") or entity.get("ion") or "").strip().upper()
                ion = ion[4:] if ion.startswith("CCD_") else ion
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
            # 无 id 的实体按序分配，避开其他实体显式用过的 id
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
            # cyclic: true -> 头尾 N(1)-C(L) 共价键（Mol-1 原生支持环肽键）
            if info.get("cyclic"):
                seq = str(info.get("sequence") or "")
                chain_id = (info.get("id") or ["A"])[0]
                explicit_bonds.append({"bond": {
                    "atom1": [chain_id, 1, "N"],
                    "atom2": [chain_id, len(seq), "C"],
                }})  # 省略 copy -> Mol-1 对全部拷贝建键
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


# ---------------------------------------------------------------- MSA 填充

MSA_ALLOWED_AA = set("ACDEFGHIKLMNPQRSTVWY")
MSA_SEARCH_TIMEOUT_S = 1800  # colabfold 队列型服务的单链搜索上限（黑洞式挂起防护）


def _sanitize_for_msa_key(seq: str) -> str:
    """与 boltz2score 写缓存前相同的 sanitize（非标准字符替换为 A），
    保证两侧 md5 键互通。"""
    return "".join(ch if ch in MSA_ALLOWED_AA else "A"
                   for ch in seq.strip().upper())


def _seq_md5(seq: str) -> str:
    """与 boltz2score/protenix2dock 共享的缓存键（msa_<md5>.a3m）。"""
    return hashlib.md5(_sanitize_for_msa_key(seq).encode("utf-8")).hexdigest()


def _cached_a3m_matches(path: Path, seq: str) -> bool:
    """首条对齐行（去小写插入列）长度须与查询一致，否则视为缓存 miss。"""
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
            if path.exists() and _cached_a3m_matches(path, seq):
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
            if not entity:
                continue
            if entity.get("unpairedMsaPath") and not os.path.exists(
                    entity["unpairedMsaPath"]):
                print(f"[mol1] warning: dropping dangling unpairedMsaPath "
                      f"{entity['unpairedMsaPath']}", file=sys.stderr)
                entity.pop("unpairedMsaPath")
            if entity.get("unpairedMsaPath"):
                continue
            seq = str(entity["sequence"]).strip().upper()
            if len(seq) > 25:
                # 复用键用序列 md5（按出现序号复用会把不同序列的搜索结果挂错）
                chain_dir = work_dir / "msa" / _seq_md5(seq)
                cached = [p for p in sorted(chain_dir.glob("**/non_pairing.a3m"))
                          if p.stat().st_size > 0
                          and _cached_a3m_matches(p, seq)]
                if cached:  # 上次运行已搜过同序列，直接复用
                    entity["unpairedMsaPath"] = str(cached[0])
                    idx += 1
                    continue
                patch_msa_server(msa_server_url)
                from concurrent.futures import ThreadPoolExecutor
                from mol.runner.msa_search import msa_search
                try:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(msa_search, [seq], str(chain_dir))
                        res_dirs = future.result(timeout=MSA_SEARCH_TIMEOUT_S)
                    chain_dir_out = res_dirs[0] if res_dirs else None
                except Exception as exc:  # noqa: BLE001 — 单链失败回退自体
                    print(f"[mol1] MSA search failed: {exc}",
                          file=sys.stderr)
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


# ---------------------------------------------------------------- 结构解析

class MolAtom:
    """轻量原子（与 pose_scorecard.Atom 同口径的最小子集）。"""
    __slots__ = ("chain", "resid", "resname", "name", "elem", "x", "y", "z")

    def __init__(self, chain, resid, resname, name, elem, x, y, z):
        self.chain, self.resid, self.resname = chain, resid, resname
        self.name, self.elem = name, elem
        self.x, self.y, self.z = x, y, z

    @property
    def pos(self):
        return (self.x, self.y, self.z)


def dist(a, b) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def parse_structure(path: str) -> list[MolAtom]:
    """mmCIF（_atom_site）与 PDB 双格式自动识别。"""
    head = Path(path).read_bytes()[:200].decode(errors="replace")
    if head.lstrip().startswith(("data_", "loop_")):
        return _parse_cif(path)
    return _parse_pdb(path)


def _parse_cif(path: str) -> list[MolAtom]:
    atoms = []
    lines = Path(path).read_text(errors="replace").splitlines()
    start = next((i for i, l in enumerate(lines)
                  if l.strip() == "_atom_site.group_PDB"), None)
    if start is None:
        return atoms
    cols = []
    i = start
    while i < len(lines) and lines[i].strip().startswith("_atom_site."):
        cols.append(lines[i].strip().split(".", 1)[1])
        i += 1
    ix = {c: k for k, c in enumerate(cols)}
    required = ("auth_asym_id", "auth_seq_id", "label_comp_id",
                "label_atom_id", "type_symbol", "Cartn_x", "Cartn_y",
                "Cartn_z")
    missing = [c for c in required if c not in ix]
    if missing:
        raise ValueError(f"{path}: _atom_site missing columns {missing}")
    while i < len(lines):
        parts = lines[i].split()
        if len(parts) < len(cols):
            break
        if parts[ix["type_symbol"]].upper() == "H":
            i += 1
            continue
        atoms.append(MolAtom(
            chain=parts[ix["auth_asym_id"]],
            resid=int(float(parts[ix["auth_seq_id"]])),
            resname=parts[ix["label_comp_id"]],
            name=parts[ix["label_atom_id"]],
            elem=parts[ix["type_symbol"]].upper(),
            x=float(parts[ix["Cartn_x"]]), y=float(parts[ix["Cartn_y"]]),
            z=float(parts[ix["Cartn_z"]])))
        i += 1
    return atoms


def _parse_pdb(path: str) -> list[MolAtom]:
    atoms = []
    for line in Path(path).read_text(errors="replace").splitlines():
        if not line.startswith("ATOM"):
            continue
        if line[17:20].strip() in ("HOH", "DOD", "WAT"):
            continue
        elem = line[76:78].strip().upper() or line[12:16].strip()[0].upper()
        if elem == "H":
            continue
        atoms.append(MolAtom(
            chain=line[21].strip(), resid=int(line[22:26]),
            resname=line[17:20].strip(), name=line[12:16].strip(), elem=elem,
            x=float(line[30:38]), y=float(line[38:46]), z=float(line[46:54])))
    return atoms


# ---------------------------------------------------------------- mol1dock

# 平台自定义连接子 CCD（不在 Mol-1 官方 components.cif 中，需转 SDF）
CUSTOM_LINKER_CCDS = ("SEZ", "29N", "BS3")


def parse_ccd_cif(cif_path: Path):
    """解析平台自定义连接子 CCD cif（chem_comp atom/bond 表）。

    返回 (atom_ids, mol)：mol 按 cif 表顺序构建（含 ideal 3D 坐标），
    原子序 i+1 即 atom_ids[i]，供 bond 原子名 -> atom map 序号映射。
    """
    lines = cif_path.read_text(errors="replace").splitlines()
    # 定位 atom 表与 bond 表的列名与数据行
    tables = {}
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line == "loop_":
            cols = []
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("_chem_comp_"):
                cols.append(lines[j].strip())
                j += 1
            rows = []
            while j < len(lines):
                s = lines[j].strip()
                if (not s or s == "#" or s == "loop_"
                        or s.startswith("_chem_comp_")):
                    break
                rows.append(s.split())
                j += 1
            if cols:
                # cols[0] 形如 _chem_comp_atom.comp_id -> 表类别 "atom"/"bond"
                tables[cols[0].split(".")[0].split("_")[-1]] = (cols, rows)
            i = j
        else:
            i += 1
    if "atom" not in tables:
        raise ValueError(f"{cif_path}: no _chem_comp_atom table")
    atom_cols, atom_rows = tables["atom"]
    ix = {c.split(".", 1)[1]: k for k, c in enumerate(atom_cols)}
    atom_ids = [r[ix["atom_id"]] for r in atom_rows]
    elements = [r[ix["type_symbol"]].upper() for r in atom_rows]
    coords = [(float(r[ix["pdbx_model_Cartn_x_ideal"]]),
               float(r[ix["pdbx_model_Cartn_y_ideal"]]),
               float(r[ix["pdbx_model_Cartn_z_ideal"]])) for r in atom_rows]

    from rdkit import Chem
    from rdkit.Geometry import Point3D
    mol = Chem.RWMol()
    conf = Chem.Conformer(len(atom_ids))
    for k, (elem, (x, y, z)) in enumerate(zip(elements, coords)):
        atom = Chem.Atom(elem)
        atom.SetNoImplicit(True)
        mol.AddAtom(atom)
        conf.SetAtomPosition(k, Point3D(x, y, z))
    order_map = {"SING": Chem.BondType.SINGLE, "DOUB": Chem.BondType.DOUBLE,
                 "TRIP": Chem.BondType.TRIPLE, "AROM": Chem.BondType.AROMATIC}
    if "bond" in tables:
        bond_cols, bond_rows = tables["bond"]
        bix = {c.split(".", 1)[1]: k for k, c in enumerate(bond_cols)}
        for r in bond_rows:
            a1, a2 = r[bix["atom_id_1"]], r[bix["atom_id_2"]]
            order = r[bix["value_order"]].upper()
            mol.AddBond(atom_ids.index(a1), atom_ids.index(a2),
                        order_map.get(order, Chem.BondType.SINGLE))
    out = mol.GetMol()
    out.AddConformer(conf)
    return atom_ids, out


def prepare_custom_linkers(jobs: list[dict], work_dir: Path,
                           linker_ccd_dir: str | None) -> None:
    """平台自定义连接子 CCD 转 SDF（FILE_ 形式）+ bond 原子名重映射。

    Mol-1 官方 components.cif 不含 SEZ/29N/BS3；其 ligand 支持 FILE_ 路径
    （PDB/SDF/MOL，需 3D 构象）。连接子 cif 的 atom/bond 表经自含解析器
    构建分子（原子序 = cif 表序），写 SDF 时设 AtomMapNum = 原子序，
    Mol-1 按 atom_map_to_atom_name 原生解析。指向连接子的 bond 的 atom
    字段由 CCD 原子名重写为 map 序号——Mol-1 对 FILE_ 配体会丢弃原原子名，
    按名字符串匹配必然失败或错绑。
    """
    if not linker_ccd_dir:
        has_custom = any(
            str(e["ligand"]["ligand"]).removeprefix("CCD_").split("_")[0]
            in CUSTOM_LINKER_CCDS
            for job in jobs for entry in job["sequences"]
            if (e := entry.get("ligand")))
        if has_custom:
            raise ValueError(
                "yaml 引用了平台自定义连接子（SEZ/29N/BS3），但未提供 "
                "--linker-ccd-dir；该 CCD 不在 Mol-1 官方 components.cif 中")
        return
    from rdkit import Chem

    sdf_dir = work_dir / "linker_sdf"
    sdf_dir.mkdir(parents=True, exist_ok=True)
    for job in jobs:
        code_to_map = {}
        for entry in job["sequences"]:
            lig = entry.get("ligand")
            if not lig:
                continue
            text = str(lig.get("ligand") or "")
            code = text.removeprefix("CCD_")
            seg = code.split("_")[0]
            if seg not in CUSTOM_LINKER_CCDS:
                continue
            sdf = sdf_dir / f"{seg}.sdf"
            if sdf.exists():
                atom_ids, _ = parse_ccd_cif(
                    Path(linker_ccd_dir) / f"{seg}.cif")
            else:
                cif_path = Path(linker_ccd_dir) / f"{seg}.cif"
                if not cif_path.exists():
                    raise FileNotFoundError(
                        f"linker CCD cif missing: {cif_path}")
                atom_ids, mol = parse_ccd_cif(cif_path)
                for k, a in enumerate(mol.GetAtoms(), start=1):
                    a.SetAtomMapNum(k)
                writer = Chem.SDWriter(str(sdf))
                writer.write(mol)
                writer.close()
            code_to_map[seg] = atom_ids
            lig["ligand"] = f"FILE_{sdf.resolve()}"

        # bond 原子名 -> map 序号（仅指向已转换连接子的键）
        for bond in job.get("covalent_bonds") or []:
            for side in ("1", "2"):
                ent = bond.get(f"entity{side}")
                ent_entry = (job["sequences"][int(ent) - 1]
                             if ent and str(ent).isdigit()
                             and 0 < int(ent) <= len(job["sequences"]) else {})
                lig = ent_entry.get("ligand")
                if not lig:
                    continue
                code = str(lig.get("ligand") or "").removeprefix("FILE_")
                seg = Path(code).stem
                if seg in code_to_map:
                    atom_ids = code_to_map[seg]
                    atom_name = str(bond.get(f"atom{side}", ""))
                    if atom_name.isdigit():
                        continue  # 已是序号
                    if atom_name not in atom_ids:
                        raise ValueError(
                            f"bond atom '{atom_name}' not in linker {seg} "
                            f"atom table {atom_ids}")
                    bond[f"atom{side}"] = atom_ids.index(atom_name) + 1


def parse_pocket_res(text: str | None) -> list[tuple[str, int]]:
    """'A:18,A:76' -> [('A', 18), ('A', 76)]。"""
    out = []
    for item in str(text or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        chain, num = item.split(":", 1)
        out.append((chain.strip(), int(num)))
    return out


def pocket_contact_report(atoms, pep_chain: str,
                          pocket_res: list[tuple[str, int]],
                          max_dist: float) -> dict:
    """采集后口袋接触报告：肽链重原子到各口袋残基的最小距离。

    口袋不条件化采样器（Mol-1 无推理期 pocket 引导），作为采集门槛与
    评分信号——与平台 blind 路线协议一致。
    """
    pep_heavy = [a for a in atoms
                 if a.chain == pep_chain and a.elem in ("C", "N", "O", "S")]
    per_res = {}
    for chain, num in pocket_res:
        pocket_atoms = [a for a in atoms
                        if a.chain == chain and a.resid == num
                        and a.elem in ("C", "N", "O", "S")]
        if not pocket_atoms or not pep_heavy:
            continue
        best = min(dist(a, p) for a in pep_heavy for p in pocket_atoms)
        per_res[f"{chain}:{num}"] = round(best, 2)
    overall = min(per_res.values()) if per_res else None
    unmatched = [f"{c}:{n}" for c, n in pocket_res
                 if f"{c}:{n}" not in per_res]
    if unmatched:
        print(f"[mol1] warning: pocket residues not found in output cif "
              f"(Mol-1 renumbers each entity 1..L): {unmatched}",
              file=sys.stderr)
    return {
        "pocket_min": overall,
        "pocket_max_allowed": max_dist,
        "pocket_pass": overall is not None and overall <= max_dist,
        "pocket_per_residue": per_res,
        "pocket_unmatched": unmatched,
    }


def interface_metrics(atoms, pep_chain: str, clash_cutoff: float = 2.2) -> dict:
    """跨链界面几何（自含精简口径）：氢键、界面最小距、clash 对。

    clash = 跨链重原子对距离 < clash_cutoff（2.2 Å，氢键给到 2.2 以下
    的强极性接触会同时计入氢键，此处按几何保守计）。
    """
    pep_heavy = [a for a in atoms
                 if a.chain == pep_chain and a.elem in ("C", "N", "O", "S")]
    rec_heavy = [a for a in atoms
                 if a.chain != pep_chain and a.elem in ("C", "N", "O", "S")]
    n_clash = 0
    min_dist = None
    n_hbond = 0
    for p in pep_heavy:
        for r in rec_heavy:
            d = dist(p, r)
            if min_dist is None or d < min_dist:
                min_dist = d
            if d < clash_cutoff:
                n_clash += 1
            polar_pair = ((p.elem in ("N", "O") and r.elem in ("N", "O"))
                          and p.name != "C" and r.name != "C")
            if polar_pair and d <= 3.5:
                n_hbond += 1
    return {
        "interface_min_dist": round(min_dist, 2) if min_dist else None,
        "interface_clash_pairs": n_clash,
        "n_interface_hbonds": n_hbond,
    }


def build_dock_summary(summaries: list[dict], output_dir: Path,
                       mode: str, pocket_res: list[tuple[str, int]],
                       pocket_max_dist: float,
                       peptide_chain: str | None = None) -> dict:
    """逐样本附加几何/口袋指标，产出 protenix2dock 兼容字段名的 summary。"""
    confidences = []
    n_clean = 0
    for item in summaries:
        conf = dict(item["confidence"])
        sample = item.get("sample")
        geo = {"interface_min_dist": None, "interface_clash_pairs": None,
               "n_interface_hbonds": None}
        pocket = {"pocket_min": None, "pocket_pass": None,
                  "pocket_per_residue": {}}
        if item.get("cif") and Path(item["cif"]).exists():
            atoms = parse_structure(str(item["cif"]))
            chains = {}
            for a in atoms:
                chains.setdefault(a.chain, set()).add(a.resid)
            if chains:
                # 显式指定优先；自动猜测仅考虑多残基链（排除离子/配体）
                if peptide_chain:
                    pep_chain = peptide_chain
                else:
                    multi = {c: r for c, r in chains.items() if len(r) > 1}
                    pep_chain = min(multi or chains,
                                    key=lambda c: len(chains[c]))
                geo = interface_metrics(atoms, pep_chain)
                if pocket_res:
                    pocket = pocket_contact_report(atoms, pep_chain,
                                                   pocket_res, pocket_max_dist)
                if not geo["interface_clash_pairs"]:
                    n_clean += 1
        row = {
            "sample": sample,
            "seed": item.get("seed"),
            "file": item.get("confidence", {}).get("file"),
            "cif": item.get("cif"),
            "ranking_score": conf.get("ranking_score"),
            "iptm": conf.get("iptm"),
            "ptm": conf.get("ptm"),
            "plddt": conf.get("plddt"),
            "gpde": conf.get("gpde"),
            "has_clash": conf.get("has_clash"),
            **geo,
            "pocket": pocket if pocket_res else None,
        }
        confidences.append(row)

    def _rank(row):
        return (row.get("ranking_score") or 0)

    def _iface(row):
        # 界面优先排序：无 clash 优先，其次界面更紧；无距离信息视为最差
        clean = 0 if (row.get("interface_clash_pairs") or 0) == 0 else 1
        min_d = row.get("interface_min_dist")
        return (clean, min_d if min_d is not None else float("inf"))

    ranked = sorted(confidences, key=_rank, reverse=True)
    by_iface = sorted(confidences, key=_iface)
    return {
        "engine": "mol1dock",
        "mode": mode,
        "n_confidences": len(confidences),
        "confidences": ranked,
        "structures": [r["cif"] for r in ranked if r.get("cif")],
        "best": ranked[0] if ranked else None,
        "best_by_interface": by_iface[0] if by_iface else None,
        "n_geometrically_clean": n_clean,
    }


# ---------------------------------------------------------------- 推理与汇总

def run_inference(input_json: Path, output_dir: Path, seed: int,
                  checkpoint_dir: str, use_msa: bool = True,
                  samples: int = 5) -> None:
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
        "--sample", str(samples),
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
        stem = conf.name.replace("_summary_confidence_sample_", "_sample_")
        cif = conf.parent / (stem[:-len(".json")] + ".cif")
        if not cif.exists():
            print(f"[mol1] warning: cif missing for {conf.name}",
                  file=sys.stderr)
        sample_match = re.search(r"_sample_(\d+)\.json$", conf.name)
        summaries.append({
            "seed": int(conf.parent.parent.name.removeprefix("seed_")),
            "sample": (int(sample_match.group(1)) if sample_match else None),
            "file": str(conf),
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
    # ---- mol1dock（peptide 模式）----
    ap.add_argument("--mode", default="complex", choices=["complex", "peptide"],
                    help="peptide=受体+肽共折叠 + 界面/口袋采集后报告")
    ap.add_argument("--samples", type=int, default=5, help="每 seed 扩散样本数")
    ap.add_argument("--seeds", default=None, help="逗号分隔多种子，如 101,102")
    ap.add_argument("--peptide-chain", default=None,
                    help="肽链 id（peptide 模式界面指标的作用域）")
    ap.add_argument("--pocket-res", default=None,
                    help="口袋残基 'A:18,A:76'（采集后门槛，不条件化采样器）")
    ap.add_argument("--pocket-max-dist", type=float, default=6.0)
    ap.add_argument("--linker-ccd-dir", default=os.environ.get(
        "MOL1_LINKER_CCD_DIR", "/workspace/linker_ccd"),
        help="平台自定义连接子 cif 目录（SEZ/29N/BS3）")
    args = ap.parse_args()
    seeds = ([int(s) for s in str(args.seeds).split(",") if s.strip()]
             if args.seeds else [args.seed])

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

    prepare_custom_linkers(jobs, work_dir, args.linker_ccd_dir)
    if not args.no_msa:
        attach_cached_msa(jobs, args.msa_cache_dir)
        fill_missing_msa(jobs, work_dir, args.msa_server_url)
    # --no-msa 的实际生效路径是 run_inference 的 --use_msa false
    # （config 内存改动对推理子进程无效）

    input_json = work_dir / "mol1_input.json"
    input_json.write_text(json.dumps(jobs, ensure_ascii=False, indent=1))

    for seed in seeds:
        run_inference(input_json, output_dir, seed, args.checkpoint_dir,
                      use_msa=not args.no_msa, samples=args.samples)

    summaries = collect_summaries(output_dir)
    if args.mode == "peptide":
        pocket_res = parse_pocket_res(args.pocket_res)
        summary = build_dock_summary(summaries, output_dir, args.mode,
                                     pocket_res, args.pocket_max_dist,
                                     peptide_chain=args.peptide_chain)
    else:
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
    best = summary.get("best") or {}
    print(json.dumps({"ok": True, "n": len(summaries),
                      "best_ranking": (best.get("confidence") or {}).get(
                          "ranking_score", best.get("ranking_score"))},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
