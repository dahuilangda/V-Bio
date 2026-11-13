#!/usr/bin/env python3
"""RFD4-Proteina adapter: input staging, spec construction, candidate
extraction and bicyclic design geometry.

Pure input/output translation -- the docker runners live in
run_single_prediction (same split as af3_adapter / protenix_adapter).

Sections:
- input staging          stage_target
- spec construction      default_spec (linear), build_spec (bicyclic)
- candidate extraction   candidates_from_summary, binder_from_cif_text
- bicyclic geometry      load_anchor_skeleton, build_bicyclic_skeleton,
                         pocket_centroid, write_combined_cif, chain_centroids
- verification payload   build_verify_yaml, combined_template_input

Bicyclic layout: anchor1 -[seed]- anchor2 -[NCAA + fillers]- anchor3. Anchor
folds come from per-linker assets (rfd4_assets/bicyc_anchor_<CCD>.cif,
generated from verified SEZ folds; the linker core is Kabsch-fitted onto the
verified arm positions). Seed-ring span ~11.8 A, closure ring ~8.4 A, ~3.5 A
reach per residue -> the closure ring needs at least MIN_CLOSURE_RESIDUES.
Constraint violations raise; no silent fallbacks.
"""
from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------- bicyclic tables

# NCAA sidechain atoms must match CCD naming (atomworks validates against it)
NCAA_ATOMS = {
    "NLE": ["CB", "CG", "CD", "CE"],
    "AIB": ["CB1", "CB2"],
    "HYP": ["CB", "CG", "CD", "OD1"],
    "ORN": ["CB", "CG", "CD", "NZ"],
    "CIT": ["CB", "CG", "CD", "NE", "CZ", "NH1", "NH2"],
}
NCAA_ONE_LETTER = {"NLE": "L", "AIB": "A", "HYP": "P", "ORN": "R", "CIT": "R"}

CLOSURE_TOL = 0.1  # max deviation of the closed peptide bond from ideal
# Closure-ring span ~8.4 A; 3 residues cannot span it (closure stalls near
# 3.6 A bond), 4 can -- verified by the optimizer
MIN_CLOSURE_RESIDUES = 4

AA3_TO_AA1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}
AA1_TO_AA3 = {v: k for k, v in AA3_TO_AA1.items()}

# Engh & Huber ideal geometry
B_C_N, A_CA_C_N = 1.329, math.radians(116.2)
B_N_CA, A_C_N_CA = 1.458, math.radians(121.7)
B_CA_C, A_N_CA_C = 1.525, math.radians(111.2)
B_CA_C_O, A_CA_C_O = 1.23, math.radians(120.8)
B_CA_CB, A_N_CA_CB = 1.53, math.radians(110.5)
T_C_N_CA_CB = math.radians(-122.6)


# ---------------------------------------------------------------- input staging

def stage_target(input_dir: Path, template_inputs: Optional[List[dict]]) -> Path:
    """Persist the RFD4 target structure (first uploaded template). RFD4 is a
    structure-conditioned engine: PDB uploads are converted to mmCIF, other
    formats pass through unchanged."""
    for idx, template in enumerate(template_inputs or []):
        content_b64 = template.get("content_base64")
        if not content_b64:
            continue
        try:
            raw = base64.b64decode(content_b64)
        except Exception as exc:
            raise ValueError(f"靶标结构解码失败: {exc}") from exc
        target_path = input_dir / "target.cif"
        fmt = str(template.get("format") or "").lower()
        suffix = Path(str(template.get("file_name") or f"target_{idx}")).suffix.lower()
        if fmt == "pdb" or suffix == ".pdb":
            import gemmi
            structure = gemmi.read_structure_string(raw.decode("utf-8", errors="replace"))
            structure.setup_entities()
            structure.remove_alternative_conformations()
            structure.remove_ligands_and_waters()
            structure.setup_entities()
            structure.make_mmcif_document().write_file(str(target_path))
        else:
            target_path.write_bytes(raw)
        return target_path
    raise ValueError(
        "RFD4 生成任务需要靶标坐标：请上传靶标结构（template_files），"
        "或先提交预测任务再将产物结构作为输入。")


