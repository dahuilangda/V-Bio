"""双环链接器的键注入与测量：LINK 记录生成、首尾键报告、
锚点重建与逐锚点键长门禁。从 run_single_prediction 拆出，
供 refine 链路与测试共用。"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

import gemmi
import numpy as np

from backend.runtime.bicyclic_linkers import atom_map as _registry_atom_map
from backend.runtime.design_constants import SEZ_RING_BOND_CUTOFF_A

BICYCLIC_LINKER_ATOM_MAP: Dict[str, List[str]] = _registry_atom_map()


def _pdb_link_line(
    atom1: str, res1: str, chain1: str, seq1: int,
    atom2: str, res2: str, chain2: str, seq2: int,
) -> str:
    """Column-exact PDB LINK record. Atom names must sit right-aligned in
    cols 13-16/43-46 — a left-aligned name makes gemmi infer a metal
    coordination (MetalC) instead of a covalent bond, and boltz2 only honors
    Covale connections."""
    cols = [" "] * 78

    def put(start: int, text: str) -> None:
        for offset, char in enumerate(text):
            cols[start - 1 + offset] = char

    put(1, "LINK")
    put(13, f"{atom1:>4}")
    put(18, f"{res1:>3}")
    put(22, chain1)
    put(23, f"{seq1:4d}")
    put(43, f"{atom2:>4}")
    put(48, f"{res2:>3}")
    put(52, chain2)
    put(53, f"{seq2:4d}")
    put(60, "1555")
    put(66, "1555")
    return "".join(cols).rstrip()


def _append_staged_bicyclic_links(
    staged_path: Path,
    cys_positions: Optional[List[int]],
    linker_ccd: str,
) -> int:
    """(Re-)append the Cys-SG <-> linker-anchor LINK records to a staged PDB.

    gemmi 0.7.5's write_pdb silently drops connections, so every rewrite of
    the staged file (pocket placement, L-space staging) loses the covalent
    topology the refine engine needs to hold the ring together. Idempotent:
    returns 0 when the file already carries LINK records. Pairs each peptide
    Cys (in cys_positions, or every Cys when None) with the linker anchors in
    BICYCLIC_LINKER_ATOM_MAP order.
    """
    text = staged_path.read_text(errors="replace")
    if any(line.startswith("LINK") for line in text.splitlines()):
        return 0
    st = gemmi.read_structure(str(staged_path))
    st.setup_entities()
    polymers = [
        c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3
    ]
    if not polymers:
        return 0
    # the peptide is the SHORTEST polymer chain (multimer targets have
    # receptor copies that carry Cys and would win a first-CYS scan)
    peptide_chain = min(polymers,
                        key=lambda c: sum(1 for r in c if r.het_flag != "H"))
    receptor_name = next(c.name for c in polymers if c is not peptide_chain)
    linker_chain_name = None
    for chain in st[0]:
        if chain.name == receptor_name:
            continue
        if any(r.name == linker_ccd for r in chain):
            linker_chain_name = chain.name
            break
    if linker_chain_name is None:
        return 0
    anchors = BICYCLIC_LINKER_ATOM_MAP.get(str(linker_ccd).upper()) or []
    if cys_positions is not None:
        wanted = {int(p) + 1 for p in cys_positions}
        sg_resnums = [int(r.seqid.num) for r in peptide_chain
                      if r.name == "CYS" and int(r.seqid.num) in wanted]
    else:
        sg_resnums = [int(r.seqid.num) for r in peptide_chain if r.name == "CYS"]
    sg_resnums.sort()
    if len(sg_resnums) != len(anchors):
        raise ValueError(
            f"staged bicyclic complex has {len(sg_resnums)} Cys-SG but linker "
            f"{linker_ccd} needs {len(anchors)} anchors"
        )
    link_lines = []
    for sg_resnum, anchor in zip(sg_resnums, anchors):
        link_lines.append(_pdb_link_line(
            "SG", "CYS", peptide_chain.name, sg_resnum,
            anchor, linker_ccd, linker_chain_name, 1,
        ))
    # insert before the trailing END record — PDB parsers stop at END
    lines = [line for line in text.splitlines() if line.strip()]
    while lines and lines[-1].strip() in ("END", "ENDMDL"):
        lines.pop()
    with staged_path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")
        for line in link_lines:
            handle.write(line + "\n")
        handle.write("END\n")
    return len(link_lines)


def _cyclic_headtail_bond_report(structure_path: Path) -> Dict[str, Any]:
    """Measure whether a cyclic peptide's head-to-tail bond survived the
    refine: N(residue 1) <-> C(residue L) on the second-largest polymer
    chain (the peptide, mirror-space contract). A peptide N-C amide bond is
    ~1.33 A; 2.5 A is the reporting cut-off — anything beyond means the ring
    opened (or never closed) and the product is not cyclic."""
    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    polymer = sorted(
        (c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3),
        key=lambda c: -sum(1 for r in c if r.het_flag != "H"),
    )
    if len(polymer) < 2:
        return {"chain": None, "n_c_distance": None, "all_bonded": False}
    # the peptide is the SHORTEST polymer chain (multi-chain receptors put
    # receptor copies at every length tier; the old polymer[1] pick read
    # a second receptor copy's termini -- a constant ~15.8 A on the RANKL
    # trimer, phantom-"opening" every ring,
    pep = polymer[-1]
    residues = [r for r in pep if r.het_flag != "H"]
    if len(residues) < 4:
        return {"chain": pep.name, "n_c_distance": None, "all_bonded": False}
    n_atom = residues[0].find_atom("N", "*")
    c_atom = residues[-1].find_atom("C", "*")
    if n_atom is None or c_atom is None:
        return {"chain": pep.name, "n_c_distance": None, "all_bonded": False}
    dist = float(n_atom.pos.dist(c_atom.pos))
    return {
        "chain": pep.name,
        "first_residue": int(residues[0].seqid.num),
        "last_residue": int(residues[-1].seqid.num),
        "n_c_distance": round(dist, 2),
        "all_bonded": dist <= 2.5,
    }


def _rebuild_single_atom_linker_anchor(structure_path: Path, *, prefer_code: str | None = None) -> Optional[Dict[str, Any]]:
    """单原子链接器（BS3=Bi）锚点几何重建：以三个匹配 Cys-SG 的外心、
    Bi-S 静息长度重摆锚点，再做锚点+SG 受限微松弛。原位改写 cif/pdb；
    多原子链接器或拓扑不完整时返回 None。
    """
    path = Path(structure_path)
    st = gemmi.read_structure(str(path))
    st.setup_entities()
    anchor_map = _structure_linker_anchor_map(st, prefer_code=prefer_code)
    linker_res = None
    linker_chain = None
    for chain in st[0]:
        for residue in chain:
            if residue.name in anchor_map:
                linker_res = residue
                linker_chain = chain
                break
        if linker_res is not None:
            break
    if linker_res is None:
        return None
    heavy = [a for a in linker_res if a.element != gemmi.Element("H")]
    if len(heavy) != 1:
        return None  # multi-atom linker: CCD geometry already governs it
    anchor = heavy[0]
    rest = 2.55 if anchor.name == "BI" else 1.82

    polymer_lens = {
        c.name: sum(1 for r in c if r.het_flag != "H") for c in st[0]}
    peptide_chain_name = min(
        (c for c, n in polymer_lens.items()
         if n >= 3 and c != linker_chain.name),
        key=lambda c: polymer_lens[c], default=None)
    if peptide_chain_name is None:
        return None
    pep = st[0][peptide_chain_name]

    sgs = [(r.seqid.num, r.find_atom("SG", "*"))
           for r in pep if r.name == "CYS" and r.find_atom("SG", "*") is not None]
    anchors_wanted = anchor_map[linker_res.name]
    remaining = list(sgs)
    matched: List[Tuple[int, Any]] = []
    for _ in anchors_wanted:  # one-to-one greedy; symmetric linkers repeat
        if not remaining:
            break
        best = min(remaining, key=lambda item: float(
            np.linalg.norm(np.array([item[1].pos.x, item[1].pos.y, item[1].pos.z])
                           - np.array([anchor.pos.x, anchor.pos.y, anchor.pos.z]))))
        remaining.remove(best)
        matched.append(best)
    if len(matched) != 3:
        return None

    pep_pts = np.array([
        [a.pos.x, a.pos.y, a.pos.z]
        for r in pep for a in r
        if a.element != gemmi.Element("H")
        and not (r.name == "CYS" and a.name == "SG")])
    matched_ids = {num for num, _ in matched}

    def _self_clearance(point: np.ndarray) -> float:
        if len(pep_pts) == 0:
            return float("inf")
        d = np.linalg.norm(pep_pts - point, axis=1)
        return float(d.min())

    p1 = np.array([matched[0][1].pos.x, matched[0][1].pos.y, matched[0][1].pos.z])
    p2 = np.array([matched[1][1].pos.x, matched[1][1].pos.y, matched[1][1].pos.z])
    p3 = np.array([matched[2][1].pos.x, matched[2][1].pos.y, matched[2][1].pos.z])
    centroid = (p1 + p2 + p3) / 3.0
    normal = np.cross(p2 - p1, p3 - p1)
    n_len = float(np.linalg.norm(normal))
    normal = normal / n_len if n_len > 1e-8 else np.array([0.0, 0.0, 1.0])
    # Circumcenter of the SG triangle (equidistant from all three vertices):
    # solve |O-p1| = |O-p2| = |O-p3| with O in the triangle plane. Placing at
    # circumcenter + normal*h puts the anchor exactly `rest` from every SG
    # before relaxation. h collapses to 0 once R >= rest (ring too small to
    # host the anchor at the ideal length — the gate judges the outcome).
    b_len = float(np.linalg.norm(p2 - p3))
    c_len = float(np.linalg.norm(p1 - p3))
    a_len = float(np.linalg.norm(p1 - p2))
    s = (a_len + b_len + c_len) / 2.0
    area2 = max(s * (s - a_len) * (s - b_len) * (s - c_len), 0.0)
    area = float(np.sqrt(area2))
    circum_r = (a_len * b_len * c_len) / (4.0 * area) if area > 1e-6 else 0.0
    h = float(np.sqrt(max(rest * rest - circum_r * circum_r, 0.0)))
    # in-plane offset from centroid to circumcenter: barycentric weights
    # sin(2A):sin(2B):sin(2C) computed from edge lengths
    w1 = a_len * a_len * (b_len * b_len + c_len * c_len - a_len * a_len)
    w2 = b_len * b_len * (c_len * c_len + a_len * a_len - b_len * b_len)
    w3 = c_len * c_len * (a_len * a_len + b_len * b_len - c_len * c_len)
    w_sum = w1 + w2 + w3
    if abs(w_sum) > 1e-9:
        circumcenter = (w1 * p1 + w2 * p2 + w3 * p3) / w_sum
    else:
        circumcenter = centroid
    # numerical guard: keep the offset in-plane (component along the normal
    # would tilt the placement) and within the triangle's scale
    in_plane = circumcenter - centroid
    in_plane = in_plane - normal * float(np.dot(in_plane, normal))

    candidates = []
    for sign in (1.0, -1.0):
        point = circumcenter + normal * (h * sign)
        candidates.append((_self_clearance(point), sign, point))
    candidates.sort(key=lambda row: row[0], reverse=True)
    clearance0, chosen_sign, point = candidates[0]
    anchor.pos = gemmi.Position(*(point.tolist()))

    # capped joint micro-relaxation: anchor carries most of the correction,
    # SGs may drift at most ~1.5 A total (their residues stay untouched —
    # moving a cysteine's SG without its residue would tear the backbone)
    sg_positions = {num: np.array([a.pos.x, a.pos.y, a.pos.z]) for num, a in matched}
    sg_start = {num: v.copy() for num, v in sg_positions.items()}
    bond_lo, bond_hi = (2.20, 2.90) if anchor.name == "BI" else (1.75, 2.05)
    for _ in range(60):
        max_v = 0.0
        p_an = np.array([anchor.pos.x, anchor.pos.y, anchor.pos.z])
        for num, _sg in matched:
            p_sg = sg_positions[num]
            d = float(np.linalg.norm(p_an - p_sg))
            target = float(np.clip(d, rest, rest))  # pull toward rest length
            viol = d - target
            max_v = max(max_v, abs(viol))
            if abs(viol) < 1e-3:
                continue
            u = (p_an - p_sg) / max(d, 1e-8)
            # d > rest (viol>0): 锚沿 -u 向 SG 收拢, SG 沿 +u 向锚收拢;
            # d < rest (viol<0): 双方反向分开——符号与距离变化量对应
            anchor_step = np.clip(-0.7 * viol * u, -0.3, 0.3)
            new_an = p_an + anchor_step
            sg_step = np.clip(0.3 * viol * u, -0.15, 0.15)
            new_sg = p_sg + sg_step
            drift = np.linalg.norm(new_sg - sg_start[num])
            if drift <= 1.5:
                sg_positions[num] = new_sg
            anchor.pos = gemmi.Position(*(new_an.tolist()))
            p_an = new_an
        # self-clash repulsion for the anchor
        p_an = np.array([anchor.pos.x, anchor.pos.y, anchor.pos.z])
        if len(pep_pts):
            d = np.linalg.norm(pep_pts - p_an, axis=1)
            j = int(np.argmin(d))
            if d[j] < 2.2:
                u = p_an - pep_pts[j]
                n = float(np.linalg.norm(u))
                u = u / n if n > 1e-8 else np.array([0.0, 0.0, 1.0])
                p_an = pep_pts[j] + 2.2 * u
                anchor.pos = gemmi.Position(*p_an.tolist())
                max_v = max(max_v, 2.2 - float(d[j]))
        for num, atom in matched:
            atom.pos = gemmi.Position(*sg_positions[num].tolist())
        if max_v < 5e-3:
            break

    if path.suffix.lower() in {".cif", ".mmcif"}:
        st.make_mmcif_document().write_file(str(path))
    else:
        st.write_pdb(str(path))

    final_sgs = [float(np.linalg.norm(
        np.array([a.pos.x, a.pos.y, a.pos.z])
        - np.array([anchor.pos.x, anchor.pos.y, anchor.pos.z])))
        for _, a in matched]
    return {
        "linker": linker_res.name,
        "side": "+" if chosen_sign > 0 else "-",
        "circumradius_a": round(circum_r, 2),
        "self_clash_min_a": round(_self_clearance(np.array(
            [anchor.pos.x, anchor.pos.y, anchor.pos.z])), 3),
        "anchor_sg_a": [round(x, 2) for x in final_sgs],
        "sg_drift_a": {str(num): round(float(np.linalg.norm(
            sg_positions[num] - sg_start[num])), 2) for num, _ in matched},
        "initial_clearance_a": round(clearance0, 3),
    }


def _structure_linker_anchor_map(st, prefer_code: str | None = None) -> Dict[str, List[str]]:
    """Registry anchors overlaid with single-atom metal anchors shipped in
    the structure. BS3 (bare Bi3+) is not offered as a design linker — its
    symmetric arms cannot express three distinct covalent bonds — but
    custom-CCD bicyclics arrive with it, and the bond gate / anchor rebuild
    must still measure Bi-S coordination instead of reporting "no linker".

    A registry residue always wins: a stray Bi ion must never shadow the
    real linker (or pass the gate on fake coordination). Overlay codes are
    only collected when no registry residue exists, and — when the caller
    knows the design's linker CCD — only that code qualifies."""
    registry_present = any(
        residue.name in BICYCLIC_LINKER_ATOM_MAP
        for chain in st[0] for residue in chain)
    overlay: Dict[str, List[str]] = {}
    if not registry_present:
        want = str(prefer_code).strip().upper() if prefer_code else None
        for chain in st[0]:
            for residue in chain:
                code = residue.name
                if code in overlay:
                    continue
                if want and code.upper() != want:
                    continue
                heavy = [a.name for a in residue
                         if a.element != gemmi.Element("H")]
                # one physical atom, three anchor slots (['BI','BI','BI'])
                if len(heavy) == 1 and heavy[0].upper() == "BI":
                    overlay[code] = [heavy[0].upper()] * 3
    if not overlay:
        return BICYCLIC_LINKER_ATOM_MAP
    return {**BICYCLIC_LINKER_ATOM_MAP, **overlay}


