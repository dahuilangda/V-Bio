"""Pure RDKit helpers for the lead-optimization fragment/reference preview APIs."""
from pathlib import Path
from typing import Any, List, Optional


def reference_ligand_sdf(file_name: str, content: str) -> str:
    """Return the uploaded reference ligand as SDF text.

    Accepts the same formats as the reference preview (.sdf/.sd/.mol2/.mol/
    .pdb/.ent/.cif/.mmcif); SDF passes through untouched, anything else is
    parsed with RDKit (CIF via a gemmi round-trip) and re-serialized so the
    optimization engine — which consumes a single SDF — can read it.
    """
    from rdkit import Chem

    suffix = Path(str(file_name or "").strip()).suffix.lower()
    if suffix in ("", ".sdf", ".sd"):
        return content

    def _mol_to_sdf(mol) -> str:
        if mol is None or mol.GetNumAtoms() == 0:
            raise ValueError(f"Failed to parse reference ligand '{file_name}'.")
        return Chem.MolToMolBlock(mol, kekulize=False) + "$$$$\n"

    if suffix == ".mol2":
        try:
            mol = Chem.MolFromMol2Block(content, sanitize=False, removeHs=False, cleanupSubstructures=False)
        except TypeError:
            mol = Chem.MolFromMol2Block(content, sanitize=False, removeHs=False)
        return _mol_to_sdf(mol)
    if suffix == ".mol":
        return _mol_to_sdf(Chem.MolFromMolBlock(content, sanitize=False, removeHs=False))
    if suffix in (".pdb", ".ent"):
        try:
            mol = Chem.MolFromPDBBlock(content, removeHs=False, sanitize=False, proximityBonding=False)
        except TypeError:
            mol = Chem.MolFromPDBBlock(content, removeHs=False, sanitize=False)
        return _mol_to_sdf(mol)
    if suffix in (".cif", ".mmcif"):
        import gemmi

        structure = gemmi.read_structure_from_string(content)
        if len(structure) == 0:
            raise ValueError(f"Failed to parse reference ligand '{file_name}'.")
        return _mol_to_sdf(Chem.MolFromPDBBlock(structure.make_pdb_string(), removeHs=False, sanitize=False))
    raise ValueError(
        f"Unsupported reference ligand format '{suffix}' — use .sdf/.sd/.mol2/.mol/.pdb/.ent/.cif/.mmcif."
    )


def _expand_atom_indices_to_complete_rings(parent: Any, atom_indices: set) -> set:
    expanded = set(int(idx) for idx in atom_indices)
    if not expanded:
        return expanded
    try:
        ring_info = parent.GetRingInfo()
        rings = [set(int(i) for i in ring) for ring in ring_info.AtomRings()]
    except Exception:
        rings = []

    changed = True
    while changed:
        changed = False
        for ring in rings:
            if expanded.intersection(ring) and not ring.issubset(expanded):
                expanded.update(ring)
                changed = True

    # Keep explicit H touching selected variable atoms on the variable side.
    extra_h: set = set()
    for atom_idx in list(expanded):
        try:
            atom = parent.GetAtomWithIdx(atom_idx)
        except Exception:
            continue
        for neighbor in atom.GetNeighbors():
            if int(neighbor.GetAtomicNum()) == 1:
                extra_h.add(int(neighbor.GetIdx()))
    expanded.update(extra_h)
    return expanded


def _normalize_attachment_query(query: str) -> str:
    try:
        from rdkit import Chem
    except Exception:
        return str(query or "").strip()
    text = str(query or "").strip()
    if not text:
        return ""
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        mol = Chem.MolFromSmarts(text)
    if mol is None:
        return ""
    dummy_atoms = [atom for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0]
    if not dummy_atoms:
        return ""
    # Canonicalize dummy labels to [*:1], [*:2], ... for stable output.
    for idx, atom in enumerate(sorted(dummy_atoms, key=lambda item: int(item.GetIdx())), start=1):
        atom.SetAtomMapNum(idx)
        atom.SetIsotope(0)
    try:
        normalized = Chem.MolToSmiles(mol, canonical=True)
    except Exception:
        return ""
    if not normalized or "*" not in normalized:
        return ""
    return normalized


