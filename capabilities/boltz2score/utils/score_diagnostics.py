from __future__ import annotations

import json
from pathlib import Path

from rdkit import Chem

from boltz.data import const
from boltz.data.types import StructureV2
from utils.ligand_alignment import (
    build_smiles_order_from_ligand_mol,
    extract_ligand_bfactors_by_chain,
    ligand_atom_plddt_stats,
    load_raw_ligand_plddt_entries,
    resolve_model_ligand_chain_id,
    resolve_model_ligand_chain_id_from_atom_names,
)


def _canonicalize_smiles_text(smiles: str) -> str:
    text = str(smiles or "").strip()
    if not text:
        return ""
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        return text
    return Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True)


def _collect_atom_coverage(processed_dir: Path, record_id: str) -> dict:
    """Collect per-chain atom presence diagnostics from processed inputs."""
    structure = StructureV2.load(processed_dir / "structures" / f"{record_id}.npz")
    structure = structure.remove_invalid_chains()

    chain_type_labels = {value: key.lower() for key, value in const.chain_type_ids.items()}
    ligand_type = const.chain_type_ids["NONPOLYMER"]

    chain_stats = []
    ligand_stats = []
    for chain in structure.chains:
        chain_name = str(chain["name"])
        mol_type = int(chain["mol_type"])
        atom_start = int(chain["atom_idx"])
        atom_end = atom_start + int(chain["atom_num"])
        atoms = structure.atoms[atom_start:atom_end]

        total_atoms = int(len(atoms))
        present_atoms = int(atoms["is_present"].sum()) if total_atoms else 0
        present_fraction = (present_atoms / total_atoms) if total_atoms else 0.0

        res_start = int(chain["res_idx"])
        res_end = res_start + int(chain["res_num"])
        residues = structure.residues[res_start:res_end]
        residue_names = sorted({str(res["name"]) for res in residues})

        item = {
            "chain": chain_name,
            "mol_type": chain_type_labels.get(mol_type, str(mol_type)),
            "total_atoms": total_atoms,
            "present_atoms": present_atoms,
            "present_fraction": present_fraction,
            "residue_names": residue_names,
        }
        chain_stats.append(item)
        if mol_type == ligand_type:
            ligand_stats.append(item)

    return {
        "record_id": record_id,
        "chain_atom_coverage": chain_stats,
        "ligand_atom_coverage": ligand_stats,
    }


