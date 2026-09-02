"""Utilities for flexible docking (dock mode).

Generates 3-D conformers from SMILES, places them at a user-specified
binding-pocket centre, and writes a multi-molecule SDF ready for
Boltz2Score's anchored-refinement pipeline.

Three mutually exclusive ways to define the pocket centre:
  1. ``--pocket_ligand <file>`` — centroid of a reference ligand
     (analogous to gnina ``--autobox_ligand``).
  2. ``--pocket_center "x,y,z"`` — explicit Cartesian coordinates.
  3. ``--pocket_residues "A:100,A:101,…"`` — centroid of the Cα atoms
     of the listed residues.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import gemmi
import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem


# ── Conformer generation ────────────────────────────────────────────


def generate_conformer_from_smiles(smiles: str, seed: int = 42) -> Chem.Mol | None:
    """Embed *smiles* with ETKDGv3 and minimize with MMFF94.

    Embedding is attempted deterministic-seeded first, then retried with
    random start coordinates (macrocycles and long flexible chains often
    fail the deterministic pass).  Raises for SMILES RDKit cannot parse or
    chemistry MMFF94 has no parameters for; returns None only when both
    embedding attempts fail.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"RDKit cannot parse SMILES: {smiles!r}")
    mol = Chem.AddHs(mol)

    params = AllChem.ETKDGv3()
    params.randomSeed = int(seed) & 0x7FFFFFFF
    if AllChem.EmbedMolecule(mol, params) == -1:
        params_retry = AllChem.ETKDGv3()
        params_retry.useRandomCoords = True
        params_retry.randomSeed = int(seed) & 0x7FFFFFFF
        if AllChem.EmbedMolecule(mol, params_retry) == -1:
            return None

    if mol.GetNumAtoms() == 1 and mol.GetNumBonds() == 0:
        # Single-atom species (metal ions): no MMFF minimization is possible
        # or needed — the atom has no internal degrees of freedom.
        return Chem.RemoveHs(mol)
    if not AllChem.MMFFHasAllMoleculeParams(mol):
        raise ValueError(f"MMFF94 has no parameters for SMILES: {smiles!r}")
    AllChem.MMFFOptimizeMolecule(mol, maxIters=200)
    return Chem.RemoveHs(mol)


# ── Pocket-centre extraction ────────────────────────────────────────


@dataclass(frozen=True)
class PocketCenter:
    """3-D Cartesian centre of the binding pocket."""

    x: float
    y: float
    z: float

    def as_array(self) -> np.ndarray:
        return np.array([self.x, self.y, self.z], dtype=float)

    def __str__(self) -> str:
        return f"({self.x:.2f}, {self.y:.2f}, {self.z:.2f})"


def parse_pocket_center(xyz_string: str) -> PocketCenter:
    """Parse an explicit ``"x,y,z"`` coordinate string."""
    parts = [p.strip() for p in str(xyz_string).split(",") if p.strip()]
    if len(parts) != 3:
        raise ValueError(
            f"--pocket_center expects 'x,y,z' (3 numbers, comma-separated). Got: {xyz_string!r}"
        )
    try:
        x, y, z = (float(p) for p in parts)
    except ValueError as exc:
        raise ValueError(
            f"--pocket_center values must be numeric. Got: {xyz_string!r}"
        ) from exc
    return PocketCenter(x, y, z)


def extract_pocket_center_from_ligand(ligand_file: Path) -> PocketCenter:
    """Compute the heavy-atom centroid of a reference ligand file.

    Accepts SDF, MOL2, or PDB.
    """
    ligand_file = Path(ligand_file).expanduser().resolve()
    if not ligand_file.exists():
        raise FileNotFoundError(f"Pocket ligand file not found: {ligand_file}")
    suffix = ligand_file.suffix.lower()

    if suffix in (".sdf", ".sd", ".mol"):
        suppl = Chem.SDMolSupplier(str(ligand_file), removeHs=True)
        mol = next((m for m in suppl if m), None)
    elif suffix == ".mol2":
        mol = Chem.MolFromMol2File(str(ligand_file), removeHs=True)
    elif suffix == ".pdb":
        mol = Chem.MolFromPDBFile(str(ligand_file), removeHs=True)
    else:
        raise ValueError(
            f"Unsupported pocket ligand format {suffix!r}. Use .sdf/.mol2/.pdb."
        )

    if mol is None or mol.GetNumAtoms() == 0:
        raise ValueError(
            f"Could not read a valid ligand from {ligand_file}. "
            "Ensure it contains a 3-D molecule."
        )
    if mol.GetNumConformers() == 0:
        raise ValueError(f"Pocket ligand {ligand_file} has no 3-D conformer.")

    conf = mol.GetConformer()
    coords = np.array(
        [list(conf.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())],
        dtype=float,
    )
    centroid = coords.mean(axis=0)
    return PocketCenter(float(centroid[0]), float(centroid[1]), float(centroid[2]))