# ---------------------------------------------------------------- spec construction

def default_spec(target_length: int, contig_override: str = "") -> str:
    """Linear binder spec: target backbone rigid, target identity pinned,
    binder length from RFD4_CONTIG or the 12-22 default."""
    import yaml

    spec = {
        "binder": {
            "input": "/workspace/input/target.cif",
            "contig": str(contig_override or "").strip()
                      or f"12-22, /0, A1-{target_length}",
            "conditions": {
                "C_CRDGRP": {"1": [{"select": "sel('A/*/*/[N,CA,C,O]')"}]},
                "C_SEQ": {"true": [{"select": "sel('A')"}]},
            },
        }
    }
    return yaml.safe_dump(spec, sort_keys=False)


def build_spec(stem: str, seed_len: int, has_ncaa: bool, pep_len: int,
               target_chains: Sequence[Tuple[str, int]],
               centroids: Dict[str, Tuple[float, float, float]]) -> str:
    """Bicyclic RFD4 spec.

    C_SEQ pins fixed residues only (terminal Cys, seed, NCAA); closure-ring
    fillers stay free for the engine. C_CRD pins the linker and Cys SG in
    absolute terms; the target backbone is rigid-body only -- an absolute
    C_CRD on group atoms conflicts with the coordinate group. Layout:
    cys1=1, seed 2..(1+seed_len), cys2=2+seed_len, NCAA=3+seed_len,
    terminal Cys=pep_len.
    """
    seed_end = 1 + seed_len
    fixed = [f"sel('B/*/1')", f"sel('B/*/[2-{seed_end}]')",
             f"sel('B/*/{seed_end + 1}')"]
    if has_ncaa:
        fixed.append(f"sel('B/*/{seed_end + 2}')")
    fixed.append(f"sel('B/*/{pep_len}')")
    cseq_lines = "\n".join(f'        - {{select: "{s}"}}' for s in fixed)
    contig = f"B1-{pep_len}, /0, C1, /0, " + ", /0, ".join(
        f"{c}1-{n}" for c, n in target_chains)
    cent_lines = "\n".join(
        f'      "{v[0]:.3f},{v[1]:.3f},{v[2]:.3f}":\n'
        f'        - {{select: "sel(\'{name}\')"}}'
        for name, v in sorted(centroids.items()))
    target_crd = "\n".join(
        f'        - {{select: "sel(\'{c}/*/*/[N,CA,C,O]\')"}}' for c, _n in target_chains)
    target_cseq = "\n".join(
        f'        - {{select: "sel(\'{c}\')"}}' for c, _n in target_chains)
    return f"""# {stem}
{stem}:
  input: /workspace/input/target.cif
  contig: "{contig}"
  conditions:
    C_SEQ:
      true:
        - {{select: "sel('C')"}}
{cseq_lines}
{target_cseq}
    C_CRD:
      true:
        - {{select: "sel('C')"}}
        - {{select: "sel('B/CYS/*/SG')"}}
    C_CENTROID:
{cent_lines}
    C_CRDGRP:
      1:
{target_crd}
"""


# ---------------------------------------------------------------- candidate extraction

def candidates_from_summary(summary: Dict[str, Any]) -> List[Dict[str, str]]:
    """summary.json -> candidate binders (shortest polymer chain per output,
    same convention as verify_designs.py). Chains with non-standard residue
    markers ([CCD]) are skipped."""
    candidates: List[Dict[str, str]] = []
    for entry in summary.get("outputs") or []:
        if not isinstance(entry, dict):
            continue
        chains = entry.get("chains") or {}
        polymers = [
            (name, str(info.get("sequence") or ""))
            for name, info in chains.items()
            if isinstance(info, dict) and "[" not in str(info.get("sequence") or "")
            and str(info.get("sequence") or "").strip()
        ]
        if not polymers:
            continue
        _name, sequence = min(polymers, key=lambda item: len(item[1]))
        label = Path(str(entry.get("file") or "")).stem or f"binder_{len(candidates) + 1}"
        candidates.append({"label": label, "sequence": sequence})
    return candidates


