import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

import numpy as np
import torch
import gemmi
from dataclasses import asdict

from boltz.data import const
from boltz.data.feature.featurizerv2 import Boltz2Featurizer
from boltz.data.mol import load_canonicals, load_molecules
from boltz.data.tokenize.boltz2 import Boltz2Tokenizer
from boltz.data.types import ChainInfo, InferenceOptions, Input, Record
from boltz.data.module.inferencev2 import collate
from boltz.main import (
    Boltz2DiffusionParams,
    BoltzSteeringParams,
    MSAModuleArgs,
    PairformerArgsV2,
    get_cache_path,
)
from boltz.model.models.boltz2 import Boltz2

if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from Boltz2Score.ipsae import calculate_ipsae
from Boltz2Score.prepare_boltz2score_inputs import prepare_structure_inputs


def _as_float(value: Any) -> float:
    if torch.is_tensor(value):
        return float(value.detach().cpu().item())
    return float(value)


def _move_to_device(batch: Dict[str, Any], device: torch.device) -> Dict[str, Any]:
    moved = {}
    for key, value in batch.items():
        if torch.is_tensor(value):
            moved[key] = value.to(device)
        else:
            moved[key] = value
    return moved


def _pair_chains_to_dict(pair_chains_iptm: Dict[int, Dict[int, torch.Tensor]]) -> Dict[str, Dict[str, float]]:
    output: Dict[str, Dict[str, float]] = {}
    for idx1, inner in pair_chains_iptm.items():
        inner_map: Dict[str, float] = {}
        for idx2, value in inner.items():
            if torch.is_tensor(value):
                inner_map[str(idx2)] = float(value.detach().cpu().item())
            else:
                inner_map[str(idx2)] = float(value)
        output[str(idx1)] = inner_map
    return output


def _build_record(record_id: str, parsed) -> Record:
    chain_infos = []
    for chain in parsed.data.chains:
        chain_infos.append(
            ChainInfo(
                chain_id=int(chain["asym_id"]),
                chain_name=str(chain["name"]),
                mol_type=int(chain["mol_type"]),
                cluster_id=-1,
                msa_id=-1,
                num_residues=int(chain["res_num"]),
                valid=True,
                entity_id=int(chain["entity_id"]),
            )
        )

    return Record(
        id=record_id,
        structure=parsed.info,
        chains=chain_infos,
        interfaces=[],
        inference_options=InferenceOptions(pocket_constraints=None, contact_constraints=None),
        templates=None,
        affinity=None,
    )


def _compute_ipsae(
    pae: np.ndarray,
    tokens: np.ndarray,
    chains,
) -> Dict[str, float]:
    chain_ids = tokens["asym_id"].astype(int)
    chain_names = np.array([str(chains[idx]["name"]) for idx in chain_ids])
    residue_types = tokens["res_name"].astype(str)
    mol_types = np.array([int(chains[idx]["mol_type"]) for idx in chain_ids])
    polymer_mask = mol_types != const.chain_type_ids["NONPOLYMER"]

    if not polymer_mask.any():
        return {}

    filtered_pae = pae[np.ix_(polymer_mask, polymer_mask)]
    filtered_chain_ids = chain_names[polymer_mask]
    filtered_res_types = residue_types[polymer_mask]

    chain_name_to_type = {
        str(chain["name"]): int(chain["mol_type"]) for chain in chains
    }
    chain_type_map: Dict[str, str] = {}
    for idx in np.unique(filtered_chain_ids):
        mol_type = chain_name_to_type.get(str(idx))
        if mol_type in (const.chain_type_ids["DNA"], const.chain_type_ids["RNA"]):
            chain_type_map[str(idx)] = "nucleic_acid"
        else:
            chain_type_map[str(idx)] = "protein"

    return calculate_ipsae(
        filtered_pae,
        filtered_chain_ids,
        residue_types=filtered_res_types,
        chain_type_map=chain_type_map,
        pae_cutoff=10.0,
    )


def _infer_target_and_ligand_chains(chains) -> tuple[str | None, str | None]:
    target_chains = []
    ligand_chains = []
    for chain in chains:
        try:
            chain_name = str(chain["name"]).strip()
        except Exception:
            chain_name = str(getattr(chain, "name", "")).strip()
        if not chain_name:
            continue
        try:
            mol_type = int(chain["mol_type"])
        except Exception:
            mol_type = int(getattr(chain, "mol_type", const.chain_type_ids["NONPOLYMER"]))
        if mol_type == const.chain_type_ids["NONPOLYMER"]:
            ligand_chains.append(chain_name)
        else:
            target_chains.append(chain_name)
    target = ",".join(sorted(set(target_chains))) if target_chains else None
    ligand = ",".join(sorted(set(ligand_chains))) if ligand_chains else None
    return target, ligand