def _parse_residue_specs(spec_string: str) -> list[tuple[str, int]]:
    """Parse "A:100,A:101,B:200" into [("A", 100), ("A", 101), ("B", 200)]."""
    entries: list[tuple[str, int]] = []
    for token in str(spec_string).split(","):
        token = token.strip()
        if not token:
            continue
        match = re.match(r"^([A-Za-z]+):(\d+)$", token)
        if not match:
            raise ValueError(
                f"Invalid residue spec {token!r}. Expected format 'CHAIN:RESNUM', e.g. 'A:100'."
            )
        entries.append((match.group(1).upper(), int(match.group(2))))
    if not entries:
        raise ValueError("No valid residue specifications found.")
    return entries


def extract_pocket_center_from_residues(
    protein_file: Path,
    residue_specs: str,
) -> PocketCenter:
    """Compute the Cα centroid of the specified pocket residues."""
    specs = _parse_residue_specs(residue_specs)
    protein_file = Path(protein_file).expanduser().resolve()

    st = gemmi.read_structure(str(protein_file))
    target = {(chain, resnum) for chain, resnum in specs}
    found_coords: list[np.ndarray] = []
    found_keys: set[tuple[str, int]] = set()

    # First model only: multi-model (NMR) files would otherwise contribute
    # one Cα per model per residue.
    for chain in st[0]:
        chain_name = chain.name.strip().upper()
        for res in chain:
            key = (chain_name, res.seqid.num)
            if key in target and key not in found_keys:
                for atom in res:
                    if atom.name.strip().upper() == "CA":
                        found_coords.append(np.array([atom.pos.x, atom.pos.y, atom.pos.z]))
                        found_keys.add(key)
                        break

    if not found_coords:
        available = sorted({
            (c.name.strip().upper(), r.seqid.num)
            for c in st[0] for r in c
        })[:20]
        raise ValueError(
            f"No Cα atoms found for the specified residues {sorted(target)}. "
            f"Available (first 20): {available}."
        )

    missing = sorted(target - found_keys)
    if missing:
        # Fail loudly: silently dropped residues would shift the pocket box.
        raise ValueError(
            f"Pocket residues not found in the structure: {missing}. "
            f"Specified {len(target)} residues, found {len(found_keys)}."
        )

    centroid = np.mean(found_coords, axis=0)
    return PocketCenter(float(centroid[0]), float(centroid[1]), float(centroid[2]))


def resolve_pocket_center(args: argparse.Namespace) -> PocketCenter:
    """Determine the pocket centre from whichever CLI flag was supplied.

    Supports four methods (validation ensures exactly one is set):
      1. ``--center_x/--center_y/--center_z``
      2. ``--pocket_center "x,y,z"`` (shorthand)
      3. ``--pocket_ligand <file>``
      4. ``--pocket_residues "A:100,..."``
    """
    if args.center_x is not None:
        return PocketCenter(
            float(args.center_x), float(args.center_y), float(args.center_z)
        )
    if args.pocket_center:
        return parse_pocket_center(args.pocket_center)
    if args.pocket_ligand:
        return extract_pocket_center_from_ligand(Path(args.pocket_ligand))
    if args.pocket_residues:
        return extract_pocket_center_from_residues(
            Path(args.protein_file), args.pocket_residues
        )
    raise ValueError(
        "Dock mode requires a pocket definition. "
        "Provide one of: --center_x/y/z, --pocket_center, --pocket_ligand, or --pocket_residues."
    )


