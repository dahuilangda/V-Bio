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


def _cif_interface_geometry(cif_path, ligand_chain: str):
    """Heavy-atom interface geometry of one predicted sample.

    Returns (min_dist, clash_pairs) between the binder chain and every
    other polymer chain, or None when the CIF is missing/unreadable. This
    is the geometric gate the best-sample selection was missing: a sample
    with a buried main-chain interpenetration can top the ipsae chart precisely BECAUSE the interface is
    over-buried — score alone selects for it.
    """
    try:
        import gemmi
        st = gemmi.read_structure(str(cif_path))
        st.setup_entities()
        st.remove_hydrogens()
    except Exception:
        return None
    rec_rows: list[list[float]] = []
    binder_rows: list[list[float]] = []
    for ch in st[0]:
        target = binder_rows if ch.name == ligand_chain else rec_rows
        for r in ch:
            if r.het_flag == 'H':
                continue
            for a in r:
                if a.element == gemmi.Element('H'):
                    continue
                target.append([a.pos.x, a.pos.y, a.pos.z])
    if not rec_rows or not binder_rows:
        return None
    rec = np.asarray(rec_rows)
    binder = np.asarray(binder_rows)
    d = np.sqrt(((rec[:, None] - binder[None]) ** 2).sum(-1))
    return float(d.min()), int((d < 2.2).sum())


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
        # Per-sample interface geometry for the best-sample gate below.
        conf_file = entry.get("file") or ""
        if conf_file:
            sample_idx = entry.get("sample")
            pred_dir = Path(conf_file).parent
            cif_hits = sorted(pred_dir.glob(f"*sample_{sample_idx}.cif"))
            if cif_hits:
                geom = _cif_interface_geometry(cif_hits[0], ligand_chain)
                if geom is not None:
                    entry["interface_min_dist"] = round(geom[0], 3)
                    entry["interface_clash_pairs"] = geom[1]
    if not by_sample:
        return
    w_dom, w_iptm, w_max = _interface_weights()
    def score(entry):
        return (w_dom * float(entry.get("ipsae_dom") or 0.0)
                + w_iptm * float(entry.get("iptm") or 0.0)
                + w_max * float(entry.get("ligand_ipsae_max") or 0.0))
    # Geometric gate on the shipped best: among samples with a physically
    # clean interface (no heavy-atom pair under 2.2 A), ship the
    # highest-scoring one. Score alone selects for over-buried interfaces
    #. When NO sample is clean the score winner still ships but is
    # flagged so downstream consumers can demand a re-run.
    clean = [e for e in summary["confidences"]
             if e.get("interface_clash_pairs") == 0
             and float(e.get("interface_min_dist") or 0.0) >= 2.3]
    pool = clean if clean else summary["confidences"]
    best = max(pool, key=score)
    best["interface_score"] = round(score(best), 4)
    if not clean:
        best["geometry_flagged"] = True
        log.warning(
            "best sample %s ships with interface clashes (min %.2f A, %d "
            "pairs under 2.2 A) — no clean sample in this run",
            best.get("sample"), float(best.get("interface_min_dist") or 0.0),
            int(best.get("interface_clash_pairs") or 0))
    summary["n_geometrically_clean"] = len(clean)
    summary["best_by_interface"] = best