def binder_from_cif_text(cif_text: str) -> Tuple[str, List[Dict[str, Any]], List[int]]:
    """Extract the shortest polymer chain of a generated structure: one-letter
    sequence, NCAA modifications, Cys positions."""
    import gemmi
    st = gemmi.read_structure_string(cif_text)
    st.setup_entities()
    st.remove_alternative_conformations()
    polymers = [c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3]
    if not polymers:
        raise ValueError("no polymer chain in generated structure")
    lengths = [sum(1 for r in c if r.het_flag != "H") for c in polymers]
    shortest = min(lengths)
    if lengths.count(shortest) > 1:
        raise ValueError("ambiguous binder: multiple shortest polymer chains")
    chain = polymers[lengths.index(shortest)]
    letters: List[str] = []
    mods: List[Dict[str, Any]] = []
    cys: List[int] = []
    unknown: List[str] = []
    for res in chain:
        if res.het_flag == "H":
            continue
        name = res.name.strip().upper()
        pos = len(letters) + 1
        if name in NCAA_ONE_LETTER:
            mods.append({"position": pos, "ccd": name,
                         "baseResidue": NCAA_ONE_LETTER[name]})
            letters.append(NCAA_ONE_LETTER[name])
        elif name in AA3_TO_AA1:
            letters.append(AA3_TO_AA1[name])
            if name == "CYS":
                cys.append(pos)
        else:
            unknown.append(name)
    if unknown:
        # silent mapping would shift every downstream mod/bond position
        raise ValueError(f"unrecognized residues in binder chain: {sorted(set(unknown))}")
    return "".join(letters), mods, cys


# ---------------------------------------------------------------- bicyclic geometry

def _centroid(points: np.ndarray) -> np.ndarray:
    return points.mean(axis=0)


def _nerf(a, b, c, bond: float, angle: float, torsion: float) -> np.ndarray:
    """NeRF: fourth atom from three preceding atoms plus bond/angle/torsion."""
    a, b, c = map(np.asarray, (a, b, c))
    bc = c - b
    bc = bc / np.linalg.norm(bc)
    n = np.cross(b - a, bc)
    nn = np.linalg.norm(n)
    if nn < 1e-8:
        m = np.cross(bc, np.array([1.0, 0.0, 0.0]))
        if np.linalg.norm(m) < 1e-8:
            m = np.cross(bc, np.array([0.0, 1.0, 0.0]))
        n = m / np.linalg.norm(m)
    else:
        n = n / nn
    m = np.cross(n, bc)
    d = np.array([-bond * math.cos(angle),
                  bond * math.cos(torsion) * math.sin(angle),
                  bond * math.sin(torsion) * math.sin(angle)])
    return c + d[0] * bc + d[1] * m + d[2] * n