def resolve_pocket_radius(args: argparse.Namespace) -> float:
    """Determine the effective pocket search radius.

    If ``--size_x/y/z`` is provided, the radius is
    computed as half the maximum box dimension (ensures all residues
    inside the box are captured).  Otherwise falls back to
    ``--pocket_radius`` (default 7.0 Å).
    """
    sx = args.size_x
    sy = args.size_y
    sz = args.size_z
    if sx is not None and sy is not None and sz is not None:
        return float(max(sx, sy, sz)) / 2.0
    return float(args.pocket_radius)


# ── Conformer placement ─────────────────────────────────────────────


def place_conformer_at_center(mol: Chem.Mol, pocket_center: PocketCenter) -> Chem.Mol:
    """Translate *mol* so its heavy-atom centroid coincides with *pocket_center*."""
    mol = Chem.Mol(mol)
    conf = mol.GetConformer()
    coords = np.array(
        [list(conf.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())],
        dtype=float,
    )
    current_centroid = coords.mean(axis=0)
    shift = pocket_center.as_array() - current_centroid
    for i in range(mol.GetNumAtoms()):
        pos = conf.GetAtomPosition(i)
        conf.SetAtomPosition(
            i,
            (pos.x + shift[0], pos.y + shift[1], pos.z + shift[2]),
        )
    return mol


# ── SMILES parsing ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SmilesEntry:
    """A single SMILES record from user input."""

    smiles: str
    name: str