def _center_complex_on_receptor(
    complex_path: Path, work_dir: Path,
    peptide_letters: list[str], linker_letters: list[str],
) -> Path:
    """Translate the whole complex so the RECEPTOR centroid sits at origin.

    The trunk/denoiser were trained on origin-centred complexes: an upload
    in its crystal frame (RANKL trimer, centroid ~87 A off origin) is out
    of distribution and the sampler never attaches the free chain --
    an off-origin receptor can prevent peptide attachment entirely
    195-265 A away (interface 208+ A) while origin-centred receptors bound
    normally. The output is therefore delivered in the centred frame
    (translation is biologically meaningless; superposition downstream is
    frame-independent).
    """
    import gemmi
    st = gemmi.read_structure(str(complex_path))
    st.setup_entities()
    st.remove_hydrogens()
    rec_pts = []
    for ch in st[0]:
        if ch.name in peptide_letters or ch.name in linker_letters:
            continue
        for r in ch:
            if r.het_flag == "H":
                continue
            for a in r:
                rec_pts.append((a.pos.x, a.pos.y, a.pos.z))
    if not rec_pts:
        return complex_path
    c = np.asarray(rec_pts, dtype=np.float64).mean(axis=0)
    if float(np.linalg.norm(c)) < 1.0:
        return complex_path  # already centred
    st_full = gemmi.read_structure(str(complex_path))
    st_full.setup_entities()
    for model in st_full:
        for ch in model:
            for r in ch:
                for a in r:
                    a.pos = gemmi.Position(a.pos.x - c[0], a.pos.y - c[1], a.pos.z - c[2])
    centered = work_dir / f"{complex_path.stem}_centered{complex_path.suffix}"
    work_dir.mkdir(parents=True, exist_ok=True)
    st_full.write_pdb(str(centered))
    log.info("complex centred on receptor centroid (offset %.1f A applied; "
             "output ships in the centred frame)", float(np.linalg.norm(c)))
    return centered

