"""Coordinate alignment between user structures and the Protenix
assembled atom table."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

def align_init_coords(
    input_json_path: Path,
    chains: list[ProteinChainData],
    ligand_mol: Chem.Mol,
    require_complete: bool = False,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Align user coordinates onto Protenix's assembled atom order.

    Runs the featurization pipeline as the inference dataloader
    (SampleDictToFeatures) on the input json, so the atom order is guaranteed
    to match what the model will see.

    Returns (coords [N_atom,3] float32, mask [N_atom] float32, info dict with
    ligand row indices).
    """
    from protenix.data.inference.json_to_feature import SampleDictToFeatures

    with open(input_json_path, "r", encoding="utf-8") as fh:
        job = json.load(fh)[0]
    sample2feat = SampleDictToFeatures(job, extract_features_for_tfg=False)
    feat, atom_array, _ = sample2feat.get_feature_dict()

    n = int(atom_array.array_length())
    coords = np.zeros((n, 3), dtype=np.float32)
    mask = np.zeros(n, dtype=np.float32)

    asym = np.asarray(atom_array.asym_id_int)
    res_id = np.asarray(atom_array.res_id)
    atom_names = [str(a) for a in np.asarray(atom_array.atom_name)]

    # Chain (asym) blocks appear in input.json entity order: proteins first,
    # then the ligand. Map each asym value to its entity index by first row.
    asym_to_entity: dict[int, int] = {}
    for i, value in enumerate(asym):
        v = int(value)
        if v not in asym_to_entity:
            asym_to_entity[v] = len(asym_to_entity)

    for i in range(n):
        entity = asym_to_entity[int(asym[i])]
        if entity < len(chains):
            chain = chains[entity]
            key_res = int(res_id[i]) - 1
            if not 0 <= key_res < len(chain.residues):
                continue
            pos = chain.residues[key_res]["atoms"].get(atom_names[i])
            if pos is not None:
                coords[i] = pos
                mask[i] = 1.0

    ligand_entity = len(chains)
    ligand_rows = np.array(
        [i for i in range(n) if asym_to_entity[int(asym[i])] == ligand_entity],
        dtype=np.int64,
    )
    conf = ligand_mol.GetConformer()
    ligand_xyz = np.array(
        [list(conf.GetAtomPosition(i)) for i in range(ligand_mol.GetNumAtoms())],
        dtype=np.float32,
    )
    if len(ligand_rows) == ligand_xyz.shape[0]:
        coords[ligand_rows] = ligand_xyz
        mask[ligand_rows] = 1.0
    else:
        print(
            f"[Warning] Ligand atom mismatch (mol={ligand_xyz.shape[0]}, "
            f"assembled={len(ligand_rows)}); ligand starts from noise."
        )
        ligand_rows = np.array([], dtype=np.int64)

    n_missing = int((mask == 0).sum())
    if require_complete and n_missing:
        raise ValueError(
            f"score mode requires every assembled atom to come from the "
            f"input structure; {n_missing} atom(s) unmatched (coordinate "
            "pass-through would fabricate or zero them). Strip hydrogens / "
            "complete side chains in the input, or rescore a full-atom model."
        )
    if n_missing:
        completed = _complete_missing_atoms(coords, mask, feat)
        if completed:
            print(f"[Info] Rebuilt {completed}/{n_missing} atom(s) absent "
                  f"from the source from the CCD reference geometry.")

    print(
        f"[Info] Init coords aligned: {int(mask.sum())}/{n} atoms "
        f"(ligand rows: {len(ligand_rows)})."
    )
    # Same atom-table contract as align_complex_init_coords: pocket guidance
    # and covalent-band builders key on (asym letter, res_id ordinal, atom name)
    return coords, mask, {
        "ligand_rows": ligand_rows,
        "asym": asym,
        "res_id": res_id,
        "atom_names": np.asarray(atom_names),
        # same assembled-table contract as align_complex_init_coords
        # (pocket validation and the VDW shell read element types)
        "elements": np.asarray(
            [str(e) for e in np.asarray(atom_array.element)]),
    }



# Element pairs that form covalent bonds in biomolecules (protein/peptide
# chemistry; keyed as sorted element-pair strings). O-O, N-N, S-O etc. are
# never bonded here — a sub-cutoff pair of those elements marks a deformed
# input, not a bond to preserve.

