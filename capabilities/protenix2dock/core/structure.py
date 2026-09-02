"""Structure parsing for protenix2dock.

Parses a user-uploaded protein structure (PDB/mmCIF), strips crystallographic
artifacts the same way Boltz2Score's dock pipeline does (polymer chains only;
waters/ions/buffers dropped — Protenix's input schema has no water entity),
normalizes non-standard residues to either a CCD modification or the closest
standard amino acid, and exposes per-residue atom coordinates for diffusion
initialisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import gemmi

# Common non-standard residues -> closest standard amino acid. Residues that
# exist as CCD entries are kept as modifications instead (CCD_ prefix in the
# input json) so their real geometry survives.
_TO_STANDARD: dict[str, str] = {
    "MSE": "M",  # selenomethionine
    "SEC": "U",  # selenocysteine; rare in docking targets
    "CSO": "C", "CSD": "C", "CME": "C", "OCS": "C",
    "HYP": "P",
    "MLY": "K",
    "FME": "M",
}
# Residues kept as CCD modifications (base letter must be a valid standard AA).
_CCD_MODIFICATIONS: dict[str, str] = {
    "PCA": "Q",  # pyroglutamate (from Gln)
    "CSX": "C",
    "SEP": "S", "TPO": "T",
}
# Capping groups and misc artifacts that are dropped entirely.
_DROP_RESIDUES = {"ACE", "NMA", "HOH", "DOD"}

THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


@dataclass
class ProteinChainData:
    """One parsed protein chain, ordered for the Protenix input json."""

    chain_name: str
    # One-letter sequence written to proteinChain.sequence.
    sequence: str
    # Per-sequence-position entries: {residue_name, ccd_or_none, atoms: {atom_name: (x,y,z)}}
    residues: list[dict] = field(default_factory=list)

    @property
    def modifications(self) -> list[dict]:
        return [
            {"ptmPosition": i + 1, "ptmType": f"CCD_{res['residue_name']}"}
            for i, res in enumerate(self.residues)
            if res["ccd"] is not None
        ]


def _residue_is_polymer(res: gemmi.Residue) -> bool:
    name = res.name.strip().upper()
    if name in _DROP_RESIDUES:
        return False
    return name in THREE_TO_ONE or name in _TO_STANDARD or name in _CCD_MODIFICATIONS


def parse_protein_chains(structure_path: Path, keep_chains: list[str] | None = None) -> list[ProteinChainData]:
    """Parse polymer chains from a structure file into Protenix-ready data.

    ``keep_chains`` optionally restricts output to the given auth chain ids
    (order preserved as given). All non-polymer artifacts are dropped.
    """
    structure = gemmi.read_structure(str(structure_path))
    structure.setup_entities()

    keep = {c.strip().upper() for c in (keep_chains or []) if c.strip()}
    chains: list[ProteinChainData] = []
    for chain in structure[0]:
        if keep and chain.name.strip().upper() not in keep:
            continue
        residues: list[dict] = []
        seq: list[str] = []
        for res in chain:
            name = res.name.strip().upper()
            if not _residue_is_polymer(res):
                continue
            if name in THREE_TO_ONE:
                letter, ccd = THREE_TO_ONE[name], None
            elif name in _TO_STANDARD:
                letter, ccd = _TO_STANDARD[name], None
            else:  # _CCD_MODIFICATIONS
                letter, ccd = _CCD_MODIFICATIONS[name], name
            atoms = {}
            for atom in res:
                atom_name = atom.name.strip()
                if atom.element == gemmi.Element("H") or atom_name.startswith("H"):
                    continue
                atoms[atom_name] = (atom.pos.x, atom.pos.y, atom.pos.z)
            if not atoms:
                continue
            seq.append(letter)
            residues.append({"residue_name": name, "ccd": ccd, "atoms": atoms})
        if residues:
            chains.append(
                ProteinChainData(
                    chain_name=chain.name.strip(),
                    sequence="".join(seq),
                    residues=residues,
                )
            )
    if not chains:
        raise ValueError(
            f"No polymer chains parsed from {structure_path}."
            + (f" Requested chains: {sorted(keep)}." if keep else "")
        )
    return chains
