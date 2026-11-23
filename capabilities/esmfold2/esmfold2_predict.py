#!/usr/bin/env python3
"""ESMFold2 prediction capability.

Runs inside the vbio-esmfold2-runtime container. Parses the platform
prediction request (boltz-style sequences + constraints), folds it with
ESMFold2, and writes results in the mol1/protenix output contract so the
platform's downstream (result archive metrics, PAE heatmap, ipSAE, MSA
depth) consumes it unchanged:

  <output>/input/seed_<seed>/predictions/
    input_sample_<i>.cif                      all-atom model
    input_full_data_sample_<i>.json           token_pair_pae, token_asym_id,
                                              token_plddt
    input_summary_confidence_sample_<i>.json  iptm, ptm, ranking_score, plddt
  <output>/summary.json                        engine, best, n_summaries

Boundary notes (see configs/engine_capabilities.json):
  - L-peptides only; D positions would need D-form CCD modifications.
  - Supported constraints: bond (head-to-tail cyclization, staples, linker
    anchors) via StructurePredictionInput.covalent_bonds. Contact/pocket
    belong to protenix2dock.
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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.runtime.backends.request import PredictionRequest  # noqa: E402


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


def inject_custom_ccd(request) -> None:
    """自定义残基注入 esm CCD 表（本进程内，按任务隔离）。

    编码已由平台在申请 GPU 前对照本引擎的 CCD 表改过名，这里只构建与注入。
    """
    from backend.runtime.custom_ccd_builder import _build_custom_ccd_mol
    from esm.models.esmfold2.conformers import load_ccd

    ccd = load_ccd()
    for item in request.custom_ccd_molecules:
        if item.get("kind") != "residue":
            continue
        mol = _build_custom_ccd_mol(item["smiles"], kind="residue",
                                    backbone=item.get("backbone"),
                                    amidated=bool(item.get("cTerminalAmidated")))
        for atom in mol.GetAtoms():
            props = atom.GetPropsAsDict()
            if "leaving_atom" in props:
                atom.ClearProp("leaving_atom")
                atom.SetProp("leaving_atom", "1" if props["leaving_atom"] else "0")
        ccd[item["ccd"]] = mol


def build_sequences(request: PredictionRequest) -> list:
    from esm.models.esmfold2 import (
        DNAInput, LigandInput, Modification, ProteinInput, RNAInput,
    )
    from string import ascii_uppercase

    sequences = []
    for index, entity in enumerate(request.entities):
        raw_ids = entity.ids
        if raw_ids:
            chain = raw_ids[0]
        else:
            chain = (ascii_uppercase[index]
                     if index < 26
                     else ascii_uppercase[index // 26 - 1] + ascii_uppercase[index % 26])
            raw_ids = [chain]

        if entity.kind in ("protein", "rna", "dna"):
            mods = []
            seq_len = len(entity.sequence)
            for mod in entity.modifications:
                if not 1 <= mod.position <= seq_len:
                    raise ValueError(
                        f"modification position {mod.position} out of range "
                        f"1..{seq_len} for chain {chain} (ccd {mod.ccd})")
                mods.append(Modification(position=mod.position - 1, ccd=mod.ccd))
            if entity.kind == "protein":
                sequences.append(ProteinInput(
                    id=raw_ids, sequence=entity.sequence, modifications=mods))
            elif entity.kind == "rna":
                sequences.append(RNAInput(
                    id=raw_ids, sequence=entity.sequence, modifications=mods))
            else:
                sequences.append(DNAInput(
                    id=raw_ids, sequence=entity.sequence, modifications=mods))
        elif entity.kind == "ion":
            if not entity.ccd:
                raise ValueError(f"ion chain {chain} needs a ccd")
            sequences.append(LigandInput(id=raw_ids, ccd=entity.ccd))
        else:
            if entity.smiles:
                sequences.append(LigandInput(id=raw_ids, smiles=entity.smiles))
            elif entity.ccd:
                sequences.append(LigandInput(id=raw_ids, ccd=entity.ccd))
            else:
                raise ValueError(f"ligand chain {chain} needs smiles or ccd")

    lengths = request.sequence_lengths()
    for bond in request.bonds:
        for chain, residue in ((bond.chain1, bond.residue1),
                               (bond.chain2, bond.residue2)):
            if chain not in lengths:
                raise ValueError(f"bond constraint references unknown chain: {chain}")
            if not 1 <= residue <= lengths[chain]:
                raise ValueError(
                    f"bond residue {residue} out of range 1..{lengths[chain]} "
                    f"for chain {chain}")

    return sequences


def build_covalent_bonds(request: PredictionRequest, sequences: list) -> list:
    """平台 bond 约束 -> SDK CovalentBond；原子名到位置的映射
    委托 SDK tokenizer 预扫，自动处理离去基剥离与修饰残基原子序。"""
    from collections import defaultdict

    from esm.models.esmfold2 import CovalentBond, StructurePredictionInput
    from esm.models.esmfold2.prepare_input import build_chains_from_input

    ends = []
    placeholder_bonds = []
    for bond in request.bonds:
        end1 = (bond.chain1, bond.residue1, bond.atom1)
        end2 = (bond.chain2, bond.residue2, bond.atom2)
        ends.append((end1, end2))
        # 占位键触发共价链的离去基剥离，使原子表与正式运行一致
        placeholder_bonds.append(CovalentBond(
            chain_id1=end1[0], res_idx1=end1[1] - 1, atom_idx1=0,
            chain_id2=end2[0], res_idx2=end2[1] - 1, atom_idx2=0))

    placeholder = StructurePredictionInput(
        sequences=sequences, covalent_bonds=placeholder_bonds)
    chains, tokens, atoms = build_chains_from_input(placeholder)
    chain_by_asym = {c.asym_id: c.chain_id for c in chains}
    atom_index_by_residue: dict = defaultdict(dict)
    for atom in atoms:
        if not atom.is_valid or atom.token_index >= len(tokens):
            continue
        token = tokens[atom.token_index]
        key = (chain_by_asym.get(token.asym_id, token.asym_id), token.residue_index)
        atom_index_by_residue[key].setdefault(atom.name, len(atom_index_by_residue[key]))

    bonds = []
    for end1, end2 in ends:
        resolved = []
        for chain, residue, atom_name in (end1, end2):
            atom_map = atom_index_by_residue.get((chain, residue - 1), {})
            if atom_name not in atom_map:
                known = sorted(atom_map)
                raise ValueError(
                    f"bond atom {atom_name!r} not found at {chain}:{residue} "
                    f"(known atoms: {known})")
            resolved.append((chain, residue - 1, atom_map[atom_name]))
        c1, r1, i1 = resolved[0]
        c2, r2, i2 = resolved[1]
        bonds.append(CovalentBond(
            chain_id1=c1, res_idx1=r1, atom_idx1=i1,
            chain_id2=c2, res_idx2=r2, atom_idx2=i2))
    return bonds


def build_inputs(request: PredictionRequest):
    from esm.models.esmfold2 import StructurePredictionInput

    sequences = build_sequences(request)
    covalent_bonds = build_covalent_bonds(request, sequences)
    return StructurePredictionInput(
        sequences=sequences,
        covalent_bonds=covalent_bonds or None)


def main() -> None:
    args = parse_args()
    request = PredictionRequest.from_yaml(Path(args.yaml).read_text())
    inject_custom_ccd(request)

    output_dir = Path(args.output_dir)
    seed = args.seed

    import torch
    from esm.models.esmfold2 import EsmFold2Model

    model = EsmFold2Model.from_pretrained(args.weights, device="cuda").eval()
    spi = build_inputs(request)
    from esm.models.esmfold2 import ESMFold2InputBuilder
    builder = ESMFold2InputBuilder()

    predictions = output_dir / "input" / f"seed_{seed}" / "predictions"
    predictions.mkdir(parents=True, exist_ok=True)

    started = time.time()
    features, chain_infos = builder.prepare_input(spi, seed=seed, device=model.device)
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