def _closure_segment(start_n, start_ca, start_c, target_n, n, obstacles):
    """Grow n residues between an anchor carbonyl C and the next anchor amide N.

    phi/psi are the variables; two-stage L-BFGS (close, then de-clash) from
    several seeds, best kept. Returns (residues, distance between the last C
    and the anchor N -- the newly formed peptide bond length).
    """
    from scipy.optimize import minimize

    def chain_from(t):
        out = []
        prev_n, prev_ca, prev_c = start_n, start_ca, start_c
        for i in range(n):
            phi, psi = t[2 * i], t[2 * i + 1]
            cur_n = _nerf(prev_n, prev_ca, prev_c, B_C_N, A_CA_C_N, psi)
            cur_ca = _nerf(prev_ca, prev_c, cur_n, B_N_CA, A_C_N_CA, math.pi)
            cur_c = _nerf(prev_c, cur_n, cur_ca, B_CA_C, A_N_CA_C, phi)
            cur_o = _nerf(cur_n, cur_ca, cur_c, B_CA_C_O, A_CA_C_O, psi + math.pi)
            cur_cb = _nerf(cur_c, cur_n, cur_ca, B_CA_CB, A_N_CA_CB, T_C_N_CA_CB)
            out.append({"N": cur_n, "CA": cur_ca, "C": cur_c,
                        "O": cur_o, "CB": cur_cb})
            prev_n, prev_ca, prev_c = cur_n, cur_ca, cur_c
        return out

    obst = np.asarray(obstacles)

    def closure_cost(t):
        last = chain_from(t)[-1]
        d = np.linalg.norm(last["C"] - target_n)
        v1, v2 = last["CA"] - last["C"], target_n - last["C"]
        ang = math.acos(np.clip(np.dot(v1, v2) /
                                (np.linalg.norm(v1) * np.linalg.norm(v2)), -1, 1))
        return 400.0 * (d - B_C_N) ** 2 + 20.0 * (ang - A_CA_C_N) ** 2

    def full_cost(t, reg=0.02):
        s = closure_cost(t)
        seg = chain_from(t)
        pts = np.array([p for r in seg for p in r.values()])
        idx = np.repeat(np.arange(n), 5)
        dd = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=-1)
        clash = (np.abs(idx[:, None] - idx[None, :]) >= 2) & (dd < 2.5)
        if clash.any():
            s += 15.0 * float(((2.5 - dd[clash]) ** 2).sum())
        do = np.linalg.norm(pts[:, None, :] - obst[None, :, :], axis=-1)
        hit = do < 2.9
        if hit.any():
            s += 15.0 * float(((2.9 - do[hit]) ** 2).sum())
        t = np.asarray(t)
        target = np.tile([math.radians(-120), math.radians(130)], n)
        return s + reg * float(((t - target) ** 2).sum())

    seeds = [np.tile([math.radians(-60), math.radians(-45)], n),
             np.tile([math.radians(-75), math.radians(145)], n),
             np.tile([math.radians(-120), math.radians(130)], n)]
    bounds = [(-math.pi, math.pi)] * (2 * n)
    best = None
    for t0 in seeds:
        r = minimize(closure_cost, t0, method="L-BFGS-B", bounds=bounds,
                     options={"maxiter": 200})
        r = minimize(full_cost, r.x, method="L-BFGS-B", bounds=bounds,
                     options={"maxiter": 250})
        if best is None or r.fun < best.fun:
            best = r
    seg = chain_from(best.x)
    return seg, float(np.linalg.norm(seg[-1]["C"] - target_n))


def load_anchor_skeleton(runtime_dir: Path, linker_ccd: str) -> Tuple[Dict[int, Dict[str, np.ndarray]], List[Tuple[str, np.ndarray]]]:
    """Load the bicyclic anchor skeleton asset for a linker: the verified fold's
    three anchor Cys plus the linker's heavy atoms. Coordinates only; the
    anchor distances fix both ring spans and the feasibility envelope."""
    import gemmi
    path = runtime_dir / "rfd4_assets" / f"bicyc_anchor_{linker_ccd}.cif"
    if not path.is_file():
        raise ValueError(f"双环锚定骨架资产缺失：{path}")
    blk = gemmi.cif.read(str(path)).sole_block()
    anchors: Dict[int, Dict[str, np.ndarray]] = {}
    linker: List[Tuple[str, np.ndarray]] = []
    for row in blk.find("_atom_site.",
                        ["label_atom_id", "label_comp_id", "label_asym_id",
                         "label_seq_id", "Cartn_x", "Cartn_y", "Cartn_z"]):
        pos = np.array([float(row[4]), float(row[5]), float(row[6])])
        if row[2] == "C":
            linker.append((row[0], pos))
        else:
            anchors.setdefault(int(row[3]), {})[row[0]] = pos
    required = {"N", "CA", "C", "O", "CB", "SG"}
    for idx, atoms in anchors.items():
        missing = required - set(atoms)
        if missing:
            raise ValueError(f"锚定骨架 Cys{idx} 缺少原子: {sorted(missing)}")
    if set(anchors) != {1, 2, 3} or len(linker) < 3:
        raise ValueError(f"锚定骨架资产不完整: anchors={sorted(anchors)}, linker={len(linker)}")
    return anchors, linker


