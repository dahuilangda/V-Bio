#!/usr/bin/env python3
"""protenix2dock — protein-ligand structure workflow on the Protenix engine.

Six modes with Boltz2Score-compatible semantics:

    score      score an input complex as-is (diffusion bypassed)
    pose       refine around the input pose        (sigma_max 0.02)
    refine     general flexible refinement         (sigma_max 0.03)
    interface  interface-weighted refinement       (sigma_max 0.04)
    dock       native blind inpainting — receptor pinned, ligand from
                                                   pure noise, full schedule
    peptide    receptor-fixed peptide inpainting   (mirror-space D-peptide design)

Pipeline: parse inputs -> MSA -> featurize/align -> engine (docker image,
vendored source) -> per-sample confidence + IPSAE -> ranked summary JSON.

Runs inside vbio-protenix-v2-runtime with PYTHONPATH pointing at
vendor/protenix-source; see README.md for the full invocation contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from core.input_prep import (
    build_input_json,
    build_peptide_complex_input,
    load_ligand_pose,
    place_dock_conformer,
    resolve_msa,
)
from core.alignment import (
    align_complex_init_coords,
    align_init_coords,
)
from core.constraints import (
    compute_ccd_bond_bands,
    resolve_entity_atom_rows,
    compute_bond_contact_pairs,
    compute_free_chain_tfg_constraints,
    compute_ligand_covalent_bands,
    compute_stereo_peptide_bonds,
    compute_vdw_shell_constraints,
)
from core.ipsae import compute_ipsae_for_output
from core.modes import SUPPORTED_MODES, built_in_config
from core.interface_metrics import add_interface_metrics
from core.peptide_engine import run_peptide_engine
from core.runner import collect_results, run_protenix
from core.structure import parse_protein_chains

log = logging.getLogger("protenix2dock")

def _interface_weights() -> tuple[float, float, float]:
    """(ipsae_dom, iptm, ligand_ipsae_max) ranking weights.

    Defaults favor ipsae_dom; override via P2D_INTERFACE_WEIGHTS.
    """
    raw = os.environ.get("P2D_INTERFACE_WEIGHTS", "").strip()
    if raw:
        parts = tuple(float(x) for x in raw.split(","))
        if len(parts) == 3:
            return parts  # type: ignore[return-value]
        log.warning("ignoring malformed P2D_INTERFACE_WEIGHTS=%r", raw)
    return (0.5, 0.3, 0.2)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--mode", required=True, choices=SUPPORTED_MODES)
    p.add_argument("--protein_file",
                   help="protein structure (.pdb/.cif/.mmcif); non-polymer artifacts stripped")
    p.add_argument("--ligand_file",
                   help="posed ligand SDF (score/pose/refine/interface)")
    p.add_argument("--input",
                   help="combined complex file; score mode evaluates it as-is")
    p.add_argument("--ligand_smiles",
                   help="ligand SMILES (dock mode)")
    p.add_argument("--target_chain",
                   help="comma-separated auth chain ids to keep (default: all)")
    p.add_argument("--output_dir", required=True)
    p.add_argument("--work_dir")
    p.add_argument("--model_name", default="protenix-v2")
    p.add_argument("--checkpoint_dir", default="/workspace/model")
    p.add_argument("--msa_server_url")
    p.add_argument("--msa_mode", default="auto",
                   help="MSA search tier: auto (length-predicted: env for "
                        "chains >=50 aa, uniref below), env (UniRef30+envdb, "
                        "slow for short peptides), uniref (UniRef30 only)")
    p.add_argument("--msa_cache_dir", default="/data/msa_cache")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--sampling_steps", type=int,
                   help="diffusion steps override (default: mode config)")
    p.add_argument("--diffusion_samples", type=int,
                   help="sample count override (default: mode config)")
    p.add_argument("--sigma_max", type=float,
                   help="schedule s_max override (default: mode config)")
    p.add_argument("--affinity_head_ckpt",
                   help="native affinity head checkpoint (train_affinity.py); "
                        "outputs appear in summary_confidence")
    p.add_argument("--low_vram", action="store_true")
    p.add_argument("--no_guidance", action="store_true",
                   help="disable TFG guidance and contact injection")
    p.add_argument("--blind_peptide", action="store_true",
                   help="peptide mode: peptide denoises from PURE NOISE "
                        "(receptor still pinned = blind inpainting). The "
                        "PairFormer trunk + MSA conditioning produce the "
                        "binding pose; staged peptide coordinates are only "
                        "used for the chemistry (bond/angle) TFG constraints. "
                        "Uses the FULL noise schedule (sigma_max 160, 200 "
                        "steps) instead of the local refine schedule"
                        "guidance is pose-independent and composes with it.")
    # peptide mode (receptor-fixed peptide design/refinement)
    p.add_argument("--peptide_chain",
                   help="peptide chain(s) in the input complex (free, proteinChain)")
    p.add_argument("--linker_chain",
                   help="bicyclic linker chain in the input complex (CCD ligand)")
    p.add_argument("--linker_ccd", default="SEZ",
                   help="CCD code of the linker (default SEZ)")
    p.add_argument("--peptide_sequence",
                   help="authoritative one-letter peptide sequence (default: parsed from the complex)")
    p.add_argument("--bond_pairs",
                   help="bicyclic bonds as 'chain:resnum:atom,chain:resnum:atom;...' "
                        "(peptide SG <-> linker anchor), e.g. 'B:1:SG,L:1:CD;B:9:SG,L:1:C1'")
    p.add_argument("--bond_upper", type=float, default=1.5,
                   help="TFG upper bound for the covalent bond pairs (A)")
    p.add_argument("--score_only", action="store_true",
                   help="peptide mode: bypass diffusion, score the input pose "
                        "with the confidence heads (bit-exact pass-through)")
    p.add_argument("--interface_chains",
                   help="chain groups defining the reported interface, as two "
                        "comma-separated groups of auto chain letters — "
                        "'A,B' (receptor vs peptide/ligand) or 'AB,C' "
                        "(multi-chain receptor vs ligand). ipSAE uses the "
                        "second group's ligand chain and pair_iptm the "
                        "weakest cross-group pair; default derives the "
                        "ligand chain from the entity order")
    return p.parse_args(argv)


def classify_complex_chains(complex_path: Path):
    """(polymer_chains, nonpolymer_chains) of the input complex by entity
    type — the routing basis for score mode (protein complex vs ligand
    complex)."""
    import gemmi

    structure = gemmi.read_structure(str(complex_path))
    structure.setup_entities()
    polymers, nonpolymers = [], []
    for chain in structure[0]:
        n_poly = sum(
            1 for r in chain if r.entity_type == gemmi.EntityType.Polymer)
        if n_poly >= 3:
            polymers.append(chain.name.strip())
        elif sum(1 for r in chain) > 0:
            nonpolymers.append(chain.name.strip())
    return polymers, nonpolymers


def split_complex_file(complex_path: Path, work_dir: Path):
    """Split a combined complex into protein PDB + ligand SDF.

    Classification is by ENTITY TYPE, not chain name: every polymer chain
    (peptide or protein) stays on the protein side; only non-polymer
    entities become the ligand. The old name-based rule turned peptide
    chains into SDF ligands — scored without proteinChain identity or MSA
    (measured on 1YCR: peptide pLDDT 0.51 as ligand vs 0.92 as
    proteinChain)."""
    import gemmi
    from rdkit import Chem

    structure = gemmi.read_structure(str(complex_path))
    structure.setup_entities()

    def _short(chain_name: str, used: set) -> str:
        # PDB chain IDs are one character; mmCIF-style ids ('Axp') get a
        # deterministic unused letter
        base = (chain_name.strip().upper() or "A")[0]
        out = base
        while out in used:
            out = chr(ord("A") + (ord(out) - ord("A") + 1) % 26
                      ) if len(used) < 26 else out + "x"
        used.add(out)
        return out

    protein = gemmi.Structure()
    ligand = gemmi.Structure()
    protein.add_model(gemmi.Model("1"))
    ligand.add_model(gemmi.Model("1"))
    used_ids: set = set()
    for chain in structure[0]:
        n_poly = sum(
            1 for r in chain if r.entity_type == gemmi.EntityType.Polymer)
        target = protein if n_poly >= 3 else ligand
        clone = chain.clone()
        clone.name = _short(clone.name, used_ids)
        target[0].add_chain(clone)
    protein.setup_entities()
    ligand.setup_entities()

    protein_path = work_dir / "input_protein.pdb"
    protein_path.write_text(protein.make_pdb_string())
    ligand_pdb = work_dir / "input_ligand.pdb"
    ligand.write_pdb(str(ligand_pdb))

    mol = Chem.MolFromPDBFile(str(ligand_pdb), removeHs=False)
    if mol is None:
        raise ValueError(f"no parseable ligand chain in {complex_path}")
    ligand_sdf = work_dir / "input_ligand.sdf"
    writer = Chem.SDWriter(str(ligand_sdf))
    writer.write(mol)
    writer.close()
    return protein_path, ligand_sdf, mol


def resolve_inputs(args, work_dir: Path):
    """Return (protein_path, ligand_sdf, ligand_mol) for the requested mode."""
    if args.input:
        protein, ligand_sdf, mol = split_complex_file(
            Path(args.input).expanduser().resolve(), work_dir
        )
        log.info("split complex: %d ligand atoms", mol.GetNumAtoms())
        return protein, ligand_sdf, mol

    if not args.protein_file:
        raise SystemExit("--protein_file is required unless --input is given")

    if args.mode == "dock":
        if not args.ligand_smiles:
            raise SystemExit("dock mode requires --ligand_smiles")
        # Native inpainting: the ligand input pose is NOT a start — its rows
        # are re-noised (mask=0) and only the molecular identity (tokens/
        # ref_pos) is consumed. Any placement position works; the centroid
        # keeps the input file geometrically sane for parsers.
        import gemmi

        st = gemmi.read_structure(str(args.protein_file))
        st.setup_entities()
        pts = [np.array([a.pos.x, a.pos.y, a.pos.z])
               for ch in st[0] for res in ch
               if res.entity_type == gemmi.EntityType.Polymer
               for a in res if a.element != gemmi.Element("H")]
        center = tuple(np.mean(pts, axis=0).tolist()) if pts else (0, 0, 0)
        ligand_sdf = work_dir / "placed_ligand.sdf"
        mol = place_dock_conformer(args.ligand_smiles, center, ligand_sdf,
                                   seed=args.seed)
        log.info("ligand identity staged at centroid %s (%d heavy atoms) — "
                 "pose discarded by native inpainting", center,
                 mol.GetNumAtoms())
        return Path(args.protein_file), ligand_sdf, mol

    if not args.ligand_file:
        raise SystemExit(f"{args.mode} mode requires --ligand_file")
    return Path(args.protein_file), Path(args.ligand_file), load_ligand_pose(
        Path(args.ligand_file))


def build_engine_inputs(args, protein_path, ligand_sdf, ligand_mol, work_dir):
    """MSA + input.json + aligned init coords for the engine.

    Returns (input_json, coords, mask, info, chains) — `chains` is the parsed
    ProteinChainData list in entity order
    translation (author seqid -> assembled ordinal).
    """
    keep_chains = [c for c in (args.target_chain or "").split(",") if c.strip()] or None
    chains = parse_protein_chains(protein_path, keep_chains=keep_chains)
    log.info("parsed %d chain(s): %s", len(chains),
             ", ".join(f"{c.chain_name}({len(c.sequence)}aa)" for c in chains))

    msa_cache = Path(args.msa_cache_dir) if args.msa_cache_dir else None
    msa_paths = {
        chain.chain_name: resolve_msa(
            chain.sequence, chain.chain_name, msa_cache,
            args.msa_server_url, work_dir / "msa", msa_mode=args.msa_mode,
        )
        for chain in chains
    }

    input_json = work_dir / "input.json"
    input_json.write_text(json.dumps([
        build_input_json(
            chains=chains,
            ligand_sdf=ligand_sdf,
            sample_name="protenix2dock_job",
            msa_paths=msa_paths,
            seeds=[args.seed],
        )
    ], indent=2), encoding="utf-8")

    coords, mask, info = align_init_coords(
        input_json, chains, ligand_mol,
        require_complete=bool(getattr(args, "score_only", False)))
    np.savez(work_dir / "init_coords.npz", coords=coords, mask=mask)
    return input_json, coords, mask, info, chains


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(argv)

    config = built_in_config(args.mode)
    output_dir = Path(args.output_dir).expanduser().resolve()
    work_dir = (Path(args.work_dir).expanduser().resolve()
                if args.work_dir else output_dir / "_work")
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    if (args.mode == "score" and args.input
            and not getattr(args, "_rerouted", False)):
        # Protein/protein complex (receptor + peptide/protein binder, no
        # small-molecule entity): the ligand pipeline would cast the second
        # polymer chain as an SDF ligand — no proteinChain identity, no MSA
        # (measured on 1YCR: peptide pLDDT 0.51 mis-cast vs 0.92 correct).
        # Route to the peptide engine's score path instead: both chains are
        # proteinChains with MSA and the confidence heads score the input
        # pose directly.
        polymers, nonpolymers = classify_complex_chains(
            Path(args.input).expanduser().resolve())
        if len(polymers) >= 2 and not nonpolymers:
            log.info(
                "score mode: protein complex (%d polymer chains) -> "
                "peptide-engine scoring (proteinChains + MSA)", len(polymers))
            args.mode = "peptide"
            args.score_only = True
            args._rerouted = True
            if not args.peptide_chain or args.peptide_chain == "B":
                # production staged-complex contract: the LAST polymer
                # chain is the designed peptide
                args.peptide_chain = polymers[-1]

    if args.mode in ("peptide", "rediffuse"):
        input_json, peptide_info = run_peptide_engine(args, work_dir)
        coords, mask = peptide_info["coords"], peptide_info["mask"]
        log.info(
            "peptide mode: receptor chains=%s peptide=%s linker=%s",
            peptide_info["entity_chain_names"][: peptide_info["peptide_entity"]],
            peptide_info["entity_chain_names"][peptide_info["peptide_entity"]],
            peptide_info.get("linker_entity"),
        )
    else:
        protein_path, ligand_sdf, ligand_mol = resolve_inputs(args, work_dir)
        input_json, coords, mask, info, chains = build_engine_inputs(
            args, protein_path, ligand_sdf, ligand_mol, work_dir
        )

    # Engine side-channels (env contract, see vendor patches). The dock/
    # peptide engines write their own init/pin npz files and set this env
    # themselves; the ligand score path passes coordinates without diffusion.
    os.environ["PROTENIX_INIT_COORDS_PATH"] = str(work_dir / "init_coords.npz")
    if args.mode == "score":
        os.environ["PROTENIX_SCORE_ONLY"] = "1"
    if args.mode == "peptide" and args.score_only:
        os.environ["PROTENIX_SCORE_ONLY"] = "1"
    if args.affinity_head_ckpt:
        os.environ["PROTENIX_AFFINITY_CKPT"] = str(
            Path(args.affinity_head_ckpt).expanduser().resolve())

    # peptide mode sets its own TFG contacts (covalent bonds)
    # and pin mask inside _run_peptide_engine; the generic ligand-anchored
    # guidance below applies to the ligand modes only.
    guidance = not args.no_guidance
    if args.mode != "peptide":
        if args.mode == "dock":
            # NATIVE blind inpainting (protocol decision; the
            # hand-rolled placement ensemble + steric anchor search removed):
            #   - protein atoms WITH source coordinates are held to the
            #     input pose by the SE(3)-equivariant restraint (same
            #     contract as peptide mode; zero-coordinate rows stay free)
            #   - the ligand denoises from PURE NOISE on the FULL schedule —
            #     the engine's own trained docking task; no external pose
            #     prior is invented
            #   - ligand covalent chemistry rides the TFG channel
            #     (RDKit topology, pose-independent)
            #   - the steric term keeps the binder out of the receptor wall
            #     the run is blind (whole-surface search)
            # protein atoms WITH source coordinates are restrained to the
            # input pose by the SE(3)-equivariant reference potential (same
            # contract as peptide mode; zero-coordinate rows stay free)
            lig_rows = info["ligand_rows"]
            dock_ref_mask = np.zeros(len(coords), dtype=np.float32)
            protein_rows = np.setdiff1d(
                np.arange(len(coords)), lig_rows)
            dock_ref_mask[protein_rows] = mask[protein_rows]
            init_mask_dock = mask.copy()
            init_mask_dock[lig_rows] = 0.0
            init_npz = work_dir / "init_coords.npz"
            np.savez(init_npz, coords=coords, mask=init_mask_dock)
            os.environ["PROTENIX_INIT_COORDS_PATH"] = str(init_npz)
            dock_pin_npz = work_dir / "pin_mask.npz"
            np.savez(dock_pin_npz, pin=dock_ref_mask)
            os.environ["PROTENIX_PIN_MASK_PATH"] = str(dock_pin_npz)
            lig_cov = (compute_ligand_covalent_bands(lig_rows, ligand_mol)
                       if not args.no_guidance else None)
            if lig_cov is not None:
                lig_cov_npz = work_dir / "covalent_bonds.npz"
                np.savez(
                    lig_cov_npz,
                    pair_index=lig_cov[0], upper=lig_cov[1], lower=lig_cov[2])
                os.environ["PROTENIX_COVALENT_BONDS_PATH"] = str(lig_cov_npz)
                log.info("dock mode: %d ligand covalent bond bands", len(lig_cov[1]))
            # Pocket-guided docking (the same PocketPotential the peptide
            # path uses, applied to the ligand's free atoms): the user's
            # pocket guidance is handled separately:
            # the            # ligand always samples the whole surface; the model's own
            # docking prior locates the binding site
            else:
                log.info("dock mode: BLIND — ligand samples "
                         "the whole surface")
            log.info("dock mode: native blind inpainting — pinned %d/%d "
                     "protein atoms, %d ligand atoms from noise",
                     int(dock_ref_mask.sum()), len(dock_ref_mask),
                     int((mask[lig_rows] > 0).sum()))
    else:
        # peptide mode: engine setup happened in _run_peptide_engine above
        pass

    n_steps = int(args.sampling_steps
                  if args.sampling_steps is not None else config["sampling_steps"])
    n_samples = int(args.diffusion_samples
                    if args.diffusion_samples is not None else config["diffusion_samples"])
    sigma_max = float(args.sigma_max
                      if args.sigma_max is not None else config["sigma_max"])
    if getattr(args, "blind_peptide", False) and args.mode == "peptide":
        # Blind inpainting overrides the peptide local-refine ladder with the
        # ORIGINAL full noise schedule — the peptide starts from pure noise.
        # (dock mode's config already carries the full schedule.) Explicit
        # --sigma_max/--sampling_steps win.
        if args.sigma_max is None:
            sigma_max = 160.0
        if args.sampling_steps is None:
            n_steps = 200
    log.info("mode=%s sigma_max=%.3f steps=%d samples=%d guidance=%s",
             args.mode, sigma_max, n_steps, n_samples, guidance)

    run_protenix(
        input_json_path=input_json,
        output_dir=output_dir,
        model_name=args.model_name,
        checkpoint_dir=Path(args.checkpoint_dir),
        seeds=[args.seed],
        n_step=max(n_steps, 1),
        n_sample=max(n_samples, 1),
        # score mode carries no schedule; the engine default covers it.
        sigma_max=sigma_max if sigma_max > 0 else 160.0,
        guidance_enable=guidance,
        low_vram=args.low_vram,
    )

    summary = collect_results(output_dir)
    summary["mode"] = args.mode
    # ipSAE ligand chain: user-declared interface second group, else the
    # entity after the protein chains (auto letters A, B, ... follow
    # input.json entity order); hardcoding "B" silently scores the wrong
    # interface on multi-chain receptors (homodimers)
    interface = (args.interface_chains or "").replace(" ", "") or None
    if interface:
        ligand_chain = interface.split(",")[-1][0]
    elif args.mode == "peptide":
        ligand_chain = chr(ord("A") + peptide_info["peptide_entity"])
    else:
        keep_chains = ([c for c in (args.target_chain or "").split(",") if c.strip()]
                       or None)
        ligand_chain = chr(
            ord("A")
            + len(parse_protein_chains(protein_path, keep_chains=keep_chains)))
    add_interface_metrics(summary, output_dir, ligand_chain=ligand_chain,
                          interface=interface)

    # Engine can fail while exiting 0; empty confidences are a task failure.
    if not summary.get("confidences"):
        err_text = ""
        err_dir = output_dir / "ERR"
        if err_dir.exists():
            for err_file in sorted(err_dir.iterdir()):
                err_text += f"\n--- {err_file.name} ---\n{err_file.read_text(errors='replace')[-4000:]}"
        raise RuntimeError(
            f"protenix2dock {args.mode} produced no confidence outputs "
            f"(n_confidences=0).{err_text}"
        )

    summary_path = output_dir / "protenix2dock_summary.json"
    # Align the ranking-best pointer with the geometric gate: when the
    # interface-best is a physically clean sample it becomes THE best;
    # a clash-flagged interface-best leaves ranking-best in place (the
    # flag travels with the entry so consumers can see why).
    bbi = summary.get("best_by_interface")
    if (bbi and not bbi.get("geometry_flagged")
            and summary.get("best", {}).get("sample") != bbi.get("sample")):
        summary["best"] = bbi
    summary_path.write_text(json.dumps(summary, indent=2, default=str),
                            encoding="utf-8")
    best = summary.get("best")
    if best:
        log.info("best sample: ranking=%.4f iptm=%.4f",
                 best["ranking_score"], best["iptm"])
    log.info("%s complete -> %s", args.mode, output_dir)


if __name__ == "__main__":
    main()