def write_atom_coverage_diagnostics(
    processed_dir: Path,
    output_dir: Path,
    record_id: str,
    requested_ligand_chain_id: str | None = None,
    ligand_smiles_map: dict[str, str] | None = None,
    reference_ligand_mol: Chem.Mol | None = None,
) -> dict[str, object] | None:
    """Write atom coverage diagnostics and attach summary to confidence JSON."""
    try:
        coverage = _collect_atom_coverage(processed_dir, record_id)
        struct_dir = output_dir / record_id
        struct_dir.mkdir(parents=True, exist_ok=True)

        diag_path = struct_dir / f"ligand_atom_coverage_{record_id}.json"
        diag_path.write_text(json.dumps(coverage, indent=2))

        detected_ligand_chains = sorted(
            {
                str(item.get("chain") or "").strip()
                for item in coverage.get("ligand_atom_coverage", [])
                if isinstance(item, dict) and str(item.get("chain") or "").strip()
            }
        )

        aligned_ligand_smiles = ""
        aligned_ligand_plddts_input_order: list[float] = []
        aligned_ligand_plddts_smiles_order: list[float] = []
        aligned_ligand_chain_id = ""
        aligned_ligand_atom_name_keys_input_order: list[str] = []
        aligned_ligand_atom_name_keys_smiles_order: list[str] = []
        aligned_ligand_atom_plddts_by_name: dict[str, float] = {}
        smiles_to_heavy: list[int] = []
        if reference_ligand_mol is not None:
            ligand_chains = [
                str(item.get("chain") or "").strip()
                for item in coverage.get("ligand_atom_coverage", [])
                if isinstance(item, dict) and str(item.get("chain") or "").strip()
            ]
            aligned_ligand_smiles, smiles_to_heavy, heavy_name_keys = build_smiles_order_from_ligand_mol(
                reference_ligand_mol
            )
            try:
                aligned_ligand_chain_id = resolve_model_ligand_chain_id(
                    ligand_chains,
                    requested_ligand_chain_id,
                )
            except RuntimeError:
                aligned_ligand_chain_id = ""

        input_smiles_map: dict[str, str] = {}
        if isinstance(ligand_smiles_map, dict) and ligand_smiles_map:
            for key, value in ligand_smiles_map.items():
                key_norm = str(key or "").strip()
                value_norm = _canonicalize_smiles_text(str(value or "").strip())
                if key_norm and value_norm:
                    input_smiles_map[key_norm] = value_norm

        requested_chain = ""
        if requested_ligand_chain_id:
            requested_chain = requested_ligand_chain_id.strip()

        model_ligand_chain_id = aligned_ligand_chain_id
        if not model_ligand_chain_id:
            if requested_chain and requested_chain in detected_ligand_chains:
                model_ligand_chain_id = requested_chain
            elif len(detected_ligand_chains) == 1:
                model_ligand_chain_id = detected_ligand_chains[0]

        if aligned_ligand_smiles:
            aligned_ligand_smiles = _canonicalize_smiles_text(aligned_ligand_smiles)
        output_smiles_map: dict[str, str] = {}
        if model_ligand_chain_id:
            resolved_smiles = (
                aligned_ligand_smiles
                or input_smiles_map.get(model_ligand_chain_id)
                or input_smiles_map.get(requested_chain)
                or ""
            )
            if resolved_smiles:
                output_smiles_map[model_ligand_chain_id] = resolved_smiles
        elif len(input_smiles_map) == 1:
            output_smiles_map = dict(input_smiles_map)

        selected_ligand_smiles = ""
        if aligned_ligand_smiles:
            selected_ligand_smiles = aligned_ligand_smiles
        elif model_ligand_chain_id and output_smiles_map:
            selected_ligand_smiles = (
                output_smiles_map.get(model_ligand_chain_id)
                or ""
            )
        elif requested_chain and input_smiles_map:
            selected_ligand_smiles = (
                input_smiles_map.get(requested_chain)
                or input_smiles_map.get(requested_chain.upper())
                or input_smiles_map.get(requested_chain.lower())
                or ""
            )
        if not selected_ligand_smiles and len(output_smiles_map) == 1:
            selected_ligand_smiles = next(iter(output_smiles_map.values()))

        for conf_path in sorted(struct_dir.glob(f"confidence_{record_id}_model_*.json")):
            try:
                data = json.loads(conf_path.read_text())
            except Exception:
                continue

            if reference_ligand_mol is not None:
                conf_base = conf_path.stem
                struct_stem = conf_base[len("confidence_") :] if conf_base.startswith("confidence_") else conf_base
                structure_file: Path | None = None
                for ext in (".cif", ".mmcif", ".pdb"):
                    candidate = struct_dir / f"{struct_stem}{ext}"
                    if candidate.exists():
                        structure_file = candidate
                        break
                if structure_file is None:
                    raise RuntimeError(
                        f"Cannot find structure file for confidence: {conf_path.name}"
                    )
                by_chain = extract_ligand_bfactors_by_chain(structure_file)
                resolved_chain_id = aligned_ligand_chain_id
                if not resolved_chain_id:
                    resolved_chain_id = resolve_model_ligand_chain_id_from_atom_names(
                        by_chain,
                        heavy_name_keys,
                        requested_ligand_chain_id,
                    )
                    aligned_ligand_chain_id = resolved_chain_id
                    model_ligand_chain_id = resolved_chain_id
                if resolved_chain_id not in by_chain:
                    raise RuntimeError(
                        "Model ligand chain not found in structure for confidence alignment: "
                        f"{resolved_chain_id}. Available: {sorted(by_chain.keys())}."
                    )
                bfactor_by_name = by_chain[resolved_chain_id]
                heavy_bfactors: list[float] = []
                missing_name_keys: list[str] = []
                for key in heavy_name_keys:
                    if key not in bfactor_by_name:
                        missing_name_keys.append(key)
                        continue
                    heavy_bfactors.append(float(bfactor_by_name[key]))
                if missing_name_keys:
                    raise RuntimeError(
                        "Ligand atom-name alignment missing entries in model output: "
                        f"{missing_name_keys[:10]}"
                    )
                if len(heavy_bfactors) != len(heavy_name_keys):
                    raise RuntimeError(
                        "Ligand heavy-atom confidence alignment length mismatch: "
                        f"{len(heavy_bfactors)} vs {len(heavy_name_keys)}."
                    )
                aligned_ligand_plddts_input_order = [float(value) for value in heavy_bfactors]
                aligned_ligand_atom_name_keys_input_order = list(heavy_name_keys)
                aligned_ligand_plddts_smiles_order = [
                    float(heavy_bfactors[idx]) for idx in smiles_to_heavy
                ]
                aligned_ligand_atom_name_keys_smiles_order = [
                    heavy_name_keys[idx] for idx in smiles_to_heavy
                ]
                aligned_ligand_atom_plddts_by_name = {
                    aligned_ligand_atom_name_keys_input_order[idx]: float(
                        aligned_ligand_plddts_input_order[idx]
                    )
                    for idx in range(len(aligned_ligand_atom_name_keys_input_order))
                }
                input_to_smiles = [0] * len(smiles_to_heavy)
                for smiles_idx, input_idx in enumerate(smiles_to_heavy):
                    input_to_smiles[input_idx] = smiles_idx

                raw_entries_by_chain = load_raw_ligand_plddt_entries(
                    struct_dir / f"raw_ligand_atom_plddts_{struct_stem}.json"
                )
                model_order_entries = raw_entries_by_chain.get(resolved_chain_id, [])

                data["model_ligand_chain_id"] = resolved_chain_id
                data["ligand_atom_plddts_by_chain"] = {
                    resolved_chain_id: aligned_ligand_plddts_input_order
                }
                if aligned_ligand_atom_name_keys_input_order:
                    data["ligand_atom_names"] = aligned_ligand_atom_name_keys_input_order
                    data["ligand_atom_name_keys"] = aligned_ligand_atom_name_keys_input_order
                    data["ligand_atom_name_keys_by_chain"] = {
                        resolved_chain_id: aligned_ligand_atom_name_keys_input_order
                    }
                    data["ligand_atom_names_by_chain"] = {
                        resolved_chain_id: aligned_ligand_atom_name_keys_input_order
                    }
                if aligned_ligand_atom_plddts_by_name:
                    data["ligand_atom_plddts_by_chain_and_name"] = {
                        resolved_chain_id: aligned_ligand_atom_plddts_by_name
                    }
                data["ligand_atom_plddts"] = aligned_ligand_plddts_input_order
                data["ligand_atom_input_order_names"] = aligned_ligand_atom_name_keys_input_order
                data["ligand_atom_input_order_plddts"] = aligned_ligand_plddts_input_order
                data["ligand_atom_smiles_order_names"] = aligned_ligand_atom_name_keys_smiles_order
                data["ligand_atom_smiles_order_plddts"] = aligned_ligand_plddts_smiles_order
                data["ligand_atom_smiles_to_input_index"] = smiles_to_heavy
                data["ligand_atom_input_to_smiles_index"] = input_to_smiles
                if model_order_entries:
                    data["ligand_atom_model_order_names"] = [
                        str(item["atom_name"]) for item in model_order_entries
                    ]
                    data["ligand_atom_model_order_plddts"] = [
                        float(item["plddt"]) for item in model_order_entries
                    ]
                    data["ligand_atom_model_order_writer_token_indices"] = [
                        int(item["writer_token_index"]) for item in model_order_entries
                    ]
                data.update(ligand_atom_plddt_stats(aligned_ligand_plddts_input_order))
                data["ligand_smiles"] = aligned_ligand_smiles

            data["ligand_atom_coverage"] = coverage["ligand_atom_coverage"]
            data["chain_atom_coverage"] = coverage["chain_atom_coverage"]
            if model_ligand_chain_id:
                data["model_ligand_chain_id"] = model_ligand_chain_id
                data["ligand_chain"] = model_ligand_chain_id
            if output_smiles_map:
                data["ligand_smiles_map"] = output_smiles_map
            if requested_chain:
                data["requested_ligand_chain_id"] = requested_chain
                data["requested_ligand_chain"] = requested_chain
            if selected_ligand_smiles and reference_ligand_mol is None:
                data["ligand_smiles"] = selected_ligand_smiles
            conf_path.write_text(json.dumps(data, indent=2))

        alignment_payload: dict[str, object] = {}
        if selected_ligand_smiles:
            selected_ligand_smiles = _canonicalize_smiles_text(selected_ligand_smiles)
            alignment_payload["ligand_smiles"] = selected_ligand_smiles
        if model_ligand_chain_id:
            alignment_payload["model_ligand_chain_id"] = model_ligand_chain_id
            alignment_payload["ligand_chain"] = model_ligand_chain_id
        if requested_chain:
            alignment_payload["requested_ligand_chain_id"] = requested_chain
            alignment_payload["requested_ligand_chain"] = requested_chain
        if output_smiles_map:
            alignment_payload["ligand_smiles_map"] = output_smiles_map
        if aligned_ligand_atom_name_keys_input_order:
            alignment_payload["ligand_atom_names"] = aligned_ligand_atom_name_keys_input_order
            alignment_payload["ligand_atom_name_keys"] = aligned_ligand_atom_name_keys_input_order
            alignment_payload["ligand_atom_input_order_names"] = aligned_ligand_atom_name_keys_input_order
            alignment_payload["ligand_atom_input_order_plddts"] = aligned_ligand_plddts_input_order
            alignment_payload["ligand_atom_smiles_order_names"] = aligned_ligand_atom_name_keys_smiles_order
            alignment_payload["ligand_atom_smiles_order_plddts"] = aligned_ligand_plddts_smiles_order
            alignment_payload["ligand_atom_smiles_to_input_index"] = smiles_to_heavy
            alignment_payload.update(ligand_atom_plddt_stats(aligned_ligand_plddts_input_order))
            if model_ligand_chain_id:
                alignment_payload["ligand_atom_name_keys_by_chain"] = {
                    model_ligand_chain_id: aligned_ligand_atom_name_keys_input_order
                }
                alignment_payload["ligand_atom_names_by_chain"] = {
                    model_ligand_chain_id: aligned_ligand_atom_name_keys_input_order
                }
        if aligned_ligand_atom_plddts_by_name and model_ligand_chain_id:
            alignment_payload["ligand_atom_plddts_by_chain_and_name"] = {
                model_ligand_chain_id: aligned_ligand_atom_plddts_by_name
            }
        return alignment_payload or None
    except Exception as exc:  # noqa: BLE001
        if reference_ligand_mol is not None:
            raise RuntimeError(
                f"Failed to write strict ligand atom-confidence alignment: {exc}"
            ) from exc
        print(f"[Warning] Failed to write atom coverage diagnostics: {exc}")
        return None