def parse_smiles_input(
    smiles: str | None = None,
    smiles_file: str | None = None,
) -> list[SmilesEntry]:
    """Parse SMILES from a direct string or a ``.smi``/``.csv`` file.

    ``.smi`` format: whitespace- or tab-separated ``SMILES  name`` per line.
    ``.csv`` format: must contain a ``SMILES`` column; ``Name``/``ID`` optional.
    """
    entries: list[SmilesEntry] = []

    if smiles:
        entries.append(SmilesEntry(smiles=smiles.strip(), name="ligand_1"))
        return entries

    if not smiles_file:
        return entries

    path = Path(smiles_file).expanduser().resolve()
    suffix = path.suffix.lower()

    if suffix == ".csv":
        with open(path, newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames or "SMILES" not in [f.upper() for f in reader.fieldnames]:
                raise ValueError(
                    f"CSV file {path.name} must contain a 'SMILES' column. "
                    f"Found columns: {reader.fieldnames}"
                )
            # Case-insensitive column lookup
            col_map = {f.upper(): f for f in reader.fieldnames}
            smiles_col = col_map["SMILES"]
            name_col = col_map.get("NAME") or col_map.get("ID") or col_map.get("TITLE")
            for i, row in enumerate(reader, start=1):
                smi = (row.get(smiles_col) or "").strip()
                if not smi:
                    continue
                name = (row.get(name_col) or "").strip() if name_col else ""
                if not name:
                    name = f"ligand_{i}"
                entries.append(SmilesEntry(smiles=smi, name=name))
    else:
        # Treat .smi, .txt, or any other extension as whitespace-separated
        with open(path) as fh:
            for i, line in enumerate(fh, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                smi = parts[0]
                name = parts[1] if len(parts) > 1 else f"ligand_{i}"
                entries.append(SmilesEntry(smiles=smi, name=name))

    if not entries:
        raise ValueError(f"No SMILES entries found in {path}.")
    return entries


# ── Full dock-ligand preparation ────────────────────────────────────


def generate_pose_ensemble(
    base_conformer: Chem.Mol,
    n_poses: int,
    seed: int,
) -> list[Chem.Mol]:
    """Generate *n_poses* diverse 3-D pose candidates from one conformer.

    Diversity comes from two cheap sources: a fresh ETKDG conformer every
    other pose (torsional diversity) and a random rigid-body rotation about
    the molecule centroid (orientation diversity).  The diffusion
    refinement then searches the pocket from each candidate instead of
    polishing a single arbitrary orientation.
    """
    smiles = Chem.MolToSmiles(base_conformer)
    rng = random.Random(int(seed) & 0x7FFFFFFF)

    def _random_rotation() -> np.ndarray:
        angles = np.array([rng.uniform(0, 2 * np.pi) for _ in range(3)])
        c, s = np.cos(angles), np.sin(angles)
        rz = np.array([[c[0], -s[0], 0], [s[0], c[0], 0], [0, 0, 1.0]])
        ry = np.array([[c[1], 0, s[1]], [0, 1.0, 0], [-s[1], 0, c[1]]])
        rx = np.array([[1.0, 0, 0], [0, c[2], -s[2]], [0, s[2], c[2]]])
        return rx @ ry @ rz

    def _rotated_about_centroid(mol: Chem.Mol, rotation: np.ndarray) -> Chem.Mol:
        mol = Chem.Mol(mol)
        conf = mol.GetConformer()
        coords = np.array(
            [list(conf.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())],
            dtype=float,
        )
        centroid = coords.mean(axis=0)
        moved = (coords - centroid) @ rotation.T + centroid
        for i in range(mol.GetNumAtoms()):
            conf.SetAtomPosition(i, tuple(moved[i]))
        return mol

    poses: list[Chem.Mol] = []
    for k in range(n_poses):
        conformer = (
            generate_conformer_from_smiles(smiles, seed=int(seed) + k * 7919)
            if k % 2 == 0
            else None
        )
        if conformer is None:
            # Reuse the previous conformer: orientation diversity alone is
            # enough to explore the pocket.
            conformer = poses[-1] if poses else base_conformer
        poses.append(_rotated_about_centroid(conformer, _random_rotation()))
    return poses


def prepare_dock_ligands(
    smiles_entries: Sequence[SmilesEntry],
    pocket_center: PocketCenter,
    seed: int,
    work_dir: Path,
    n_poses: int = 1,
) -> Path:
    """Generate ETKDG conformers, place them at the pocket, write a combined SDF.

    Returns the path to the multi-molecule SDF.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    combined_sdf = work_dir / "dock_ligands.sdf"

    writer = Chem.SDWriter(str(combined_sdf))
    success_count = 0
    failed: list[str] = []
    pose_groups: dict[str, list[str]] = {}

    for idx, entry in enumerate(smiles_entries):
        # Vary the seed per ligand for diverse conformers.
        ligand_seed = (int(seed) + idx * 7919) & 0x7FFFFFFF
        mol = generate_conformer_from_smiles(entry.smiles, seed=ligand_seed)
        if mol is None:
            failed.append(f"{entry.name}\t{entry.smiles}\tconformer generation failed")
            print(f"  [Dock] skipped {entry.name}: both embedding attempts failed (SMILES {entry.smiles!r})")
            continue

        if n_poses <= 1:
            pose_mols = [mol]
            pose_names = [entry.name]
        else:
            pose_mols = generate_pose_ensemble(mol, n_poses, seed=ligand_seed)
            pose_names = [f"{entry.name}__pose{k:02d}" for k in range(len(pose_mols))]
        pose_groups[entry.name] = pose_names

        for pose_name, pose_mol in zip(pose_names, pose_mols):
            placed = place_conformer_at_center(pose_mol, pocket_center)
            placed.SetProp("_Name", pose_name)
            placed.SetProp("_DockSMILES", entry.smiles)
            placed.SetProp("_DockSeed", str(ligand_seed))
            writer.write(placed)
            success_count += 1
        print(
            f"  [Dock] {entry.name}: {len(pose_names)} pose(s) "
            f"x {pose_mols[0].GetNumAtoms()} atoms placed at {pocket_center}"
        )

    writer.close()

    if success_count == 0:
        raise RuntimeError(
            "All conformer generations failed. Check your SMILES strings.\n"
            + "\n".join(f"  {f}" for f in failed)
        )

    if failed:
        failed_path = work_dir / "failed_smiles.txt"
        failed_path.write_text("\n".join(failed) + "\n")
        print(f"  [Dock] {len(failed)} SMILES failed; see {failed_path}")

    # Machine-readable record of partial failures.
    prep_record = {
        "submitted": len(smiles_entries),
        "succeeded": success_count,
        "n_poses_per_ligand": int(n_poses),
        "pose_groups": pose_groups,
        "failed": [
            {"name": parts[0], "smiles": parts[1], "reason": parts[2]}
            for parts in (line.split("\t", 2) for line in failed)
        ],
    }
    (work_dir / "dock_preparation.json").write_text(
        json.dumps(prep_record, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n[Dock] Generated {success_count} pose(s) from "
          f"{len(smiles_entries)} SMILES: {combined_sdf}")
    return combined_sdf