def build_bicyclic_skeleton(
    seed: str,
    ncaa_ccd: Optional[str],
    length: int,
    anchors: Dict[int, Dict[str, np.ndarray]],
    linker_atoms: List[Tuple[str, np.ndarray]],
) -> Tuple[List[Tuple[str, Dict[str, np.ndarray]]], List[Tuple[str, np.ndarray]], str, Tuple[float, float]]:
    """Build the skeleton: anchor1 -[seed]- anchor2 -[NCAA + fillers]- anchor3.

    The NCAA takes the first closure-ring position (adjacent to the seed-ring
    exit). Returns (residues, linker heavy atoms, one-letter sequence with X
    fillers, (seed-ring / closure-ring peptide bond lengths)).
    """
    closure_n = length - len(seed) - 3
    if closure_n < MIN_CLOSURE_RESIDUES:
        raise ValueError(
            f"length too short: seed {len(seed)} + 3 Cys + closure ring of at "
            f"least {MIN_CLOSURE_RESIDUES} requires total >= "
            f"{len(seed) + 3 + MIN_CLOSURE_RESIDUES}, got {length}")
    if ncaa_ccd and ncaa_ccd not in NCAA_ATOMS:
        raise ValueError(f"unsupported NCAA CCD: {ncaa_ccd} "
                         f"(supported: {sorted(NCAA_ATOMS)})")

    cys1, cys2, cys3 = anchors[1], anchors[2], anchors[3]
    obstacles = [cys1["SG"], cys2["SG"], cys3["SG"]] + [p for _, p in linker_atoms]

    # reach pre-check: ~3.5 A per residue; fail fast instead of grinding L-BFGS
    span1 = float(np.linalg.norm(cys1["C"] - cys2["N"]))
    span2 = float(np.linalg.norm(cys2["C"] - cys3["N"]))
    if span1 > (len(seed) + 1) * 3.5 or span2 > (closure_n + 1) * 3.5:
        raise ValueError(
            f"bicyclic spans unreachable: seed-ring {span1:.1f} A over {len(seed)}"
            f" residues / closure-ring {span2:.1f} A over {closure_n} residues"
            f" (reach ~3.5 A per residue)")

    seed_ring, bond1 = _closure_segment(
        cys1["N"], cys1["CA"], cys1["C"], cys2["N"], len(seed), obstacles)
    closure_ring, bond2 = _closure_segment(
        cys2["N"], cys2["CA"], cys2["C"], cys3["N"], closure_n, obstacles)
    if abs(bond1 - B_C_N) > CLOSURE_TOL or abs(bond2 - B_C_N) > CLOSURE_TOL:
        raise ValueError(
            f"closure failed: seed-ring bond {bond1:.2f} A / closure-ring bond "
            f"{bond2:.2f} A (ideal {B_C_N:.2f} A); seed/length not feasible "
            f"on this skeleton")

    def _ncaa_sidechain(atoms: Dict[str, np.ndarray], ccd: str) -> Dict[str, np.ndarray]:
        # first sidechain atom placed as a proper L-CB (same reference as the
        # backbone builder); the rest stack outward along the CB direction
        cb = _nerf(atoms["C"], atoms["N"], atoms["CA"],
                   B_CA_CB, A_N_CA_CB, T_C_N_CA_CB)
        d = cb - atoms["CA"]
        d = d / np.linalg.norm(d)
        out = {k: v for k, v in atoms.items() if k != "CB"}
        for k, name in enumerate(NCAA_ATOMS[ccd]):
            out[name] = cb + 0.9 * k * d
        return out

    residues: List[Tuple[str, Dict[str, np.ndarray]]] = [("CYS", cys1)]
    residues += [(AA1_TO_AA3[ch], r) for ch, r in zip(seed, seed_ring)]
    residues.append(("CYS", cys2))
    for i, r in enumerate(closure_ring):
        if i == 0 and ncaa_ccd:
            residues.append((ncaa_ccd, _ncaa_sidechain(r, ncaa_ccd)))
        else:
            residues.append(("ALA", r))
    residues.append(("CYS", cys3))

    letters = ["C"] + list(seed) + ["C"] + ["X"] * closure_n + ["C"]
    if ncaa_ccd:
        letters[len(seed) + 2] = NCAA_ONE_LETTER[ncaa_ccd]
    return residues, linker_atoms, "".join(letters), (bond1, bond2)


