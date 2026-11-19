#!/usr/bin/env python3
"""ESMFold2 prediction capability.

Runs inside the vbio-esmfold2-runtime container. Parses the platform
prediction yaml (version 1, boltz-style sequences + constraints), folds it
with ESMFold2, and writes results in the mol1/protenix output contract so
the platform's downstream (result archive metrics, PAE heatmap, ipSAE,
MSA depth) consumes it unchanged:

  <output>/input/seed_<seed>/predictions/
    input_sample_<i>.cif                      all-atom model
    input_full_data_sample_<i>.json           token_pair_pae, token_asym_id,
                                              token_chain_ids, plddt
    input_summary_confidence_sample_<i>.json  iptm, ptm, ranking_score, plddt
  <output>/summary.json                        engine, best, n_summaries

Boundary notes (see configs/engine_capabilities.json):
  - L-peptides only; D positions would need D-form CCD modifications.
  - constraints.bond is rejected: cross-chain bond constraints are a
    protenix2dock capability in this integration.
  - No MSA input (single-sequence mode; the full model still benefits from
    its language-model priors).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yaml", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--seed", type=int, default=101)
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--num-loops", type=int, default=int(os.environ.get("ESMFOLD2_NUM_LOOPS", "20")))
    ap.add_argument("--num-sampling-steps", type=int,
                    default=int(os.environ.get("ESMFOLD2_NUM_SAMPLING_STEPS", "200")))
    ap.add_argument("--weights", default=os.environ.get(
        "ESMFOLD2_WEIGHTS", "biohub/ESMFold2"))
    return ap.parse_args()


def parse_platform_yaml(path: str) -> dict:
    data = yaml.safe_load(open(path)) or {}
    sequences = data.get("sequences") or []
    if not sequences:
        raise ValueError("yaml has no sequences")
    inputs = []
    for item in sequences:
        for kind in ("protein", "rna", "dna", "ligand"):
            entity = item.get(kind)
            if isinstance(entity, dict):
                inputs.append((kind, entity))
                break
    if not inputs:
        raise ValueError("yaml sequences carry no supported entities")
    return {"inputs": inputs, "constraints": data.get("constraints") or []}


def check_constraints(constraints: list) -> None:
    for c in constraints:
        if isinstance(c, dict) and "bond" in c:
            raise ValueError(
                "constraints.bond is not supported by the esmfold2 backend; "
                "use protenix2dock for bond-constrained designs")


def build_inputs(entries: list):
    from esm.models.esmfold2 import (
        DNAInput, ESMFold2InputBuilder, LigandInput, ProteinInput,
        RNAInput, StructurePredictionInput,
    )
    from string import ascii_uppercase

    sequences = []
    chain_names = []
    for index, (kind, entity) in enumerate(entries):
        chain = ascii_uppercase[index]
        chain_names.append(chain)
        ids = [chain]
        copies = entity.get("id")
        if isinstance(copies, list) and len(copies) > 1:
            ids = [f"{chain}{n + 1}" for n in range(len(copies))]
        if kind == "protein":
            sequences.append(ProteinInput(id=ids, sequence=entity["sequence"]))
        elif kind == "rna":
            sequences.append(RNAInput(id=ids, sequence=entity["sequence"]))
        elif kind == "dna":
            sequences.append(DNAInput(id=ids, sequence=entity["sequence"]))
        else:
            smiles = str(entity.get("smiles") or "").strip()
            ccd = entity.get("ccd")
            ccd_list = [c for c in (ccd if isinstance(ccd, list) else [ccd]) if c]
            if smiles:
                sequences.append(LigandInput(id=ids, smiles=smiles))
            elif ccd_list:
                sequences.append(LigandInput(id=ids, ccd=ccd_list))
            else:
                raise ValueError(f"ligand chain {chain} needs smiles or ccd")
    return StructurePredictionInput(sequences=sequences), chain_names


def main() -> None:
    args = parse_args()
    spec = parse_platform_yaml(args.yaml)
    check_constraints(spec["constraints"])

    output_dir = Path(args.output_dir)
    seed = args.seed

    import torch
    from esm.models.esmfold2 import ESMFold2InputBuilder, EsmFold2Model

    model = EsmFold2Model.from_pretrained(args.weights, device="cuda").eval()
    spi, chain_names = build_inputs(spec["inputs"])
    builder = ESMFold2InputBuilder()

    predictions = output_dir / "input" / f"seed_{seed}" / "predictions"
    predictions.mkdir(parents=True, exist_ok=True)

    started = time.time()
    results = builder.fold(
        model, spi,
        num_loops=args.num_loops,
        num_sampling_steps=args.num_sampling_steps,
        num_diffusion_samples=args.samples,
        seed=seed,
    )
    if not isinstance(results, list):
        results = [results]
    print(f"folded {len(results)} sample(s) in {time.time() - started:.0f}s",
          file=sys.stderr)

    summaries = []
    for index, result in enumerate(results):
        sample_cif = (result.complex.to_mmcif()
                      if hasattr(result.complex, "to_mmcif")
                      else result.complex.model_dump())
        chain_id = result.complex.chain_id
        chain_names_by_index = list(result.complex.chain_names)
        token_chain_ids = [chain_names_by_index[int(c)] for c in chain_id]
        pae = result.pae.detach().cpu().numpy() if result.pae is not None else None
        plddt = (result.plddt.detach().cpu().numpy()
                 if result.plddt is not None else None)
        iptm = float(result.iptm) if result.iptm is not None else 0.0
        ptm = float(result.ptm) if result.ptm is not None else 0.0
        mean_plddt = float(plddt.mean()) if plddt is not None else 0.0
        ranking_score = 0.8 * mean_plddt + 0.2 * iptm

        (predictions / f"input_sample_{index}.cif").write_text(
            sample_cif if isinstance(sample_cif, str) else str(sample_cif))

        full_data = {
            "token_pair_pae": pae.tolist() if pae is not None else [],
            "token_asym_id": [int(c) for c in chain_id],
            "token_chain_ids": token_chain_ids,
            "token_plddt": plddt.tolist() if plddt is not None else [],
            "atom_plddt": plddt.tolist() if plddt is not None else [],
            "ranking_score": ranking_score,
        }
        (predictions / f"input_full_data_sample_{index}.json").write_text(
            json.dumps(full_data))

        summary = {
            "iptm": iptm,
            "ptm": ptm,
            "plddt": mean_plddt * 100.0,
            "ranking_score": ranking_score,
            "chain_iptm": (
                [float(x) for x in result.pair_chains_iptm.diagonal()]
                if result.pair_chains_iptm is not None else []),
            "chain_pair_iptm": (
                result.pair_chains_iptm.tolist()
                if result.pair_chains_iptm is not None else []),
        }
        (predictions / f"input_summary_confidence_sample_{index}.json").write_text(
            json.dumps(summary))
        summaries.append({
            "sample": index,
            "confidence": {**summary, "iptm": iptm, "plddt": mean_plddt * 100.0},
        })

    best = max(summaries, key=lambda s: s["confidence"]["ranking_score"])
    (output_dir / "summary.json").write_text(json.dumps({
        "engine": "esmfold2",
        "input": args.yaml,
        "n_summaries": len(summaries),
        "skipped": 0,
        "best": best,
        "summaries": summaries,
    }, ensure_ascii=False))
    print(json.dumps({
        "ok": True,
        "n": len(summaries),
        "best_ranking": best["confidence"]["ranking_score"],
    }))


if __name__ == "__main__":
    main()
