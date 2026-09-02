#!/usr/bin/env python3
"""protenix2dock — protein-ligand structure workflow on the Protenix engine.

Six modes with Boltz2Score-compatible semantics:

    score      score an input complex as-is (diffusion bypassed)
    pose       refine around the input pose        (sigma_max 0.02)
    refine     general flexible refinement         (sigma_max 0.03)
    interface  interface-weighted refinement       (sigma_max 0.04)
    dock       place a SMILES ligand into a pocket box and refine
                                                   (sigma_max 0.05, rigid receptor)
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
    _bond_pairs_as_rows,
    align_complex_init_coords,
    align_init_coords,
    build_input_json,
    build_peptide_complex_input,
    compute_bond_contact_pairs,
    compute_free_chain_tfg_constraints,
    compute_contact_pairs,
    compute_ligand_covalent_bands,
    load_ligand_pose,
    place_dock_conformer,
    resolve_msa,
)
from core.ipsae import compute_ipsae_for_output
from core.modes import SUPPORTED_MODES, built_in_config
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
    p.add_argument("--center_x", type=float)
    p.add_argument("--center_y", type=float)
    p.add_argument("--center_z", type=float)
    p.add_argument("--size_x", type=float, default=18.0)
    p.add_argument("--size_y", type=float, default=18.0)
    p.add_argument("--size_z", type=float, default=18.0)
    p.add_argument("--target_chain",
                   help="comma-separated auth chain ids to keep (default: all)")
    p.add_argument("--output_dir", required=True)
    p.add_argument("--work_dir")
    p.add_argument("--model_name", default="protenix-v2")
    p.add_argument("--checkpoint_dir", default="/workspace/model")
    p.add_argument("--msa_server_url")
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
    p.add_argument("--blind", action="store_true",
                   help="dock mode: full-surface blind docking — no pocket box, "
                        "no placement ensemble, no receptor pin, no anchors; "
                        "the engine's standard full-noise diffusion generates "
                        "the complex (no init-coord side channels)")
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
    p.add_argument("--bond_upper", type=float, default=2.2,
                   help="TFG upper bound for the covalent bond pairs (A)")
    p.add_argument("--pocket_cutoff", type=float, default=9.0,
                   help="peptide-pocket anchoring contact cutoff (A)")
    p.add_argument("--pocket_upper", type=float, default=8.0,
                   help="peptide-pocket anchoring upper bound cap (A)")
    p.add_argument("--anchor_slack", type=float, default=0.3,
                   help="per-pair anchoring slack over the placed geometry (A); "
                        "0 disables per-pair tightening (flat pocket_upper)")
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


def split_complex_file(complex_path: Path, keep_chains, work_dir: Path):
    """Split a combined complex into protein PDB + ligand SDF."""
    import gemmi
    from rdkit import Chem

    structure = gemmi.read_structure(str(complex_path))
    structure.setup_entities()
    keep = {c.strip().upper() for c in keep_chains or ["A"] if c.strip()}

    def _short(chain_name: str, used: set) -> str:
        # PDB chain IDs are one character; mmCIF-style ids ('Axp') get a
        # deterministic unused letter
        base = (chain_name.strip().upper() or "A")[0]
        out = base
        while out in used:
            out = chr(ord("A") + (ord(out) - ord("A") + 1) % 26)
        used.add(out)
        return out

    protein = gemmi.Structure()
    ligand = gemmi.Structure()
    protein.add_model(gemmi.Model("1"))
    ligand.add_model(gemmi.Model("1"))
    used_ids: set = set()
    for chain in structure[0]:
        is_protein = chain.name.strip().upper() in keep
        target = protein if is_protein else ligand
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
        keep = [c for c in (args.target_chain or "").split(",") if c.strip()]
        protein, ligand_sdf, mol = split_complex_file(
            Path(args.input).expanduser().resolve(), keep, work_dir
        )
        log.info("split complex: %d ligand atoms", mol.GetNumAtoms())
        return protein, ligand_sdf, mol

    if not args.protein_file:
        raise SystemExit("--protein_file is required unless --input is given")

    if args.mode == "dock":
        if not args.ligand_smiles:
            raise SystemExit("dock mode requires --ligand_smiles")
        if args.blind:
            # blind docking: no pocket prior — the ligand conformer only
            # identifies the molecule (the engine ignores it as a start);
            # place it at the protein centroid
            import gemmi

            st = gemmi.read_structure(str(args.protein_file))
            st.setup_entities()
            pts = [np.array([a.pos.x, a.pos.y, a.pos.z])
                   for ch in st[0] for res in ch
                   if res.entity_type == gemmi.EntityType.Polymer
                   for a in res if a.element != gemmi.Element("H")]
            center = tuple(np.mean(pts, axis=0).tolist()) if pts else (0, 0, 0)
        else:
            if args.center_x is None or args.center_y is None or args.center_z is None:
                raise SystemExit("dock mode requires --center_x/--center_y/--center_z "
                                 "(or --blind for a full-surface search)")
            center = (args.center_x, args.center_y, args.center_z)
        ligand_sdf = work_dir / "placed_ligand.sdf"
        mol = place_dock_conformer(args.ligand_smiles, center, ligand_sdf,
                                   seed=args.seed)
        log.info("placed conformer at %s (%d heavy atoms)", center,
                 mol.GetNumAtoms())
        return Path(args.protein_file), ligand_sdf, mol

    if not args.ligand_file:
        raise SystemExit(f"{args.mode} mode requires --ligand_file")
    return Path(args.protein_file), Path(args.ligand_file), load_ligand_pose(
        Path(args.ligand_file))


def build_engine_inputs(args, protein_path, ligand_sdf, ligand_mol, work_dir):
    """MSA + input.json + aligned init coords for the engine."""
    keep_chains = [c for c in (args.target_chain or "").split(",") if c.strip()] or None
    chains = parse_protein_chains(protein_path, keep_chains=keep_chains)
    log.info("parsed %d chain(s): %s", len(chains),
             ", ".join(f"{c.chain_name}({len(c.sequence)}aa)" for c in chains))

    msa_cache = Path(args.msa_cache_dir) if args.msa_cache_dir else None
    msa_paths = {
        chain.chain_name: resolve_msa(
            chain.sequence, chain.chain_name, msa_cache,
            args.msa_server_url, work_dir / "msa",
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

    coords, mask, info = align_init_coords(input_json, chains, ligand_mol)
    np.savez(work_dir / "init_coords.npz", coords=coords, mask=mask)
    return input_json, coords, mask, info


def add_interface_metrics(summary: dict, output_dir: Path, ligand_chain="B",
                          interface: str | None = None):
    """ipSAE + interface-scoped iptm per sample plus the best-by-interface
    entry (mutates in place).

    `interface` is two comma-separated groups of chain letters ("A,B" or
    "AB,C"): pair_iptm is the WEAKEST cross-group pair from the engine's
    chain-pair matrix — the all-pairs global averages in linkers and
    receptor-receptor contacts, which misreports the interface the caller
    cares about. When unset it falls back to the global iptm.
    """
    by_sample = compute_ipsae_for_output(output_dir, ligand_chain_id=ligand_chain)

    def _pair_iptm_of(conf_file: str):
        if not interface or not conf_file:
            return None
        try:
            payload = json.loads(Path(conf_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        matrix = payload.get("chain_pair_iptm")
        if not matrix:
            return None
        try:
            first, second = (g.strip() for g in interface.split(","))
        except ValueError:
            return None
        idx = {ch: i for i, ch in enumerate(
            chr(ord("A") + k) for k in range(len(matrix)))}
        values = [float(matrix[idx[a]][idx[b]])
                  for a in first if a in idx for b in second if b in idx]
        return min(values) if values else None

    for entry in summary.get("confidences", []):
        ips = by_sample.get(entry.get("sample"))
        if ips:
            entry["ligand_ipsae_max"] = ips.get("ligand_ipsae_max")
            entry["ipsae_dom"] = ips.get("ipsae_dom")
            entry["ligand_plddt"] = ips.get("ligand_plddt_mean")
            entry["interface_pair_count"] = ips.get("interface_pair_count")
        pair = _pair_iptm_of(entry.get("file") or "")
        if pair is not None:
            entry["pair_iptm"] = round(pair, 4)
    if not by_sample:
        return
    w_dom, w_iptm, w_max = _interface_weights()
    def score(entry):
        return (w_dom * float(entry.get("ipsae_dom") or 0.0)
                + w_iptm * float(entry.get("iptm") or 0.0)
                + w_max * float(entry.get("ligand_ipsae_max") or 0.0))
    best = max(summary["confidences"], key=score)
    best["interface_score"] = round(score(best), 4)
    summary["best_by_interface"] = best


def _run_peptide_engine(
    args, work_dir: Path, output_dir: Path
) -> tuple[Path, dict[str, Any]]:
    """Peptide-mode engine orchestration: receptor-fixed inpainting.

    The input complex (--input) carries the receptor + placed peptide (+
    bicyclic linker). The peptide enters the input json as a proteinChain —
    never as an SDF — with optional covalent_bonds to the linker. The
    receptor is pinned to the input pose for every diffusion step (true
    inpainting via the PROTENIX_PIN_MASK_PATH side channel); TFG guidance
    carries the covalent bond pairs (bicyclic chemistry) + pocket anchoring
    contacts.
    """
    complex_path = Path(args.input).expanduser().resolve()
    if not complex_path.is_file():
        raise SystemExit(f"--input complex file not found: {complex_path}")

    peptide_letters = [s.strip() for s in (args.peptide_chain or "").split(",") if s.strip()]
    linker_letters = [s.strip() for s in (args.linker_chain or "").split(",") if s.strip()]
    if not peptide_letters:
        raise SystemExit("peptide mode requires --peptide_chain")
    receptor_letters = [
        c for c in parse_protein_chains(complex_path)
        if c.chain_name not in peptide_letters and c.chain_name not in linker_letters
    ]
    receptor_names = [c.chain_name for c in receptor_letters]
    if not receptor_names:
        raise SystemExit("peptide mode requires at least one receptor protein chain")

    peptide_chains = parse_protein_chains(complex_path, keep_chains=peptide_letters)
    if len(peptide_chains) != 1:
        raise SystemExit(
            f"peptide mode expects exactly one peptide chain, got "
            f"{[c.chain_name for c in peptide_chains]}"
        )
    peptide = peptide_chains[0]
    if args.peptide_sequence:
        authoritative = str(args.peptide_sequence).strip().upper()
        if len(authoritative) != len(peptide.sequence):
            raise SystemExit(
                f"--peptide_sequence length {len(authoritative)} does not match "
                f"the parsed peptide chain ({len(peptide.sequence)})"
            )
        peptide.sequence = authoritative

    msa_cache = Path(args.msa_cache_dir) if args.msa_cache_dir else None
    msa_paths = {
        chain.chain_name: resolve_msa(
            chain.sequence, chain.chain_name, msa_cache,
            args.msa_server_url, work_dir / "msa",
        )
        for chain in receptor_letters
    }

    # Bicyclic ring: peptide SG <-> linker anchor bonds.
    linker_entity = None
    covalent_bonds: list[dict[str, Any]] = []
    staged_bond_pairs: list[tuple[tuple[str, int, str], tuple[str, int, str]]] = []
    if linker_letters:
        linker_entity = linker_letters[0]
        peptide_entity_no = len(receptor_names) + 1  # 1-based entity id
        linker_entity_no = len(receptor_names) + 2
        raw_pairs = [p.strip() for p in (args.bond_pairs or "").split(";") if p.strip()]

        def _parse_ref(ref: str) -> tuple[str, int, str]:
            parts = ref.strip().split(":")
            if len(parts) != 3:
                raise SystemExit(f"malformed bond atom reference {ref!r} (expect chain:resnum:atom)")
            return parts[0], int(parts[1]), parts[2]

        for pair in raw_pairs:
            a1, a2 = (p.strip() for p in pair.split(","))
            ref1, ref2 = _parse_ref(a1), _parse_ref(a2)
            if ref1[0] in peptide_letters:
                pep_ref, link_ref = ref1, ref2
            else:
                pep_ref, link_ref = ref2, ref1
            covalent_bonds.append({
                "entity1": peptide_entity_no,
                "copy1": 1,
                "position1": pep_ref[1],
                "atom1": pep_ref[2],
                "entity2": linker_entity_no,
                "copy2": 1,
                "position2": link_ref[1],
                "atom2": link_ref[2],
            })
            staged_bond_pairs.append((ref1, ref2))
        if not covalent_bonds:
            raise SystemExit("linker chain given but --bond_pairs defines no bonds")

    entity_chain_names: list[str] = receptor_names + [peptide.chain_name]
    if linker_entity is not None:
        entity_chain_names.append(linker_entity)

    input_json = work_dir / "input.json"
    input_json.write_text(json.dumps([
        build_peptide_complex_input(
            receptor_chains=receptor_letters,
            peptide_chain=peptide,
            msa_paths=msa_paths,
            linker_ccd=str(args.linker_ccd).strip().upper() if linker_entity else None,
            covalent_bonds=covalent_bonds,
            sample_name="protenix2dock_peptide",
            seeds=[args.seed],
        )
    ], indent=2), encoding="utf-8")

    coords, mask, info = align_complex_init_coords(
        input_json, complex_path, entity_chain_names)
    init_npz = work_dir / "init_coords.npz"
    np.savez(init_npz, coords=coords, mask=mask)
    os.environ["PROTENIX_INIT_COORDS_PATH"] = str(init_npz)

    # Receptor pinning: receptor atoms WITH source coordinates are clamped to
    # the input pose on every diffusion step (true fixed-target inpainting).
    # Receptor atoms absent from the source stay free (noise start) — pinning
    # a zero coordinate row would clamp them to the origin. Peptide + linker
    # denoise freely from their placed poses.
    pin = np.zeros(len(coords), dtype=np.float32)
    for entity in range(len(receptor_names)):
        rows = info["entity_rows"][entity]
        pin[rows] = mask[rows]
    n_unpinned = int(
        sum((mask[info["entity_rows"][e]] == 0).sum()
            for e in range(len(receptor_names))))
    if n_unpinned:
        log.warning(
            "%d receptor atom(s) absent from the input complex run free",
            n_unpinned)
    pin_npz = work_dir / "pin_mask.npz"
    np.savez(pin_npz, pin=pin)
    os.environ["PROTENIX_PIN_MASK_PATH"] = str(pin_npz)
    log.info("pinned %d/%d receptor atoms", int(pin.sum()), len(pin))

    guidance = not args.no_guidance
    pocket = None
    if guidance:
        # staged chain names -> input.json auto chain letters (the assembled
        # atom table keys on the latter)
        staged_to_auto = {
            name: chr(ord("A") + i)
            for i, name in enumerate(entity_chain_names)
        }
        contact_arrays: list[np.ndarray] = []
        contact_uppers: list[np.ndarray] = []
        n_bond_pairs = 0
        if staged_bond_pairs:
            bonds = compute_bond_contact_pairs(
                info, staged_bond_pairs, float(args.bond_upper),
                staged_to_auto=staged_to_auto)
            if bonds is not None:
                contact_arrays.append(bonds[0])
                contact_uppers.append(bonds[1])
                n_bond_pairs = len(bonds[1])
        free_rows = np.concatenate([
            info["entity_rows"][len(receptor_names)],
        ] + ([info["entity_rows"][len(receptor_names) + 1]]
             if linker_entity is not None else []))
        pocket = compute_contact_pairs(
            coords, mask, free_rows,
            float(args.pocket_cutoff), float(args.pocket_upper),
            anchor_slack=float(args.anchor_slack),
        )
        if pocket is not None:
            contact_arrays.append(pocket[0])
            contact_uppers.append(pocket[1])
        if contact_arrays:
            contacts = work_dir / "tfg_contacts.npz"
            np.savez(
                contacts,
                pair_index=np.concatenate(contact_arrays, axis=0),
                upper=np.concatenate(contact_uppers, axis=0),
            )
            os.environ["PROTENIX_TFG_CONTACTS_PATH"] = str(contacts)
            log.info(
                "TFG contacts: %d bond + %d pocket pairs",
                n_bond_pairs,
                len(pocket[0]) if pocket is not None else 0,
            )

    # Hard geometric anchors enforced on x_t every diffusion step by the
    # vendored sampler (the TFG contacts above remain the soft/chemical
    # channel). TWO families:
    #   - pocket pairs, band [d-slack, d+slack]: keeps the peptide at the
    #     placed geometry — neither drifting away nor penetrating the wall
    #   - the covalent ring bonds, band [1.75, 2.05]: the deformation
    #     happens DURING diffusion, so the bonds need the same every-step
    #     hard treatment (TFG only projects the x0 prediction, not x_t)
    # Skipped entirely under --no_guidance (blind docking).
    # Physics bands for the free chains (peptide + linker), built on the
    # assembled atom table — the coordinates the sampler actually moves:
    #   - every intra-chain covalent bond as a tight rest-length band
    #   - a VDW clash floor on every receptor x free heavy pair
    # Both ride the anchor projection so a steric shove distributes over
    # bonded neighbours instead of tearing atoms off; bonds project last
    # (official angles-then-bonds ordering). The receptor stays pinned —
    # the projector's free/pinned weights never move pinned atoms.
    free_entities = {
        len(receptor_names),
        *((len(receptor_names) + 1,) if linker_entity is not None else ()),
    }
    tfg_constraints = compute_free_chain_tfg_constraints(
        info, coords, mask, free_entities=free_entities)
    # Clash floors ride the TFG contact set: the official
    # PairwiseDistancePotential enforces VDW lower bounds through its clash
    # category on x0.

    if tfg_constraints is not None:
        tfg_npz = work_dir / "tfg_constraints.npz"
        np.savez(tfg_npz, **tfg_constraints)
        os.environ["PROTENIX_TFG_CONSTRAINTS_PATH"] = str(tfg_npz)
        n_bond = int(tfg_constraints["pairwise_distance_is_bond"].sum())
        n_angle = int(tfg_constraints["pairwise_distance_is_angle"].sum())
        log.info("free-chain TFG constraints: %d bonds + %d angles",
                 n_bond, n_angle)

    return input_json, {
        "coords": coords,
        "mask": mask,
        "info": info,
        "peptide_entity": len(receptor_names),
        "linker_entity": (len(receptor_names) + 1) if linker_entity is not None else None,
        "entity_chain_names": entity_chain_names,
    }


def _placement_ensemble(
    coords: np.ndarray,
    mask: np.ndarray,
    lig_rows: np.ndarray,
    protein_rows: np.ndarray,
    n_samples: int,
    seed: int,
    center: np.ndarray | None = None,
    pool: int = 256,
    floor: float = 2.7,
    min_separation_deg: float = 30.0,
    conformers: list[np.ndarray] | None = None,
    box: np.ndarray | None = None,
) -> np.ndarray:
    """Per-sample start coordinates for the dock mode pose search.

    The local dock schedule cannot recover from a bad global placement, so
    each diffusion sample starts from its own placement of the conformer at
    the pocket centre. Candidates come from every conformer variant (a
    single ETKDG minimum is not the bound conformer of a flexible ligand),
    placed by deterministic shape alignment — the ligand's principal axes
    rotated onto the pocket cavity's principal axes — and a seeded random
    SO(3) pool for diversity. Every candidate is scored by steric overlap
    against the receptor minus a contact-shell reward (a pure overlap
    minimum points the ligand out of the pocket); the best-scoring,
    angularly separated starts seed the diffusion and the confidence
    ranking across samples is the pose search.
    """
    rng = np.random.default_rng(seed)
    lig = coords[lig_rows]
    center = lig.mean(axis=0) if center is None else np.asarray(center, float)
    known_protein = protein_rows[mask[protein_rows] > 0]
    pro = coords[known_protein]

    from scipy.spatial import cKDTree

    tree = cKDTree(pro)

    def _score(placed: np.ndarray) -> float:
        dist, _ = tree.query(placed, k=1)
        overlap = float((dist < floor).sum())
        contacts = float(((dist >= 3.0) & (dist <= 5.5)).sum())
        return overlap * 3.0 - contacts

    # pocket cavity: grid points of the search box not buried in the receptor
    half = (np.asarray(box, dtype=float) if box is not None
            else (lig.max(0) - lig.min(0)) / 2 + 4.0)
    steps = [np.arange(center[k] - half[k], center[k] + half[k], 0.8)
             for k in range(3)]
    grid = np.stack([a.reshape(-1) for a in np.meshgrid(*steps, indexing="ij")],
                    axis=1)
    d_grid, _ = tree.query(grid, k=1)
    void = grid[d_grid > 2.4]

    variants = [lig] + [c for c in (conformers or [])
                        if len(c) == len(lig)]
    # rotation AND translation probes: the crystal pose is an orientation
    # around the pocket centre plus a small offset from it
    offsets = np.array([[dx, dy, dz]
                        for dx in (-2.4, 0.0, 2.4)
                        for dy in (-2.4, 0.0, 2.4)
                        for dz in (-2.4, 0.0, 2.4)], dtype=np.float64)
    candidates: list[tuple[float, np.ndarray, np.ndarray]] = []  # score, rot, placed
    for body in variants:
        body_c = body - body.mean(0)
        if len(void) >= 10:
            cavity = void - void.mean(0)
            _, _, cavity_axes = np.linalg.svd(cavity, full_matrices=False)
            _, _, body_axes = np.linalg.svd(body_c, full_matrices=False)
            for signs in (np.diag([sx, sy, sz])
                          for sx in (1, -1) for sy in (1, -1) for sz in (1, -1)):
                rot = cavity_axes.T @ signs @ body_axes
                if np.linalg.det(rot) < 0:
                    continue
                oriented = body_c @ rot.T
                for off in offsets:
                    placed = oriented + center + off
                    candidates.append((_score(placed), rot, placed))
        q, r = np.linalg.qr(rng.normal(size=(max(pool // len(variants), 32), 3, 3)))
        rots = q * np.sign(np.diagonal(r, axis1=1, axis2=2))[:, None, :]
        # QR column-sign correction can still yield an improper rotation;
        # a reflected start is the wrong enantiomer and the small-sigma
        # schedule cannot repair chirality
        rots[np.linalg.det(rots) < 0, :, -1] *= -1.0
        for rot in rots:
            oriented = body_c @ rot.T
            for off in offsets:
                placed = oriented + center + off
                candidates.append((_score(placed), rot, placed))

    candidates.sort(key=lambda item: item[0])

    def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
        cos = np.clip((np.trace(a.T @ b) - 1.0) / 2.0, -1.0, 1.0)
        return float(np.degrees(np.arccos(cos)))

    picked: list[np.ndarray] = []
    picked_idx: list[int] = []
    picked_rots: list[np.ndarray] = []
    picked_offsets: list[np.ndarray] = []
    for idx, (_, rot, placed) in enumerate(candidates):
        off = placed.mean(axis=0) - center
        if any(_angle_deg(rot, other_rot) < min_separation_deg
               and np.linalg.norm(off - other_off) < 2.0
               for other_rot, other_off in zip(picked_rots, picked_offsets)):
            continue
        picked.append(placed)
        picked_idx.append(idx)
        picked_rots.append(rot)
        picked_offsets.append(off)
        if len(picked) == n_samples:
            break
    for idx, (_, _, placed) in enumerate(candidates):
        # separation floor exhausted the pool: fall back to the next-best
        # candidates not already picked
        if len(picked) >= n_samples:
            break
        if idx in picked_idx:
            continue
        picked.append(placed)

    ensemble = np.repeat(coords[None], n_samples, axis=0)
    for s, placed in enumerate(picked[:n_samples]):
        ensemble[s][lig_rows] = placed
    return ensemble


def _diverse_conformers(smiles: str, seed: int, k: int = 6) -> list[np.ndarray]:
    """Heavy-atom conformer variants for the dock placement ensemble.

    A single ETKDG minimum routinely differs from the bound conformer of a
    flexible ligand by more than the local dock schedule can travel, so the
    pose search seeds from several RMSD-diverse conformers. Atom order is
    the canonical SMILES order after H removal, matching the placed SDF the
    featurizer aligned against.
    """
    from rdkit import Chem
    from rdkit.Chem import AllChem, rdMolAlign

    template = Chem.MolFromSmiles(smiles)
    if template is None:
        return []
    mol = Chem.AddHs(template)
    kept_mols: list = []
    params = AllChem.ETKDGv3()
    for s in range(k * 6):
        params.randomSeed = seed + s
        cand = Chem.Mol(mol)
        if AllChem.EmbedMolecule(cand, params) != 0:
            continue
        AllChem.MMFFOptimizeMolecule(cand)
        cand = Chem.RemoveHs(cand)
        if kept_mols and any(
                rdMolAlign.GetBestRMS(cand, other) < 0.8
                for other in kept_mols):
            continue
        kept_mols.append(cand)
        if len(kept_mols) == k:
            break
    out = []
    for m in kept_mols:
        conf = m.GetConformer()
        out.append(np.array(
            [list(conf.GetAtomPosition(i)) for i in range(m.GetNumAtoms())],
            dtype=np.float64))
    return out


def _steric_anchor_pairs(
    placements: list[np.ndarray],
    coords: np.ndarray,
    mask: np.ndarray,
    lig_rows: np.ndarray,
    protein_rows: np.ndarray,
    floor: float = 2.7,
    select_radius: float = 11.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Hard-anchor steric floor between the ligand and the receptor.

    Pair every ligand atom with every protein atom within `select_radius` of
    ANY placement in the start ensemble, band [floor, 1e3]: the upper bound
    never fires, so the band is pure repulsion and stays valid for every
    ensemble start. The floor sits just under the shortest legitimate
    heavy-atom H-bond so it removes hard clashes without fighting real
    contacts.
    """
    empty_idx = np.zeros((0, 2), dtype=np.int64)
    empty_b = np.zeros(0, dtype=np.float32)
    if not placements or lig_rows.size == 0 or protein_rows.size == 0:
        return empty_idx, empty_b, empty_b
    known_protein = protein_rows[mask[protein_rows] > 0]
    pro = coords[known_protein]

    select = np.zeros(len(pro), dtype=bool)
    for lig in placements:
        d = np.linalg.norm(pro[:, None, :] - lig[None, :, :], axis=-1)
        select |= (d < select_radius).any(axis=1)
    if not select.any():
        return empty_idx, empty_b, empty_b

    pro_sel = np.where(select)[0]
    lig_all = np.concatenate(placements, axis=0)
    d = np.linalg.norm(pro[pro_sel][:, None, :] - lig_all[None, :, :], axis=-1)
    pi, li = np.where(d < select_radius)
    if pi.size == 0:
        return empty_idx, empty_b, empty_b
    lig_idx = lig_rows[li % len(lig_rows)]
    index = np.stack(
        [known_protein[pro_sel[pi]], lig_idx], axis=1).astype(np.int64)
    # the same (protein, ligand) pair can appear once per placement; the
    # projector's index_add would accumulate duplicate corrections
    index = np.unique(index, axis=0)
    lower = np.full(len(index), float(floor), dtype=np.float32)
    upper = np.full(len(index), 1e3, dtype=np.float32)
    return index, upper, lower


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

    if args.mode == "peptide":
        input_json, peptide_info = _run_peptide_engine(args, work_dir, output_dir)
        coords, mask = peptide_info["coords"], peptide_info["mask"]
        log.info(
            "peptide mode: receptor chains=%s peptide=%s linker=%s",
            peptide_info["entity_chain_names"][: peptide_info["peptide_entity"]],
            peptide_info["entity_chain_names"][peptide_info["peptide_entity"]],
            peptide_info.get("linker_entity"),
        )
    else:
        protein_path, ligand_sdf, ligand_mol = resolve_inputs(args, work_dir)
        input_json, coords, mask, info = build_engine_inputs(
            args, protein_path, ligand_sdf, ligand_mol, work_dir
        )

    # Engine side-channels (env contract, see vendor patches).
    if args.mode == "dock" and args.blind:
        # blind docking: no init coordinates at all — the engine's standard
        # full-noise diffusion generates the whole complex
        os.environ.pop("PROTENIX_INIT_COORDS_PATH", None)
    else:
        os.environ["PROTENIX_INIT_COORDS_PATH"] = str(work_dir / "init_coords.npz")
    if args.mode == "score":
        os.environ["PROTENIX_SCORE_ONLY"] = "1"
    if args.mode == "peptide" and args.score_only:
        os.environ["PROTENIX_SCORE_ONLY"] = "1"
    if args.affinity_head_ckpt:
        os.environ["PROTENIX_AFFINITY_CKPT"] = str(
            Path(args.affinity_head_ckpt).expanduser().resolve())

    # peptide mode sets its own TFG contacts (covalent bonds + pocket anchors)
    # and pin mask inside _run_peptide_engine; the generic ligand-anchored
    # guidance below applies to the ligand modes only.
    if args.mode != "peptide":
        guidance = (args.mode != "score" and not args.no_guidance
                    and not (args.mode == "dock" and args.blind))
        if args.mode == "dock" and not args.blind:
            # Rigid-receptor docking with a placement ensemble:
            #  - every protein atom with source coordinates is pinned to the
            #    input structure (same inpainting contract as peptide mode;
            #    zero-coordinate rows stay free — pinning them would clamp
            #    atoms to the origin)
            #  - the local dock schedule cannot recover from a bad global
            #    placement (all samples collapse to the single placed
            #    orientation), so each diffusion sample starts from its own
            #    rotation of the conformer around the box centre; ranking
            #    across samples is the engine's pose search
            #  - steric floor: hard-anchor lower bounds (~VDW contact
            #    distance) on ligand/protein pairs — the TFG channel only
            #    carries upper bounds, so without this the refinement can
            #    park the ligand 0.6 A inside the receptor wall
            pin = np.zeros(len(coords), dtype=np.float32)
            protein_rows = np.setdiff1d(
                np.arange(len(coords)), info["ligand_rows"])
            pin[protein_rows] = mask[protein_rows]

            n_samples = int(args.diffusion_samples) if args.diffusion_samples \
                else int(config["diffusion_samples"])
            init_npz = work_dir / "init_coords.npz"
            lig_rows = info["ligand_rows"]
            placements: list[np.ndarray] = []
            if n_samples > 1 and lig_rows.size and bool((mask[lig_rows] > 0).all()):
                ensemble = _placement_ensemble(
                    coords, mask, lig_rows, protein_rows,
                    n_samples=n_samples, seed=args.seed,
                    center=(np.array([args.center_x, args.center_y, args.center_z])
                            if args.center_x is not None else None),
                    conformers=_diverse_conformers(
                        getattr(args, "ligand_smiles", "") or "", args.seed),
                    box=(np.array([args.size_x, args.size_y, args.size_z]) / 2.0
                         if args.size_x is not None else None))
                np.savez(init_npz, coords=ensemble, mask=mask)
                placements = [ensemble[s][lig_rows] for s in range(n_samples)]
                log.info("dock mode: %d-start placement ensemble "
                         "(conformer x orientation, overlap-scored)", n_samples)
            else:
                np.savez(init_npz, coords=coords, mask=mask)
                if lig_rows.size:
                    placements = [coords[lig_rows]]

            steric = _steric_anchor_pairs(
                placements, coords, mask, lig_rows, protein_rows)
            anchor_npz = work_dir / "anchor_pairs.npz"
            np.savez(
                anchor_npz,
                pair_index=steric[0], upper=steric[1], lower=steric[2])
            os.environ["PROTENIX_ANCHOR_PAIRS_PATH"] = str(anchor_npz)
            pin_npz = work_dir / "pin_mask.npz"
            np.savez(pin_npz, pin=pin)
            os.environ["PROTENIX_PIN_MASK_PATH"] = str(pin_npz)
            # Ligand covalent bonds (RDKit graph, exact topology): same
            # every-step projection as peptide mode — the steric floor alone
            # can shove one ligand atom off its bonded neighbour. Blind dock
            # keeps the untouched full-noise generation path.
            lig_cov = compute_ligand_covalent_bands(lig_rows, ligand_mol)
            if lig_cov is not None:
                lig_cov_npz = work_dir / "covalent_bonds.npz"
                np.savez(
                    lig_cov_npz,
                    pair_index=lig_cov[0], upper=lig_cov[1], lower=lig_cov[2])
                os.environ["PROTENIX_COVALENT_BONDS_PATH"] = str(lig_cov_npz)
                log.info("dock mode: %d ligand covalent bond bands", len(lig_cov[1]))
            log.info("dock mode: pinned %d/%d protein atoms, %d steric floor pairs",
                     int(pin.sum()), len(pin), len(steric[0]))
        if guidance:
            pairs = compute_contact_pairs(
                coords, mask, info["ligand_rows"],
                float(config["anchor_contact_cutoff"]),
                float(config["anchor_max_distance"]),
            )
            if pairs is not None:
                contacts = work_dir / "tfg_contacts.npz"
                np.savez(contacts, pair_index=pairs[0], upper=pairs[1])
                os.environ["PROTENIX_TFG_CONTACTS_PATH"] = str(contacts)
    else:
        guidance = not args.no_guidance

    n_steps = int(args.sampling_steps
                  if args.sampling_steps is not None else config["sampling_steps"])
    n_samples = int(args.diffusion_samples
                    if args.diffusion_samples is not None else config["diffusion_samples"])
    sigma_max = float(args.sigma_max
                      if args.sigma_max is not None else config["sigma_max"])
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
    summary_path.write_text(json.dumps(summary, indent=2, default=str),
                            encoding="utf-8")
    best = summary.get("best")
    if best:
        log.info("best sample: ranking=%.4f iptm=%.4f",
                 best["ranking_score"], best["iptm"])
    log.info("%s complete -> %s", args.mode, output_dir)


if __name__ == "__main__":
    main()