def _dpeptide_linker_bond_report(structure_path: Path, *, prefer_code: str | None = None) -> Dict[str, Any]:
    """逐锚点测量链接器-Cys SG 距离；C-S 键长约 1.8 A，报告阈值 2.5 A
    （Bi 配位窗口另计）。"""
    st = gemmi.read_structure(str(structure_path))
    st.setup_entities()
    anchor_map = _structure_linker_anchor_map(st, prefer_code=prefer_code)

    linker_res = None
    linker_code = ""
    for chain in st[0]:
        for residue in chain:
            if residue.het_flag == "H" and residue.name in anchor_map:
                linker_res = residue
                linker_code = residue.name
                break
        if linker_res is not None:
            break
    if linker_res is None:
        return {"linker": None, "all_bonded": None, "bonds": []}

    anchors = anchor_map[linker_code]
    # SG pool: peptide-side chains only. The receptor carries its own Cys;
    # letting a receptor SG into the pool lets the one-to-one greedy match
    # a receptor Cys to an anchor and mis-judge the design linkage.
    polymer_lens = {
        c.name: sum(1 for r in c if r.het_flag != "H") for c in st[0]
    }
    linker_chain_name = next(
        (c.name for c in st[0]
         if any(r.name in anchor_map for r in c)), None)
    peptide_chain_name = min(
        (c for c, n in polymer_lens.items()
         if n >= 3 and c != linker_chain_name),
        key=lambda c: polymer_lens[c], default=None)
    sg_atoms = []
    for chain in st[0]:
        if peptide_chain_name is not None and chain.name != peptide_chain_name:
            continue
        for residue in chain:
            sg = residue.find_atom("SG", "*")
            if sg is not None and residue.name == "CYS":
                sg_atoms.append((residue.seqid.num, sg))
    bonds = []
    remaining = list(sg_atoms)  # one-to-one: symmetric linkers repeat an
    for anchor_name in anchors:  # anchor atom; nearest-per-anchor would let
        anchor_atom = linker_res.find_atom(anchor_name, "*")  # one SG win all
        if anchor_atom is None or not remaining:
            bonds.append({"anchor": anchor_name, "cys_resnum": None,
                          "distance": None, "bonded": False})
            continue
        best = min(
            remaining,
            key=lambda item: (
                (item[1].pos.x - anchor_atom.pos.x) ** 2
                + (item[1].pos.y - anchor_atom.pos.y) ** 2
                + (item[1].pos.z - anchor_atom.pos.z) ** 2),
        )
        remaining.remove(best)
        dist = math.sqrt(
            (best[1].pos.x - anchor_atom.pos.x) ** 2
            + (best[1].pos.y - anchor_atom.pos.y) ** 2
            + (best[1].pos.z - anchor_atom.pos.z) ** 2)
        # Bonded window per anchor chemistry: C-S (SEZ/29N) ~1.82 A,
        # Bi-S coordination (BS3) ~2.4-2.7 A. Below ~1.2 A (C-S) or
        # ~1.6 A (Bi-S) the pair is an overlap, not a bond.
        lo, hi = (1.6, 3.2) if anchor_name == "BI" else (1.2, 2.5)
        bonds.append({
            "anchor": anchor_name,
            "cys_resnum": best[0],
            "distance": round(dist, 3),
            "bonded": bool(lo <= dist <= hi),
        })
    distinct = len({b["cys_resnum"] for b in bonds if b["cys_resnum"] is not None})
    # 锚点自碰撞门禁:链接器锚(如 BS3 的 Bi)压进自身肽骨架(键长合格但物理
    # 重叠)在化学上不可用, 与键长一同判失败。
    self_clash_min = None
    if linker_res is not None:
        anchor_atoms = [a for a in linker_res if a.element != gemmi.Element("H")]
        other_heavy = [
            a for chain in st[0] if chain.name != linker_chain_name
            for r in chain for a in r
            if a.element != gemmi.Element("H")
            and not (chain.name == peptide_chain_name
                     and r.name == "CYS" and a.name == "SG")]
        if anchor_atoms and other_heavy:
            import numpy as _np_clash
            pts = _np_clash.array([[a.pos.x, a.pos.y, a.pos.z] for a in other_heavy])
            for a in anchor_atoms:
                d = _np_clash.linalg.norm(
                    pts - _np_clash.array([a.pos.x, a.pos.y, a.pos.z]), axis=1)
                m = float(d.min())
                self_clash_min = m if self_clash_min is None else min(self_clash_min, m)
    self_clash = self_clash_min is not None and self_clash_min < 2.0
    return {
        "linker": linker_code,
        "all_bonded": bool(all(b["bonded"] for b in bonds)
                           and distinct == len(bonds)
                           and not self_clash),
        "distinct_cys": distinct,
        "anchor_self_clash_min_a": (round(self_clash_min, 3)
                                    if self_clash_min is not None else None),
        "self_clash": self_clash,
        "bonds": bonds,
        "cutoff_a": SEZ_RING_BOND_CUTOFF_A,
    }