def derive_attachment_query_from_atom_indices(query_mol: Any, atom_indices: List[int], *, expand_rings: bool = True) -> str:
    try:
        from rdkit import Chem
    except Exception:
        return ""
    parent = None
    if query_mol is not None and hasattr(query_mol, "GetNumAtoms"):
        parent = query_mol
    else:
        query_text = str(query_mol or "").strip()
        parent = Chem.MolFromSmiles(query_text)
        if parent is None and query_text:
            # Keep atom order when possible for index-driven selections, even for partially non-kekulized inputs.
            parent = Chem.MolFromSmiles(query_text, sanitize=False)
            if parent is not None:
                try:
                    Chem.SanitizeMol(
                        parent,
                        sanitizeOps=Chem.SanitizeFlags.SANITIZE_ALL ^ Chem.SanitizeFlags.SANITIZE_KEKULIZE,
                    )
                except Exception:
                    pass
    if parent is None:
        return ""
    atom_set = {
        int(idx)
        for idx in atom_indices
        if isinstance(idx, int) and 0 <= int(idx) < parent.GetNumAtoms()
    }
    if expand_rings:
        atom_set = _expand_atom_indices_to_complete_rings(parent, atom_set)
    if not atom_set:
        return ""

    boundary_bond_indices: set = set()
    for atom_idx in atom_set:
        atom = parent.GetAtomWithIdx(atom_idx)
        for neighbor in atom.GetNeighbors():
            neighbor_idx = int(neighbor.GetIdx())
            if neighbor_idx in atom_set:
                continue
            bond = parent.GetBondBetweenAtoms(atom_idx, neighbor_idx)
            if bond is not None:
                boundary_bond_indices.add(int(bond.GetIdx()))
    if not boundary_bond_indices:
        return ""

    fragmented = Chem.FragmentOnBonds(parent, sorted(boundary_bond_indices), addDummies=True)
    fragments = Chem.GetMolFrags(fragmented, asMols=False, sanitizeFrags=False)
    selected_atoms: Optional[tuple] = None
    best_overlap = -1
    for frag_atoms in fragments:
        overlap = len(atom_set.intersection(int(a) for a in frag_atoms))
        if overlap > best_overlap:
            best_overlap = overlap
            selected_atoms = tuple(int(a) for a in frag_atoms)
    if not selected_atoms or best_overlap <= 0:
        return ""
    try:
        query = Chem.MolFragmentToSmiles(fragmented, atomsToUse=list(selected_atoms), canonical=True)
    except Exception:
        return ""
    normalized = _normalize_attachment_query(query)
    if not normalized or "*" not in normalized:
        return ""
    return normalized


def attachment_fragment_smiles_from_atom_indices(parent_mol: Any, atom_indices: List[int]) -> str:
    from rdkit import Chem

    atom_set = {int(idx) for idx in atom_indices if isinstance(idx, (int, float))}
    if not atom_set:
        return ''
    query = derive_attachment_query_from_atom_indices(parent_mol, sorted(atom_set), expand_rings=False)
    if not query or '*' not in query:
        return ''

    if Chem.MolFromSmiles(query) is not None:
        return query

    # Keep attachment-aware output only when we can normalize it to stable SMILES.
    query_mol = Chem.MolFromSmarts(query)
    if query_mol is None:
        return ''
    try:
        normalized = Chem.MolToSmiles(query_mol, canonical=True)
    except Exception:
        return ''
    if not normalized or '*' not in normalized:
        return ''
    if Chem.MolFromSmiles(normalized) is None:
        return ''
    return normalized


def decode_smiles_atom_index_from_name(atom_name: str) -> Optional[int]:
    token = str(atom_name or '').strip().upper()
    if len(token) != 4 or not token.startswith('Q'):
        return None
    alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    value = 0
    for ch in token[1:]:
        idx = alphabet.find(ch)
        if idx < 0:
            return None
        value = value * 36 + idx
    return value