def _run_peptide_engine(
    args, work_dir: Path, output_dir: Path
) -> tuple[Path, dict[str, Any]]:
    """Peptide-mode engine orchestration: receptor-fixed inpainting.

    The input complex (--input) carries the receptor + placed peptide (+
    bicyclic linker). The peptide enters the input json as a proteinChain —
    never as an SDF — with optional covalent_bonds to the linker. The
    receptor is held to the input pose by an SE(3)-equivariant reference
    restraint (PROTENIX_TARGET_REF_PATH); TFG guidance carries the covalent
    bond pairs (bicyclic chemistry).
    """
    complex_path = Path(args.input).expanduser().resolve()
    if not complex_path.is_file():
        raise SystemExit(f"--input complex file not found: {complex_path}")
    complex_path = _center_complex_on_receptor(
        complex_path, work_dir,
        peptide_letters=[s for s in (args.peptide_chain or "").split(",") if s.strip()],
        linker_letters=[s for s in (args.linker_chain or "").split(",") if s.strip()])

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
            args.msa_server_url, work_dir / "msa", msa_mode=args.msa_mode,
        )
        for chain in receptor_letters
    }
    # MSA is enabled for the peptide chain too (user policy: MSA everywhere,
    # HARD failure on miss — the env-db service returns usable
    # alignments for designed peptides and the MSA measurably improves the
    # refine; a no-MSA run is not an equivalent candidate and must not
    # silently proceed).
    # 1800s matches the env-db boundary: the GPU prefilter is fast,
    # but the CPU result2msa stage on a 20-aa designed peptide runs 30-60 min
    # (thousands of weak env hits). A shorter deadline systematically fails.
    peptide_msa_timeout = int(os.environ.get("P2D_PEPTIDE_MSA_TIMEOUT_SECONDS", "1800"))
    msa_paths[peptide.chain_name] = resolve_msa(
        peptide.sequence, peptide.chain_name, msa_cache,
        args.msa_server_url, work_dir / "msa",
        timeout=max(60, peptide_msa_timeout),
        msa_mode=args.msa_mode,
    )

    # Covalent bonds for the peptide entity: bicyclic SG<->linker-anchor
    # pairs AND cyclic head-tail N-C pairs both ride --bond_pairs; the
    # former cross to a linker entity, the latter stay inside the peptide
    # (entity1 == entity2). Both land in input.json covalent_bonds (model
    # conditions on the topology) and in TFG contact pairs below (hard
    # projection on x0) — pose-independent chemistry either way.
    linker_entity = None
    covalent_bonds: list[dict[str, Any]] = []
    staged_bond_pairs: list[tuple[tuple[str, int, str], tuple[str, int, str]]] = []
    peptide_entity_no = len(receptor_names) + 1  # 1-based entity id
    raw_pairs = [p.strip() for p in (args.bond_pairs or "").split(";") if p.strip()]

    def _parse_ref(ref: str) -> tuple[str, int, str]:
        parts = ref.strip().split(":")
        if len(parts) != 3:
            raise SystemExit(f"malformed bond atom reference {ref!r} (expect chain:resnum:atom)")
        return parts[0], int(parts[1]), parts[2]

    n_linker_bonds = 0
    if linker_letters:
        linker_entity = linker_letters[0]
        linker_entity_no = len(receptor_names) + 2
    for pair in raw_pairs:
        a1, a2 = (p.strip() for p in pair.split(","))
        ref1, ref2 = _parse_ref(a1), _parse_ref(a2)
        link_refs = [r for r in (ref1, ref2) if r[0] in linker_letters]
        if link_refs:
            if not linker_letters:
                raise SystemExit(
                    f"bond pair {pair!r} references a linker chain but --linker_chain is unset")
            pep_ref, link_ref = (
                (ref1, ref2) if ref1[0] in peptide_letters else (ref2, ref1))
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
            n_linker_bonds += 1
        elif ref1[0] in peptide_letters and ref2[0] in peptide_letters:
            covalent_bonds.append({
                "entity1": peptide_entity_no,
                "copy1": 1,
                "position1": ref1[1],
                "atom1": ref1[2],
                "entity2": peptide_entity_no,
                "copy2": 1,
                "position2": ref2[1],
                "atom2": ref2[2],
            })
        else:
            raise SystemExit(
                f"bond pair {pair!r} does not touch the peptide chain "
                f"{peptide_letters} or the linker {linker_letters or '<none>'}")
        staged_bond_pairs.append((ref1, ref2))
    if linker_letters and not n_linker_bonds:
        raise SystemExit("linker chain given but --bond_pairs defines no linker bonds")

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
        input_json, complex_path, entity_chain_names,
        require_complete=bool(getattr(args, "score_only", False)))


    blind_peptide = bool(getattr(args, "blind_peptide", False))
    free_entities = {
        len(receptor_names),
        *((len(receptor_names) + 1,) if linker_letters else ()),
    }
    # cyclic_chains: head-tail bond is a TRUE amide bond when bond_pairs
    # connect the peptide's own N-terminus to its C-terminus. Detection is
    # topological (both refs on the peptide chain, positions == {first,last},
    # atoms == {N,C}), not a hard-coded chain letter.
    pep_len = len(peptide.residues) if hasattr(peptide, 'residues') else 0
    headtail_refs: list[bool] = []
    for pair in raw_pairs:
        a1, a2 = (p.strip() for p in pair.split(","))
        try:
            r1, r2 = _parse_ref(a1), _parse_ref(a2)
        except SystemExit:
            headtail_refs.append(False)
            continue
        same_pep = (r1[0] in peptide_letters and r2[0] in peptide_letters)
        terminal_pos = ({r1[1], r2[1]} == {1, pep_len}) if pep_len else False
        nc_atoms = {r1[2], r2[2]} == {"N", "C"}
        headtail_refs.append(same_pep and terminal_pos and nc_atoms)
    is_cyclic = any(headtail_refs)
    tfg_constraints = compute_free_chain_tfg_constraints(
        info, coords, mask, free_entities=free_entities,
        cyclic_chains=is_cyclic)
    # Inter-chain VDW shell: lower-bound pairs every binder heavy atom
    # against every receptor heavy atom. The blind route's steric
    # attractive and the denoiser happily proposes buried x0; this family
    # rides the PROJECTED constraint channel, pushing any buried pose back
    # to the surface on every guided step (root fix for interpenetration).
    pep_entity_rows = list(info["entity_rows"][len(receptor_names)])
    # inter-chain covalent bonds (peptide<->linker) resolve to assembled
    # rows so the shell can exclude them: clash-constraining a bonded pair
    # makes the shell fight the bond every guided step
    interchain_bond_rows: list[tuple[int, int]] = []
    for bond in covalent_bonds:
        if bond["entity1"] == bond["entity2"]:
            continue
        # input.json entity ids are 1-based; entity_rows is 0-based (the
        # peptide itself is indexed as entity_rows[len(receptor_names)])
        for a1 in resolve_entity_atom_rows(
                info, bond["entity1"] - 1, bond["position1"],
                [bond["atom1"].strip("[]")]):
            for a2 in resolve_entity_atom_rows(
                    info, bond["entity2"] - 1, bond["position2"],
                    [bond["atom2"].strip("[]")]):
                interchain_bond_rows.append((a1, a2))
    vdw = compute_vdw_shell_constraints(
        info, pep_entity_rows,
        interchain_bond_pairs=interchain_bond_rows or None)
    if vdw is not None:
        if tfg_constraints is None:
            tfg_constraints = {k: v for k, v in vdw.items()}
        else:
            tfg_constraints = {
                k: np.concatenate([tfg_constraints[k], vdw[k]],
                                  axis=1 if k == "pairwise_distance_index" else 0)
                for k in vdw
            }
        log.info("inter-chain VDW shell: %d lower-bound pairs (floor 3.1 A)",
                 vdw["pairwise_distance_lower_bound"].shape[0])
    stereo = compute_stereo_peptide_bonds(info, mask, free_entities=free_entities)
    if stereo is not None:
        # gate for the guidance term: validate_features crashes when a
        # configured term lacks its feats (dock-mode npz carries none)
        os.environ["PROTENIX_TFG_STEREO"] = "1"
        if tfg_constraints is None:
            tfg_constraints = {}
        tfg_constraints.update(stereo)
        log.info("peptide-bond omega stereo constraints: %d trans quadruples "
                 "(X-Pro left free)", stereo["stereo_bond_orientation"].shape[0])
    if tfg_constraints is not None:
        tfg_npz = work_dir / "tfg_constraints.npz"
        np.savez(tfg_npz, **tfg_constraints,
                 elements=np.asarray(info["elements"]).astype(str),
                 atom_names=np.asarray(info["atom_names"]).astype(str),
                 res_id=np.asarray(info["res_id"]).astype(np.int64),
                 asym=np.asarray(info["asym"]).astype(np.int64))
        os.environ["PROTENIX_TFG_CONSTRAINTS_PATH"] = str(tfg_npz)
        n_bond = int(tfg_constraints["pairwise_distance_is_bond"].sum())
        n_angle = int(tfg_constraints["pairwise_distance_is_angle"].sum())
        log.info("free-chain TFG constraints: %d bonds + %d angles",
                 n_bond, n_angle)

    init_mask = mask
    if blind_peptide:
        pep_rows = info["entity_rows"][len(receptor_names)]
        init_mask = mask.copy()
        init_mask[pep_rows] = 0.0
        log.info("blind peptide: %d peptide atoms start from pure noise "
                 "(full schedule)", int((mask[pep_rows] > 0).sum()))
        # BLIND ROUTE RUNS TFG IN ENERGY-ONLY MODE. Root fix (
        # T1-T4 A/B): the raw sampler produces F/Y/W/H side chains with
        # perfect CCD geometry (ring planes near zero,
        # junction angles +-3 deg, OH in-plane); the TFG PROJECTED
        # projected channel that the TFG guidance fed whenever the
        # constraints npz exists (peptide mode writes it unconditionally
        # -- --no_guidance was a silent no-op) dragged rings into boats
        # (0.41-0.53 A). Energy-only keeps the soft steric mu-gradient
        # (pushes the lightly-buried side chains the bare sampler leaves,
        # n22 2-5) with an EMPTY projected channel.
        os.environ.setdefault("PROTENIX_TFG_ENERGY_ONLY", "1")

    init_npz = work_dir / "init_coords.npz"
    np.savez(init_npz, coords=coords, mask=init_mask)
    os.environ["PROTENIX_INIT_COORDS_PATH"] = str(init_npz)

    # Fixed target: the receptor is restrained to its input pose by an
    # SE(3)-EQUIVARIANT reference potential, not by coordinate clamping. The
    # sampler applies a random global rotation+translation at the start of
    # every diffusion step (centre_random_augmentation, AF3 Alg. 19), so an
    # absolute-coordinate clamp is only valid in the frame the step began in
    # -- the free atoms ride the frame while the clamped ones do not, putting
    # the state outside the training distribution. The
    # restraint instead aligns the reference onto the current frame by Kabsch
    # at every evaluation and penalises one-sided deviation with a bounded
    # linear gradient -- Boltz-2's force-template formulation.
    # Receptor atoms absent from the input carry no reference and stay free
    # (a zero coordinate row would pull them to the origin).
    # One representative atom per receptor residue (CB, else CA): the
    # target is held as a rigid body, so anchoring every atom would apply an
    # independent pull to each and stretch its own bonds. boltz-2 makes the same
    # choice -- its force-template restrains `token_to_rep_atom` only.
    ref_mask = np.zeros(len(coords), dtype=np.float32)
    for entity in range(len(receptor_names)):
        rows = info["entity_rows"][entity]
        ref_mask[rows] = mask[rows]
    n_unreferenced = int(
        sum((mask[info["entity_rows"][e]] == 0).sum()
            for e in range(len(receptor_names))))
    if n_unreferenced:
        log.warning(
            "%d receptor atom(s) absent from the input run free",
            n_unreferenced)
    # CCD rest-length bands for the free chains: chemistry, not guidance --
    # projected onto the sampler state every step (angles then bonds last) so
    # the free chain keeps its bond geometry while the sampler moves it.
    # Free entities = the chains that denoise (peptide + linker). Their
    # covalent bonds and aromatic rings get tight two-sided bands; the
    # receptor contributes the clash-shell lower bounds (pinned atoms carry
    # zero correction in the projector, so the shell pushes only the
    # peptide).
    _free_entities = {
        len(receptor_names),
        *((len(receptor_names) + 1,) if linker_entity is not None else ()),
    }
    try:
        _ccd_bands = compute_ccd_bond_bands(
            info, coords, mask, free_entities=_free_entities)
    except Exception as _band_exc:
        _ccd_bands = None
        log.warning("CCD bond bands unavailable: %s", _band_exc)
    if _ccd_bands is not None:
        (_chem_idx, _chem_up, _chem_lo, _clash_idx, _clash_lo,
         _rigid, _aro_dof) = _ccd_bands
        _band_npz = work_dir / "ccd_bond_bands.npz"
        _band_payload: dict[str, Any] = {
            "pair_index": _chem_idx, "upper": _chem_up, "lower": _chem_lo}
        if _rigid is not None:
            _band_payload["ring_rows"] = _rigid[0]
            _band_payload["ring_coords"] = _rigid[1]
            _band_payload["ring_bb_rows"] = _rigid[2]
            _band_payload["ring_bb_coords"] = _rigid[3]
        if _aro_dof is not None:
            _band_payload["aro_dof"] = _aro_dof
            # rebuild mode only: the analytic side-chain rebuild moves
            # interface atoms onto exact CCD geometry; without the clash
            # floors those corrections bury into the pinned receptor
            #. Default route has no
            # rebuilds -- the network's own side chains need no floors.
            if os.environ.get("PROTENIX_AROMATIC_REBUILD", "") in ("1", "true"):
                os.environ.setdefault("PROTENIX_CLASH_SHELL_PROJECT", "1")
        np.savez(_band_npz, **_band_payload)
        os.environ["PROTENIX_CCD_BOND_BANDS_PATH"] = str(_band_npz)
        log.info("CCD chemistry bands: %d bonds+rings + %d rigid aromatic "
                 "templates", len(_chem_up),
                 int(_rigid[0].shape[0]) if _rigid is not None else 0)
        if _clash_idx is not None:
            _clash_npz = work_dir / "clash_shell.npz"
            np.savez(_clash_npz, pair_index=_clash_idx, lower=_clash_lo)
            os.environ["PROTENIX_CLASH_SHELL_PATH"] = str(_clash_npz)
            # Default ON for the peptide route: the TFG projection cleans
            # the denoiser's x0, but the EMITTED state (Euler extrapolation
            # + noise + the chemistry projections below) is never itself
            # guarded — weak-prior interfaces shipped 1-3 sub-2.2 A pairs.
            # The generator's band projector runs after the chemistry each
            # step, pin-aware (receptor rows take zero correction), at the
            # 2.6 A guard floor written above. Explicit env wins.
            os.environ.setdefault("PROTENIX_CLASH_SHELL_PROJECT", "1")
            log.info("clash shell bands written: %d one-sided 2.6 A guard "
                     "floors (per-step pin-aware projection ON)",
                     len(_clash_lo))

    pin_npz = work_dir / "pin_mask.npz"
    np.savez(pin_npz, pin=ref_mask)
    os.environ["PROTENIX_PIN_MASK_PATH"] = str(pin_npz)
    log.info("pinned %d/%d receptor atoms (fixed-target inpainting; only "
             "the peptide is generated)",
             int(ref_mask.sum()), len(ref_mask))

    guidance = not args.no_guidance
    if guidance:
        # Pocket semantics follow the binder TOPOLOGY. A cyclic peptide is
        # compact: the solvent-side anchor box localizes it and the engine
        # runs the term at constant weight. A linear chain cannot ball up
        # within `upper` of one atom -- anchor forced a 16-mer (~50 A
        # extended) into a 12 A ball and crushed it into the receptor
        #. Linear chains take boltz2's per-residue contact
        # groups with its guidance weight ramp instead.
        # cyclic topology still feeds the TFG bond list (head-tail amide);
        is_cyclic = any(headtail_refs)
        # backbone stereochemistry guard (all-residue CA chirality + omega
        # planarity) for the free chain: computed later once the assembled
        # table exists

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
            # head-tail cyclization pairs ride the TRUE bond constraint in
            # compute_free_chain_tfg_constraints (is_bond=1, amide rest);
            # sending them through the contact path as an is_angle band
            # [2.0, 2.31] fights that constraint every guided step
            contact_pairs = [p_ for p_, is_ht in zip(staged_bond_pairs, headtail_refs)
                             if not is_ht]
            bonds = compute_bond_contact_pairs(
                info, contact_pairs, float(args.bond_upper),
                staged_to_auto=staged_to_auto) if contact_pairs else None
            if bonds is not None:
                contact_arrays.append(bonds[0])
                contact_uppers.append(bonds[1])
                n_bond_pairs = len(bonds[1])
        if contact_arrays:
            contacts = work_dir / "tfg_contacts.npz"
            np.savez(
                contacts,
                pair_index=np.concatenate(contact_arrays, axis=0),
                upper=np.concatenate(contact_uppers, axis=0),
            )
            os.environ["PROTENIX_TFG_CONTACTS_PATH"] = str(contacts)
            log.info("TFG contacts: %d bond pairs", n_bond_pairs)

    # Ring-bond enforcement contract (TFG-native):
    #   - input.json covalent_bonds   -> featurizer bond features (the model
    #     conditions on the ring topology)
    #   - compute_bond_contact_pairs  -> TFG PairwiseDistancePotential pairs
    #     (upper = --bond_upper), projected on x0 every guided step
    #   - TFG projects only the denoiser's clean estimate, so intermediate
    #     high-noise states may transiently break bonds; the orchestrator's
    #     post-refine _dpeptide_linker_bond_report gate rejects candidates
    #     whose final structure has broken ring bonds (no silent fallback).
    # Free-chain physics bands were computed above, right after the coords
    # alignment, from the ORIGINAL mask (see the --blind_peptide note there);
    # recomputing here would drop the peptide's chemistry guard under blind.

    return input_json, {
        "coords": coords,
        "mask": mask,
        "info": info,
        "peptide_entity": len(receptor_names),
        "linker_entity": (len(receptor_names) + 1) if linker_entity is not None else None,
        "entity_chain_names": entity_chain_names,
    }


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
            # pocket guidance removed (user decision):
            # the            # ligand always samples the whole surface; the model's own
            # docking prior locates the binding site
            else:
                log.info("dock mode: BLIND — ligand samples "
                         "the whole surface")
            log.info("dock mode: native blind inpainting — pinned %d/%d "
                     "protein atoms, %d ligand atoms from noise",
                     int(pin.sum()), len(pin), int((mask[lig_rows] > 0).sum()))
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
