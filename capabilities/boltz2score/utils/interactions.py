"""Protein-ligand interaction analysis via PLIP.

Takes a Boltz2Score output structure (``best_model.cif``), prepares it for
PLIP (short chain IDs, protonation via OpenBabel), runs the PLIP analysis,
and writes a typed interaction list as JSON for downstream display.

Pipeline: CIF -> PDB (chains remapped to single letters, ligand -> 'L')
-> OpenBabel AddHydrogens(pH 7.4) -> PLIP PDBComplex.analyze()
-> interactions_<record>.json (+ best_interactions.json alias).

PLIP requires protonated structures for hydrogen-bond geometry; salt-bridge
perception is name-based and unaffected by the added hydrogens.
"""

from __future__ import annotations

import json
import math
import os
import string
import tempfile
from pathlib import Path

import gemmi

# Scratch files must never land in /tmp (small root partition) — default to
# the structure's own directory when the caller does not provide one.

# PLIP interaction attribute -> unified type label
_HBOND = "hydrogen_bond"
_TYPE_SOURCES = (
    ("hbonds_pdon", _HBOND),
    ("hbonds_ldon", _HBOND),
    ("hydrophobic_contacts", "hydrophobic"),
    ("saltbridge_lneg", "salt_bridge"),
    ("saltbridge_pneg", "salt_bridge"),
    ("pistacking", "pi_stacking"),
    ("pication_laro", "pi_cation"),
    ("pication_paro", "pi_cation"),
    ("halogen_bonds", "halogen_bond"),
    ("water_bridges", "water_bridge"),
)


def _require_plip():
    try:
        from plip.structure.preparation import PDBComplex  # noqa: F401
        from openbabel import openbabel as ob  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Interaction analysis requires the 'plip' and 'openbabel-wheel' packages. "
            "Install them into the runtime environment (see requirements.txt)."
        ) from exc


def _remap_chains_for_pdb(structure: gemmi.Structure, ligand_chain_name: str | None):
    """Map long Boltz chain IDs to PDB-safe single letters; ligand chain -> 'L'."""
    letters = iter(c for c in string.ascii_uppercase if c != "L")
    mapping: dict[str, str] = {}
    for chain in structure[0]:
        original = chain.name
        is_ligand = (
            ligand_chain_name is not None and original == ligand_chain_name
        ) or (len(list(chain)) == 1)
        mapping[original] = "L" if is_ligand else next(letters, "X")
        chain.name = mapping[original]
    return mapping


def _parse_h_pdb_atom_table(pdb_text: str) -> dict[int, dict[str, object]]:
    """serial -> {chain, resname, resnum, atom_name, element} from PDB lines."""
    table: dict[int, dict[str, object]] = {}
    for line in pdb_text.splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        try:
            serial = int(line[6:11])
        except ValueError:
            continue
        table[serial] = {
            "chain": line[21:22].strip(),
            "resname": line[17:20].strip(),
            "resnum": int(line[22:26]) if line[22:26].strip().isdigit() else None,
            "atom_name": line[12:16].strip(),
            "element": (line[76:78].strip() or line[12:16].strip()[:1]).upper(),
        }
    return table


def _atom_names(table: dict[int, dict[str, object]], idx) -> list[str]:
    if idx is None:
        return []
    entry = table.get(int(idx))
    if entry is None or entry["element"] in {"H", "D"}:
        return []
    name = str(entry["atom_name"]).strip()
    return [name] if name else []