def run_boltz2score(
    input_path: Path,
    output_dir: Path,
    work_dir: Path,
    accelerator: str,
    devices: int,
    target_chain: str | None,
    ligand_chain: str | None,
    affinity_refine: bool,
    enable_affinity: bool,
    auto_enable_affinity: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    parsed, extra_mols, structure = prepare_structure_inputs(input_path)
    record_id = input_path.stem
    record = _build_record(record_id, parsed)

    inferred_target, inferred_ligand = _infer_target_and_ligand_chains(parsed.data.chains)
    if not target_chain:
        target_chain = inferred_target
    if not ligand_chain:
        ligand_chain = inferred_ligand

    tokenizer = Boltz2Tokenizer()
    featurizer = Boltz2Featurizer()

    input_data = Input(
        structure=parsed.data,
        msa={},
        record=record,
        residue_constraints=None,
        templates=None,
        extra_mols=extra_mols,
    )
    tokenized = tokenizer.tokenize(input_data)

    cache_dir = Path(get_cache_path())
    mol_dir = cache_dir / "mols"
    molecules = load_canonicals(mol_dir)
    molecules.update(extra_mols)
    missing = set(tokenized.tokens["res_name"].tolist()) - set(molecules.keys())
    if missing:
        molecules.update(load_molecules(mol_dir, missing))

    rng = np.random.default_rng(42)
    features = featurizer.process(
        tokenized,
        molecules=molecules,
        random=rng,
        training=False,
        max_atoms=None,
        max_tokens=None,
        max_seqs=const.max_msa_seqs,
        pad_to_max_seqs=False,
        single_sequence_prop=0.0,
        compute_frames=True,
        compute_constraint_features=True,
        override_method=None,
        compute_affinity=False,
    )
    features["record"] = record

    batch = collate([features])

    device = torch.device("cpu" if accelerator == "cpu" else "cuda")
    batch = _move_to_device(batch, device)

    cache_dir = Path(get_cache_path())
    ckpt_path = cache_dir / "boltz2_conf.ckpt"
    if not ckpt_path.exists():
        raise FileNotFoundError(f"Boltz2 checkpoint not found: {ckpt_path}")

    predict_args = {
        "recycling_steps": 0,
        "sampling_steps": 1,
        "diffusion_samples": 1,
        "max_parallel_samples": 1,
        "write_confidence_summary": True,
        "write_full_pae": True,
        "write_full_pde": True,
    }
    diffusion_params = asdict(Boltz2DiffusionParams())
    pairformer_args = asdict(PairformerArgsV2())
    msa_args = asdict(
        MSAModuleArgs(
            subsample_msa=False,
            num_subsampled_msa=1024,
            use_paired_feature=True,
        )
    )
    steering_args = asdict(BoltzSteeringParams())

    model = Boltz2.load_from_checkpoint(
        ckpt_path,
        strict=True,
        map_location="cpu",
        predict_args=predict_args,
        diffusion_process_args=diffusion_params,
        ema=False,
        pairformer_args=pairformer_args,
        msa_args=msa_args,
        steering_args=steering_args,
        skip_run_structure=True,
        run_trunk_and_structure=True,
        confidence_prediction=True,
    )
    model.eval()
    model.to(device)

    recycling_steps = int(predict_args.get("recycling_steps", 0))
    sampling_steps = int(predict_args.get("sampling_steps", 1)) or 1

    with torch.no_grad():
        out = model(
            batch,
            recycling_steps=recycling_steps,
            num_sampling_steps=sampling_steps,
            diffusion_samples=1,
            max_parallel_samples=1,
            run_confidence_sequentially=True,
        )

    pae = out["pae"][0].detach().cpu().numpy()
    ipsae = _compute_ipsae(pae, tokenized.tokens, parsed.data.chains)

    pair_chains_iptm = _pair_chains_to_dict(out["pair_chains_iptm"])
    chains_ptm = {k: v.get(k, 0.0) for k, v in pair_chains_iptm.items()}

    raw_complex_plddt = _as_float(out["complex_plddt"])
    raw_complex_iplddt = _as_float(out["complex_iplddt"])
    scaled_complex_plddt = raw_complex_plddt * 100.0 if raw_complex_plddt <= 1.0 else raw_complex_plddt
    scaled_complex_iplddt = raw_complex_iplddt * 100.0 if raw_complex_iplddt <= 1.0 else raw_complex_iplddt

    token_chain_ids = tokenized.tokens["asym_id"].astype(int)
    token_chain_names = np.array([str(parsed.data.chains[idx]["name"]) for idx in token_chain_ids])
    token_mol_types = tokenized.tokens["mol_type"].astype(int)

    plddt_raw = out["plddt"][0].detach().cpu().numpy()
    plddt_scaled = plddt_raw * 100.0 if float(np.max(plddt_raw)) <= 1.0 else plddt_raw

    plddt_by_chain: Dict[str, float] = {}
    for chain_name in np.unique(token_chain_names):
        mask = token_chain_names == chain_name
        if mask.any():
            plddt_by_chain[str(chain_name)] = float(plddt_scaled[mask].mean())

    plddt_by_mol_type: Dict[str, float] = {}
    for mol_name, mol_id in const.chain_type_ids.items():
        mask = token_mol_types == mol_id
        if mask.any():
            plddt_by_mol_type[mol_name.lower()] = float(plddt_scaled[mask].mean())

    protein_plddt = plddt_by_mol_type.get("protein")
    ligand_plddt = plddt_by_mol_type.get("nonpolymer")

    plddt_by_residue = []
    plddt_by_ligand_atom = []
    token_res_idx = tokenized.tokens["res_idx"].astype(int)
    token_res_name = tokenized.tokens["res_name"].astype(str)
    token_atom_idx = tokenized.tokens["atom_idx"].astype(int)

    residue_key_to_vals: Dict[tuple[str, int, str], list[float]] = {}
    for idx in range(len(plddt_scaled)):
        chain_name = str(token_chain_names[idx])
        res_idx = int(token_res_idx[idx])
        res_name = str(token_res_name[idx])
        mol_type = int(token_mol_types[idx])
        if mol_type == const.chain_type_ids["NONPOLYMER"]:
            atom_name = None
            ligand_mol = molecules.get(res_name)
            if ligand_mol is not None and token_atom_idx[idx] < ligand_mol.GetNumAtoms():
                atom = ligand_mol.GetAtomWithIdx(int(token_atom_idx[idx]))
                if atom.HasProp("name"):
                    atom_name = atom.GetProp("name")
                else:
                    atom_name = f"{atom.GetSymbol()}{atom.GetIdx()}"
            if not atom_name:
                atom_name = f"ATOM{int(token_atom_idx[idx])}"
            plddt_by_ligand_atom.append(
                {
                    "chain": chain_name,
                    "res_idx": res_idx,
                    "res_name": res_name,
                    "atom_idx": int(token_atom_idx[idx]),
                    "atom_name": atom_name,
                    "plddt": float(plddt_scaled[idx]),
                }
            )
        else:
            key = (chain_name, res_idx, res_name)
            residue_key_to_vals.setdefault(key, []).append(float(plddt_scaled[idx]))

    for (chain_name, res_idx, res_name), vals in residue_key_to_vals.items():
        plddt_by_residue.append(
            {
                "chain": chain_name,
                "res_idx": int(res_idx),
                "res_name": res_name,
                "plddt": float(np.mean(vals)),
                "count": int(len(vals)),
            }
        )

    confidence_summary = {
        "confidence_score": _as_float(
            (4 * out["complex_plddt"] + torch.where(out["iptm"] > 0, out["iptm"], out["ptm"])) / 5
        ),
        "ptm": _as_float(out["ptm"]),
        "iptm": _as_float(out["iptm"]),
        "ligand_iptm": _as_float(out["ligand_iptm"]),
        "protein_iptm": _as_float(out["protein_iptm"]),
        "complex_plddt": scaled_complex_plddt,
        "complex_plddt_raw": raw_complex_plddt,
        "complex_plddt_protein": protein_plddt,
        "complex_plddt_ligand": ligand_plddt,
        "complex_iplddt": scaled_complex_iplddt,
        "complex_iplddt_raw": raw_complex_iplddt,
        "complex_pde": _as_float(out["complex_pde"]),
        "complex_ipde": _as_float(out["complex_ipde"]),
        "plddt_by_chain": plddt_by_chain,
        "plddt_by_mol_type": plddt_by_mol_type,
        "plddt_by_residue": plddt_by_residue,
        "plddt_by_ligand_atom": plddt_by_ligand_atom,
        "chains_ptm": chains_ptm,
        "pair_chains_iptm": pair_chains_iptm,
        "ipsae": ipsae,
    }
    for key, value in ipsae.items():
        confidence_summary[f"ipsae_{key}"] = value

    record_dir = output_dir / record_id
    record_dir.mkdir(parents=True, exist_ok=True)

    structure_output = record_dir / f"{record_id}_model_0.cif"
    if input_path.suffix.lower() == ".cif":
        shutil.copyfile(input_path, structure_output)
    else:
        struct = gemmi.read_structure(str(input_path))
        struct.setup_entities()
        doc = struct.make_mmcif_document()
        doc.write_file(str(structure_output))

    confidence_path = record_dir / f"confidence_{record_id}_model_0.json"
    confidence_path.write_text(json.dumps(confidence_summary, indent=2))


    plddt_path = record_dir / f"plddt_{record_id}_model_0.npz"
    np.savez_compressed(plddt_path, plddt=plddt_scaled)

    plddt_raw_path = record_dir / f"plddt_raw_{record_id}_model_0.npz"
    np.savez_compressed(plddt_raw_path, plddt=plddt_raw)

    pae_path = record_dir / f"pae_{record_id}_model_0.npz"
    np.savez_compressed(pae_path, pae=pae)

    chain_map = {
        str(chain["asym_id"]): str(chain["name"]) for chain in parsed.data.chains
    }
    (output_dir / "chain_map.json").write_text(json.dumps(chain_map, indent=2))

    should_run_affinity = False
    if enable_affinity:
        should_run_affinity = True
    elif auto_enable_affinity:
        should_run_affinity = bool(ligand_chain or inferred_ligand)
    else:
        should_run_affinity = bool(target_chain and ligand_chain)

    if should_run_affinity:
        try:
            from affinity.main import Boltzina

            ligand_resname = None
            ligand_chain_ids = None
            if ligand_chain:
                ligand_chain_ids = {c.strip() for c in ligand_chain.split(",") if c.strip()}
            for model in structure:
                for chain in model:
                    if ligand_chain_ids is not None and chain.name not in ligand_chain_ids:
                        continue
                    for residue in chain:
                        ligand_resname = residue.name.strip()
                        if ligand_resname:
                            break
                    if ligand_resname:
                        break
                if ligand_resname:
                    break

            affinity_output_dir = work_dir / "boltz2score_affinity"
            affinity_work_dir = work_dir / "boltz2score_affinity_work"
            boltzina = Boltzina(
                output_dir=str(affinity_output_dir),
                work_dir=str(affinity_work_dir),
                skip_run_structure=not affinity_refine,
                ligand_resname=ligand_resname or "LIG",
            )
            boltzina.predict([str(input_path)])
            if boltzina.results:
                affinity_data = dict(boltzina.results[0])
                affinity_data["source"] = "boltz2score"
                affinity_path = record_dir / f"affinity_{record_id}.json"
                affinity_path.write_text(json.dumps(affinity_data, indent=2))
            else:
                predictions_dir = affinity_work_dir / "boltz_out" / "predictions"
                affinity_files = sorted(predictions_dir.glob("*/affinity_*.json"))
                if affinity_files:
                    affinity_data = json.loads(affinity_files[0].read_text())
                    affinity_data["source"] = "boltz2score_fallback"
                    affinity_data["raw_path"] = str(affinity_files[0])
                    affinity_path = record_dir / f"affinity_{record_id}.json"
                    affinity_path.write_text(json.dumps(affinity_data, indent=2))
                else:
                    raise RuntimeError(
                        "Affinity prediction completed without producing output files. "
                        "Check ligand detection and Boltzina logs."
                    )
        except Exception as exc:
            raise RuntimeError(f"Affinity prediction failed: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Boltz2Score: confidence-only scoring")
    parser.add_argument("--input", required=True, help="Input PDB/CIF structure")
    parser.add_argument("--output_dir", required=True, help="Output directory")
    parser.add_argument("--work_dir", required=True, help="Working directory")
    parser.add_argument("--accelerator", default="gpu")
    parser.add_argument("--devices", default="1")
    parser.add_argument("--num_workers", default="0")
    parser.add_argument("--target_chain")
    parser.add_argument("--ligand_chain")
    parser.add_argument("--affinity_refine", action="store_true")
    parser.add_argument("--enable_affinity", action="store_true")
    parser.add_argument("--auto_enable_affinity", action="store_true")

    args = parser.parse_args()

    run_boltz2score(
        input_path=Path(args.input).resolve(),
        output_dir=Path(args.output_dir).resolve(),
        work_dir=Path(args.work_dir).resolve(),
        accelerator=args.accelerator,
        devices=int(args.devices),
        target_chain=args.target_chain,
        ligand_chain=args.ligand_chain,
        affinity_refine=args.affinity_refine,
        enable_affinity=args.enable_affinity,
        auto_enable_affinity=args.auto_enable_affinity,
    )


if __name__ == "__main__":
    main()
