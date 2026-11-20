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
                                              token_plddt
    input_summary_confidence_sample_<i>.json  iptm, ptm, ranking_score, plddt
  <output>/summary.json                        engine, best, n_summaries

Boundary notes (see configs/engine_capabilities.json):
  - L-peptides only; D positions would need D-form CCD modifications.
  - constraints are rejected: contact/bond constraints are a
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



def esm_ccd_has(ccd: str) -> bool:
    try:
        from esm.models.esmfold2.conformers import load_ccd
        return ccd in load_ccd()
    except Exception:
        return True  # CCD table unavailable: let esm decide at tokenization

def parse_platform_yaml(path: str) -> dict:
    data = yaml.safe_load(open(path)) or {}
    sequences = data.get("sequences") or []
    if not sequences:
        raise ValueError("yaml has no sequences")
    inputs = []
    for item in sequences:
        for kind in ("protein", "rna", "dna", "ligand", "ion"):
            entity = item.get(kind)
            if isinstance(entity, dict):
                inputs.append((kind, entity))
                break
    if not inputs:
        raise ValueError("yaml sequences carry no supported entities")
    return {"inputs": inputs, "constraints": data.get("constraints") or []}


def check_constraints(constraints: list) -> None:
    supported = set()
    for c in constraints:
        if isinstance(c, dict):
            keys = {k for k in c if k not in supported}
            if keys:
                raise ValueError(
                    f"constraints {sorted(keys)} are not supported by the "
                    "esmfold2 backend")


def build_inputs(entries: list):
    from esm.models.esmfold2 import (
        DNAInput, ESMFold2InputBuilder, LigandInput, Modification,
        ProteinInput, RNAInput, StructurePredictionInput,
    )
    from string import ascii_uppercase

    sequences = []
    chain_names = []
    for index, (kind, entity) in enumerate(entries):
        # Use the yaml entity ids verbatim (one per copy) so output cif chain
        # names match mol1/protenix for the same yaml; generated letters are
        # the fallback when the yaml omits id.
        raw_ids = entity.get("id")
        if isinstance(raw_ids, list) and raw_ids:
            ids = [str(x) for x in raw_ids]
            chain = ids[0]
        elif raw_ids:
            ids = [str(raw_ids)]
            chain = ids[0]
        else:
            chain = (ascii_uppercase[index]
                     if index < 26
                     else ascii_uppercase[index // 26 - 1] + ascii_uppercase[index % 26])
            ids = [chain]
        chain_names.append(chain)
        if kind in ("protein", "rna", "dna"):
            mods = []
            seq_len = len(entity.get("sequence") or "")
            for m in (entity.get("modifications") or []):
                if not isinstance(m, dict) or not m.get("ccd"):
                    continue
                pos = int(m["position"])
                if not 1 <= pos <= seq_len:
                    raise ValueError(
                        f"modification position {pos} out of range 1..{seq_len} "
                        f"for chain {chain} (ccd {m['ccd']})")
                ccd = str(m["ccd"])
                # Unknown CCD would silently degrade to a backbone-only
                # residue — fail loudly instead.
                if not esm_ccd_has(ccd):
                    raise ValueError(
                        f"CCD component {ccd!r} not in the esm CCD table "
                        f"(chain {chain} position {pos})")
                mods.append(Modification(position=pos - 1, ccd=ccd))
            if kind == "protein":
                sequences.append(ProteinInput(
                    id=ids, sequence=entity["sequence"], modifications=mods))
            elif kind == "rna":
                sequences.append(RNAInput(
                    id=ids, sequence=entity["sequence"], modifications=mods))
            else:
                sequences.append(DNAInput(
                    id=ids, sequence=entity["sequence"], modifications=mods))
        elif kind == "ion":
            ccd_code = str(entity.get("ccd") or "").upper()
            if not ccd_code:
                raise ValueError(f"ion chain {chain} needs a ccd")
            sequences.append(LigandInput(id=ids, ccd=[ccd_code]))
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
    spi, _chain_names = build_inputs(spec["inputs"])
    builder = ESMFold2InputBuilder()

    predictions = output_dir / "input" / f"seed_{seed}" / "predictions"
    predictions.mkdir(parents=True, exist_ok=True)

    started = time.time()
    # prepare/forward/decode kept separate so chain_infos (per-token asym ids)
    # survives the decode — fold() hides it and the PAE contract needs it.
    features, chain_infos = builder.prepare_input(spi, seed=seed, device=model.device)
    # prepare_input consumed the seed for conformer generation; re-seed
    # around the forward so diffusion sampling is reproducible too
    # (mirrors the SDK's fold(), processor.py:426).
    from esm.models.esmfold2.processor import _seed_context
    with torch.no_grad(), _seed_context(seed):
        output = model(
            **features,
            num_loops=args.num_loops,
            num_sampling_steps=args.num_sampling_steps,
            num_diffusion_samples=args.samples,
        )
    results = builder.decode(
        output, features, chain_infos,
        num_diffusion_samples=args.samples,
        complex_id="pred",
    )
    if not isinstance(results, list):
        results = [results]
    print(f"folded {len(results)} sample(s) in {time.time() - started:.0f}s",
          file=sys.stderr)

    summaries = []
    for index, result in enumerate(results):
        sample_cif = result.complex.to_mmcif()
        # Per-token asym ids from chain_infos (token grid), NOT the collapsed
        # per-residue complex.chain_id — PAE/plddt live on the token grid.
        token_asym = []
        for ci in chain_infos:
            token_asym.extend([ci.asym_id] * len(ci.tokens))
        pae = result.pae.detach().cpu().numpy() if result.pae is not None else None
        plddt = (result.plddt.detach().cpu().numpy()
                 if result.plddt is not None else None)
        if plddt is not None and len(token_asym) != len(plddt):
            raise RuntimeError(
                f"token asym count {len(token_asym)} != plddt count {len(plddt)}")
        iptm = float(result.iptm) if result.iptm is not None else None
        ptm = float(result.ptm) if result.ptm is not None else None
        mean_plddt = float(plddt.mean()) if plddt is not None else None
        ranking_score = (
            0.8 * mean_plddt + 0.2 * iptm
            if mean_plddt is not None and iptm is not None else None)

        (predictions / f"input_sample_{index}.cif").write_text(sample_cif)

        full_data = {
            "token_pair_pae": pae.tolist() if pae is not None else [],
            "token_asym_id": token_asym,
            "token_plddt": plddt.tolist() if plddt is not None else [],
            "ranking_score": ranking_score,
        }
        (predictions / f"input_full_data_sample_{index}.json").write_text(
            json.dumps(full_data))

        summary = {
            "iptm": iptm,
            "ptm": ptm,
            "plddt": mean_plddt * 100.0 if mean_plddt is not None else None,
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
            "confidence": {**summary, "iptm": iptm,
                           "plddt": (mean_plddt * 100.0
                                     if mean_plddt is not None else None)},
        })

    best = max(summaries, key=lambda s: s["confidence"]["ranking_score"] or -1.0)
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
