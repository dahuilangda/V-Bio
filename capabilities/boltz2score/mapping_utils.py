from __future__ import annotations


def resolve_chain_smiles(
    map_data: dict[str, str],
    chain_name: str,
    resname: str,
) -> str | None:
    if not map_data:
        return None
    residue = str(resname or "").strip()
    for candidate in (str(chain_name or "").strip() + f":{residue}", str(chain_name or "").strip()):
        if candidate in map_data:
            return map_data[candidate]
    return None


def resolve_model_ligand_chain_id_by_atom_names(
    by_chain: dict[str, dict[str, float]],
    heavy_name_keys: list[str],
    requested_ligand_chain_id: str | None,
) -> str:
    required_names = set(heavy_name_keys)
    if not required_names:
        raise RuntimeError("Reference ligand atom-name keys are empty.")

    requested = str(requested_ligand_chain_id or "").strip()
    if requested:
        if requested in by_chain:
            requested_names = set(by_chain[requested].keys())
            if requested_names != required_names:
                raise RuntimeError(
                    "Requested ligand chain does not match the reference ligand atom-name set. "
                    f"Requested={requested!r}, "
                    f"missing={sorted(required_names - requested_names)[:10]}, "
                    f"extra={sorted(requested_names - required_names)[:10]}."
                )
            return requested

    matches = [
        chain_id
        for chain_id, atom_map in by_chain.items()
        if set(atom_map.keys()) == required_names
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        if requested:
            raise RuntimeError(
                f"Requested ligand chain {requested!r} not found in output structure, and no "
                "output ligand chain has an atom-name set identical to the reference ligand. "
                f"Available ligand chains: {sorted(by_chain.keys())}."
            )
        raise RuntimeError(
            "Unable to find an output ligand chain whose atom-name set matches the reference ligand."
        )
    raise RuntimeError(
        "Multiple output ligand chains match the reference ligand atom-name set. "
        f"Candidates: {matches}. Please provide an exact ligand chain id."
    )