def pocket_centroid(target_cif: Path,
                    author_contacts: Sequence[Tuple[str, int]]) -> np.ndarray:
    """CA centroid of the pocket residues; every author number must resolve."""
    import gemmi
    st = gemmi.read_structure(str(target_cif))
    st.setup_entities()
    wanted = {(c.strip(), int(n)) for c, n in author_contacts}
    pts: List[np.ndarray] = []
    seen = set()
    for ch in st[0]:
        for res in ch:
            seen.add((ch.name, res.seqid.num))
            if (ch.name, res.seqid.num) in wanted:
                ca = res.find_atom("CA", "*")
                if ca is not None:
                    pts.append(np.array([ca.pos.x, ca.pos.y, ca.pos.z]))
    missing = wanted - seen
    if missing:
        raise ValueError(f"pocket residues not found in target (author numbering): "
                         f"{sorted(missing)}")
    if not pts:
        raise ValueError("pocket residues have no CA atoms; no centroid")
    return _centroid(np.array(pts))


def write_combined_cif(path: Path, stem: str, residues, linker_heavy,
                       linker_ccd: str, target_cif: Path) -> List[Tuple[str, int]]:
    """Write the combined cif (B peptide + C linker + target chains).

    Target atom_site rows are merged as-is (no Structure round-trip); target
    chains named B/C are renamed to free letters. Returns the final target
    chains as (chain name, residue count) for contig and spec selects.
    """
    import gemmi
    blk = gemmi.cif.read(str(target_cif)).sole_block()
    rows = blk.find("_atom_site.",
                    ["group_PDB", "type_symbol", "label_atom_id", "label_comp_id",
                     "label_asym_id", "label_seq_id", "Cartn_x", "Cartn_y", "Cartn_z"])

    tags = ["group_PDB", "id", "type_symbol", "label_atom_id", "label_comp_id",
            "label_asym_id", "label_entity_id", "label_seq_id",
            "pdbx_PDB_ins_code", "pdbx_PDB_model_num", "Cartn_x", "Cartn_y",
            "Cartn_z", "occupancy", "B_iso_or_equiv", "auth_seq_id",
            "auth_asym_id", "auth_comp_id", "auth_atom_id"]
    lines = [f"data_{stem}", "#", "loop_"]
    lines += [f"_atom_site.{t}" for t in tags]
    atom_id = 0

    def emit(group, sym, atom, comp, asym, entity, seq, x, y, z):
        nonlocal atom_id
        atom_id += 1
        lines.append(f"{group} {atom_id} {sym} {atom} {comp} {asym} {entity} {seq} ? 1 "
                     f"{x:.3f} {y:.3f} {z:.3f} 1.00 20.00 {seq} {asym} {comp} {atom}")

    for seq_id, (comp, atoms) in enumerate(residues, start=1):
        for name, p in atoms.items():
            emit("ATOM", name[0], name, comp, "B", "1", seq_id, p[0], p[1], p[2])
    for name, p in linker_heavy:
        sym = "S" if name.startswith("S") else "C"
        emit("HETATM", sym, name, linker_ccd, "C", "2", 1, p[0], p[1], p[2])

    # polymer rows only: waters/ligands carry non-numeric or '.' seq ids and
    # would otherwise inflate residue counts (and contig) with fake chains
    polymer_rows = [r for r in rows
                    if r[0] == "ATOM" and str(r[5]).strip().lstrip("-").isdigit()]
    occupied = {"B", "C"} | {r[4] for r in polymer_rows}
    rename_pool = iter(c for c in "ADEFGHIJKLMNOPQRSTUVWXYZ" if c not in occupied)
    chain_map: Dict[str, str] = {}
    entity_map: Dict[str, str] = {}
    residue_ids: Dict[str, set] = {}
    for row in polymer_rows:
        src_chain = row[4]
        if src_chain not in chain_map:
            dst = src_chain
            if dst in {"B", "C"}:
                try:
                    dst = next(rename_pool)
                except StopIteration:
                    raise ValueError(
                        f"target chain {src_chain} collides with peptide/linker names "
                        f"and no letter is left for renaming") from None
            chain_map[src_chain] = dst
            entity_map[src_chain] = str(3 + len(entity_map))
            residue_ids[dst] = set()
        residue_ids[chain_map[src_chain]].add(str(row[5]))
    for row in polymer_rows:
        emit(row[0], row[1], row[2], row[3], chain_map[row[4]],
             entity_map[row[4]], row[5],
             float(row[6]), float(row[7]), float(row[8]))
    path.write_text("\n".join(lines) + "\n")
    return [(chain_map[src_chain], len(residue_ids[chain_map[src_chain]]))
            for src_chain in chain_map]