def _finite(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _first_attr(item, names: tuple[str, ...]):
    for name in names:
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def _extract(site, table: dict[int, dict[str, object]], chain_back: dict[str, str]):
    interactions: list[dict[str, object]] = []
    for attr, label in _TYPE_SOURCES:
        for item in getattr(site, attr, []) or []:
            if attr.startswith("hbonds_"):
                # Donor/acceptor assignment depends on which side is protein.
                protisdon = bool(getattr(item, "protisdon", False))
                prot_idx = item.d_orig_idx if protisdon else item.a_orig_idx
                lig_idx = item.a_orig_idx if protisdon else item.d_orig_idx
            else:
                prot_idx = _first_attr(item, ("bsatom_orig_idx", "a_orig_idx", "prot_orig_idx"))
                lig_idx = _first_attr(item, ("ligatom_orig_idx", "lig_orig_idx"))

            reschain = str(getattr(item, "reschain", "") or "").strip()
            restype = str(getattr(item, "restype", "") or "").strip()
            resnr = getattr(item, "resnr", None)
            orig_chain = chain_back.get(reschain, reschain)
            distance = _finite(
                _first_attr(item, ("distance_ad", "distance", "dist"))
            )
            interactions.append(
                {
                    "type": label,
                    "resid": f"{orig_chain}:{restype}{resnr}" if resnr is not None else orig_chain,
                    "restype": restype,
                    "resnr": resnr,
                    "reschain": orig_chain,
                    "distance": distance,
                    "ligand_atoms": _atom_names(table, lig_idx),
                    "protein_atoms": _atom_names(table, prot_idx),
                    "sidechain": bool(getattr(item, "sidechain", False)),
                }
            )
    return interactions


def _pocket_residues(interactions: list[dict[str, object]]):
    best: dict[str, dict[str, object]] = {}
    for item in interactions:
        resid = str(item.get("resid"))
        dist = item.get("distance")
        cur = best.get(resid)
        if cur is None or (dist is not None and (cur["distance"] is None or dist < cur["distance"])):
            best[resid] = {
                "resid": resid,
                "restype": item.get("restype"),
                "resnr": item.get("resnr"),
                "distance": dist,
            }
    residues = sorted(
        best.values(),
        key=lambda r: (r["distance"] is None, r["distance"] if r["distance"] is not None else 0.0),
    )
    return residues


def analyze_structure_interactions(
    structure_path: Path,
    ligand_chain_name: str | None = None,
    temp_dir: Path | None = None,
) -> dict[str, object]:
    """Run PLIP analysis on one structure file, return the typed report."""
    _require_plip()
    from openbabel import openbabel as ob
    from plip.structure.preparation import PDBComplex

    structure = gemmi.read_structure(str(structure_path))
    structure.setup_entities()
    chain_back_map = _remap_chains_for_pdb(structure, ligand_chain_name)
    forward_map = {v: k for k, v in chain_back_map.items()}

    scratch_dir = Path(temp_dir) if temp_dir is not None else structure_path.parent

    fd, pdb_path = tempfile.mkstemp(suffix=".pdb", dir=str(scratch_dir))
    os.close(fd)
    try:
        structure.write_pdb(pdb_path)
        conv = ob.OBConversion()
        conv.SetInAndOutFormats("pdb", "pdb")
        mol = ob.OBMol()
        conv.ReadFile(mol, pdb_path)
        mol.AddHydrogens(False, True, 7.4)
        h_pdb = conv.WriteString(mol)
    finally:
        Path(pdb_path).unlink(missing_ok=True)

    table = _parse_h_pdb_atom_table(h_pdb)

    pc = PDBComplex()
    pc.load_pdb(h_pdb, as_string=True)
    pc.analyze()

    # Prefer the interaction set of the remapped ligand chain; fall back to
    # the largest analyzed small molecule.
    chosen_key = None
    for key in pc.interaction_sets:
        parts = key.split(":")
        if len(parts) >= 2 and parts[1] == "L":
            chosen_key = key
            break
    if chosen_key is None and pc.interaction_sets:
        chosen_key = max(
            pc.interaction_sets,
            key=lambda k: len(pc.interaction_sets[k].ligand.mol.atoms),
        )

    if chosen_key is None:
        return {
            "ligand_chain_id": ligand_chain_name or "",
            "counts": {},
            "interactions": [],
            "pocket_residues": [],
            "note": "PLIP found no ligand to analyze.",
        }

    site = pc.interaction_sets[chosen_key]
    interactions = _extract(site, table, forward_map)
    counts: dict[str, int] = {}
    for item in interactions:
        counts[item["type"]] = counts.get(item["type"], 0) + 1

    return {
        "ligand_chain_id": forward_map.get("L", ligand_chain_name or ""),
        "ligand_resname": str(site.ligand.hetid),
        "counts": counts,
        "interactions": interactions,
        "pocket_residues": _pocket_residues(interactions),
    }


def compute_and_write_interactions(
    output_dir: Path,
    record_id: str,
    ligand_chain_name: str | None = None,
) -> dict[str, object] | None:
    """Analyze the best-scoring output structure and write JSON artifacts."""
    result_dir = Path(output_dir) / record_id
    structure_path = None
    for name in ("best_model.cif", "best_model.mmcif", "best_model.pdb"):
        candidate = result_dir / name
        if candidate.exists():
            structure_path = candidate
            break
    if structure_path is None:
        return None

    report = analyze_structure_interactions(
        structure_path, ligand_chain_name, temp_dir=result_dir
    )
    report["record_id"] = record_id

    payload = json.dumps(report, indent=2, allow_nan=False) + "\n"
    out_path = result_dir / f"interactions_{record_id}.json"
    out_path.write_text(payload)
    (result_dir / "best_interactions.json").write_text(payload)
    return report