# Standard amino-acid heavy-atom bond graphs (per residue type). Chemistry,
# not geometry: bonds exist because of the residue's covalent structure, so a
# deformed or partially rebuilt input cannot lose a bond from the constraint
# set the way a distance cut-off can. Peptide C(i)-N(i+1) links and the C=O
# carbonyl are added from the backbone.
# Geometry reference tables live in geometry_tables.py (single source of truth)
from core.geometry_tables import (
    STD_AA_BONDS as _STD_AA_BONDS,
    ideal_bond_length as _ideal_bond_length,
    ideal_bond_length_residue as _ideal_bond_length_residue,
    ideal_bond_angle as _ideal_bond_angle,
    RESIDUE_BOND as _RESIDUE_BOND,
)

# Amide peptide bond rest length (Engh & Huber)
AMIDE = 1.33

def _structure_atom_lookup(
    structure_path: Path,
) -> dict[tuple[str, int, str], tuple[float, float, float]]:
    """{(chain, residue_ordinal, atom_name): (x, y, z)} heavy atoms.

    The ordinal is the residue's 1-based position within its chain in file
    order. The assembled atom table numbers each entity's residues 1..n by
    sequence position, so ordinals — not the source file's seqid — are the
    correspondence key (crystal files routinely start at author numbering
    26, 2, ...; keying by seqid silently shifts the sequence against the
    coordinates).
    """
    import gemmi

    structure = gemmi.read_structure(str(structure_path))
    structure.setup_entities()
    lookup: dict[tuple[str, int, str], tuple[float, float, float]] = {}
    for chain in structure[0]:
        for ordinal, residue in enumerate(chain, start=1):
            for atom in residue:
                name = atom.name.strip()
                if atom.element == gemmi.Element("H") or name.startswith("H"):
                    continue
                lookup[(chain.name, ordinal, name)] = (
                    atom.pos.x, atom.pos.y, atom.pos.z,
                )
    return lookup


def _complete_missing_atoms(
    coords: np.ndarray,
    mask: np.ndarray,
    feat: dict,
) -> int:
    """Rebuild atoms absent from the source from the CCD reference geometry.

    A noise-start atom is isolated from its residue and diffusion at the
    small-sigma dock ladder never reliably relocates it (atoms parked near
    the coordinate origin in redock outputs). Instead: rigidly fit each
    residue's KNOWN atoms onto their ref_pos (the engine's own CCD
    conformer) and carry the missing atoms through the same transform.
    Deterministic, exact for the known part, ideal geometry for the rest.
    Mutates coords/mask in place; returns the number of atoms completed.
    """
    a2t = feat["atom_to_token_idx"]
    if a2t.dim() >= 2:
        a2t = a2t[0] if a2t.shape[0] == 1 else a2t.argmax(dim=-1)
    token_of = np.asarray(a2t.detach().cpu().long().numpy()).flatten()
    ref = np.asarray(feat["ref_pos"].detach().cpu().float().numpy())

    by_token: dict[int, list[int]] = {}
    for i in np.where(mask == 0)[0]:
        by_token.setdefault(int(token_of[i]), []).append(int(i))

    completed = 0
    for token, missing in by_token.items():
        rows = np.where((token_of == token) & (mask > 0))[0]
        if len(rows) < 3:
            continue
        src = ref[rows]
        if np.linalg.matrix_rank(src - src.mean(0)) < 2:
            continue
        m = src.mean(0)
        t = coords[rows].mean(0)
        H = (src - m).T @ (coords[rows] - t)
        U, _, Vt = np.linalg.svd(H)
        d = np.sign(np.linalg.det(Vt.T @ U.T))
        R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
        for i in missing:
            coords[i] = (ref[i] - m) @ R.T + t
            mask[i] = 1.0
            completed += 1
    return completed