def chain_centroids(combined_cif: Path) -> Dict[str, Tuple[float, float, float]]:
    """Per-chain all-atom centroids of the combined cif (for C_CENTROID)."""
    import gemmi
    blk = gemmi.cif.read(str(combined_cif)).sole_block()
    pts: Dict[str, List[np.ndarray]] = {}
    for row in blk.find("_atom_site.",
                        ["label_asym_id", "Cartn_x", "Cartn_y", "Cartn_z"]):
        pts.setdefault(row[0], []).append(
            np.array([float(row[1]), float(row[2]), float(row[3])]))
    return {name: tuple(round(float(v), 3) for v in _centroid(np.array(p)))
            for name, p in sorted(pts.items())}


# ---------------------------------------------------------------- verification payload

def build_verify_yaml(target_sequence: str, binder_sequence: str,
                      mods: List[Dict[str, Any]], cys_positions: List[int],
                      linker_ccd: str, linker_atoms: Sequence[str]) -> str:
    """Bicyclic co-folding yaml: linker ligand + three SG bonds (Cys order to
    linker arm order)."""
    if len(cys_positions) != 3:
        raise ValueError(
            f"bicyclic verification needs 3 Cys, got {len(cys_positions)}: {cys_positions}")
    if len(linker_atoms) != 3:
        raise ValueError(
            f"linker {linker_ccd} must provide exactly 3 arm atoms, got {len(linker_atoms)}")
    if cys_positions != sorted(cys_positions):
        raise ValueError(f"Cys positions must be ascending, got {cys_positions}")
    if any(pos < 1 or pos > len(binder_sequence) for pos in cys_positions):
        raise ValueError(
            f"Cys positions {cys_positions} outside binder sequence length {len(binder_sequence)}")
    mod_lines = ""
    if mods:
        mod_lines = "    modifications:\n" + "".join(
            f"    - position: {m['position']}\n      ccd: {m['ccd']}\n"
            f"      baseResidue: {m['baseResidue']}\n" for m in mods)
    bonds = "".join(
        f"- bond:\n    atom1: [B, {cys}, SG]\n    atom2: [L, 1, {atom}]\n"
        for cys, atom in zip(cys_positions, linker_atoms))
    return (
        "version: 1\nsequences:\n"
        "- protein:\n    id: A\n"
        f"    sequence: {target_sequence}\n"
        "- protein:\n    id: B\n"
        f"    sequence: {binder_sequence}\n"
        + mod_lines +
        "- ligand:\n    id: L\n"
        f"    ccd: {linker_ccd}\n"
        "constraints:\n" + bonds)


def combined_template_input(combined_cif: Path) -> Dict[str, str]:
    """Package the combined cif as a template_inputs entry (staged as the RFD4
    input target.cif)."""
    return {
        "file_name": combined_cif.name,
        "format": "cif",
        "content_base64": base64.b64encode(combined_cif.read_bytes()).decode("ascii"),
    }