def align_complex_init_coords(
    input_json_path: Path,
    source_structure_path: Path,
    entity_chain_names: list[str],
    require_complete: bool = False,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Align the source complex's coordinates onto the assembled atom order.

    Works for any entity mix (proteins, CCD ligands) by matching
    (chain letter, res_id, atom name) against the staged complex: the
    input.json sequences order defines the auto chain letters (A, B, C, ...),
    and `entity_chain_names` maps each entity to the staged chain it came
    from.

    Returns (coords [N_atom,3] float32, mask [N_atom] float32, info dict with
    per-entity row indices and the assembled atom table).
    """
    from protenix.data.inference.json_to_feature import SampleDictToFeatures

    with open(input_json_path, "r", encoding="utf-8") as fh:
        job = json.load(fh)[0]
    sample2feat = SampleDictToFeatures(job, extract_features_for_tfg=False)
    feat, atom_array, _ = sample2feat.get_feature_dict()

    n = int(atom_array.array_length())
    coords = np.zeros((n, 3), dtype=np.float32)
    mask = np.zeros(n, dtype=np.float32)

    asym = np.asarray(atom_array.asym_id_int)
    res_id = np.asarray(atom_array.res_id)
    atom_names = [str(a) for a in np.asarray(atom_array.atom_name)]

    asym_to_entity: dict[int, int] = {}
    for value in asym:
        v = int(value)
        if v not in asym_to_entity:
            asym_to_entity[v] = len(asym_to_entity)

    lookup = _structure_atom_lookup(Path(source_structure_path))

    # per-entity residue ordinal: the assembled table numbers each entity's
    # residues 1..n in sequence order, so the lookup key is the res_id's
    # rank within its entity (first-appearance order), never the raw res_id
    entity_ordinal: dict[tuple[int, int], int] = {}
    for i in range(n):
        entity = asym_to_entity[int(asym[i])]
        if entity >= len(entity_chain_names):
            continue
        entity_ordinal.setdefault((entity, int(res_id[i])), len(
            {rid for (ent, rid) in entity_ordinal if ent == entity}) + 1)

    matched = 0
    unmatched: list[str] = []
    for i in range(n):
        entity = asym_to_entity[int(asym[i])]
        if entity >= len(entity_chain_names):
            continue
        staged_chain = entity_chain_names[entity]
        ordinal = entity_ordinal[(entity, int(res_id[i]))]
        pos = lookup.get((staged_chain, ordinal, atom_names[i]))
        if pos is not None:
            coords[i] = pos
            mask[i] = 1.0
            matched += 1
        else:
            unmatched.append(f"{staged_chain}#{ordinal}({res_id[i]}){atom_names[i]}")
    if unmatched:
        if require_complete:
            # score mode: the output CIF must be the input structure,
            # bit-exact. A CCD rebuild or a zeroed row would SHIFT residues
            # in the shipped file (measured: user uploads with missing side
            # atoms came back with fabricated/origin-collapsed atoms) —
            # fail loudly instead.
            raise ValueError(
                f"score mode requires every assembled atom to come from "
                f"the input complex; {len(unmatched)} unmatched (first: "
                f"{', '.join(unmatched[:20])}). Complete the side chains "
                "or fix the atom naming/numbering in the input file."
            )
        # atoms absent from the source carry no coordinates; they are
        # rebuilt below from the CCD reference geometry where possible and
        # only the rest stay mask=0 (the sampler's designed unknown-atom
        # start). Callers must never pin a mask=0 row — a zero coordinate
        # row would clamp the atom to the origin.
        print(
            f"[Warning] {len(unmatched)} assembled atom(s) absent from the "
            f"source complex (first: {', '.join(unmatched[:10])})"
        )
        completed = _complete_missing_atoms(coords, mask, feat)
        if completed:
            print(f"[Info] Rebuilt {completed} atom(s) from the CCD reference "
                  f"geometry.")

    entity_rows: dict[int, np.ndarray] = {}
    for entity in range(len(entity_chain_names)):
        entity_rows[entity] = np.array(
            [i for i in range(n) if asym_to_entity[int(asym[i])] == entity],
            dtype=np.int64,
        )

    print(
        f"[Info] Complex init coords aligned: {matched}/{n} atoms "
        f"({len(entity_chain_names)} entities)."
    )
    return coords, mask, {
        "entity_rows": entity_rows,
        "elements": np.asarray(
            [str(e) for e in np.asarray(atom_array.element)]),
        "comp_ids": np.asarray(
            [str(cn) for cn in np.asarray(atom_array.res_name)]),
        "entity_chain_names": entity_chain_names,
        "asym": asym,
        "res_id": res_id,
        "atom_names": np.asarray(atom_names),
        "asym_to_entity": asym_to_entity,
    }


