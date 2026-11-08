"""Peptide-mode engine orchestration: receptor-fixed inpainting with
mirror-space D-peptide design support."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from core.alignment import align_complex_init_coords
from core.constraints import (
    compute_ccd_bond_bands,
    compute_free_chain_tfg_constraints,
    compute_ligand_covalent_bands,
    compute_stereo_peptide_bonds,
    compute_vdw_shell_constraints,
    resolve_entity_atom_rows,
)
from core.input_prep import build_peptide_complex_input, resolve_msa
from core.runner import run_protenix
from core.structure import parse_protein_chains

log = logging.getLogger("protenix2dock")


def center_complex_on_receptor(
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

def run_peptide_engine(args, work_dir: Path) -> tuple[Path, dict[str, Any]]:
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
    complex_path = center_complex_on_receptor(
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
        # 盲样路径固定为 energy-only TFG：projected 通道会把 F/Y/W/H 侧链
        # 的环平面拖入船式构象（实测 0.41-0.53 A）；energy-only 保留软
        # 位斥梯度，维持轻度埋藏侧链的展开姿态。
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
    staged_to_auto = {
        name: chr(ord("A") + i)
        for i, name in enumerate(entity_chain_names)
    }
    if _ccd_bands is not None:
        (_chem_idx, _chem_up, _chem_lo, _clash_idx, _clash_lo,
         _rigid, _aro_dof) = _ccd_bands
        _band_npz = work_dir / "ccd_bond_bands.npz"
        _band_payload: dict[str, Any] = {
            "pair_index": _chem_idx, "upper": _chem_up, "lower": _chem_lo}
        # Inter-entity covalent bonds (Cys-SG <-> linker anchors) ride the
        # same two-sided bands: the TFG contact channel carries only an upper
        # bound, which prevents tears but not collapses. Rest length follows
        # the anchor chemistry: thioether C-S ~1.81 A (SEZ/29N), Bi-S
        # coordination ~2.55 A (BS3 — the CCD entry is a bare Bi ion, so the
        # rest length is coordination chemistry, not a template value).
        _sg_pairs = [p_ for p_, is_ht in zip(staged_bond_pairs, headtail_refs)
                     if not is_ht]
        if _sg_pairs:
            _is_bi = any(ref[2] == "BI" for pair in _sg_pairs for ref in pair)
            _rest, _half = (2.55, 0.35) if _is_bi else (1.81, 0.35)
            _resolved = compute_bond_contact_pairs(
                info, _sg_pairs, _rest + _half, staged_to_auto=staged_to_auto)
            if _resolved is not None:
                _n = len(_resolved[0])
                _band_payload["pair_index"] = np.concatenate(
                    [_chem_idx, _resolved[0]], axis=0)
                _band_payload["upper"] = np.concatenate(
                    [_chem_up, np.full(_n, _rest + _half, dtype=np.float32)])
                _band_payload["lower"] = np.concatenate(
                    [_chem_lo, np.full(_n, max(_rest - _half, 0.5),
                                       dtype=np.float32)])
                log.info("SG-anchor covalent bands: %d two-sided pairs "
                         "(rest=%.2f A)", _n, _rest)
                if _is_bi and _n >= 2:
                    # Mutual SG-SG separation: a single-ion anchor can draw
                    # all three thiolates into one cramped fan (SG-Bi-SG
                    # down to 13 degrees, SG-SG 1.2 A). Bi(III) thiolate is
                    # trigonal-pyramidal: S-S ~3.6 A at ~90-100 degrees.
                    _elements = np.char.upper(np.asarray(info["elements"]).astype(str))
                    _sg_rows = sorted({int(r[0]) if _elements[int(r[0])] == "S"
                                       else int(r[1]) for r in _resolved[0]})
                    for i in range(len(_sg_rows)):
                        for j in range(i + 1, len(_sg_rows)):
                            _band_payload["pair_index"] = np.concatenate([
                                _band_payload["pair_index"],
                                np.array([[_sg_rows[i], _sg_rows[j]]],
                                         dtype=np.int64)])
                            _band_payload["upper"] = np.concatenate(
                                [_band_payload["upper"], np.array([6.0],
                                dtype=np.float32)])
                            _band_payload["lower"] = np.concatenate(
                                [_band_payload["lower"], np.array([3.0],
                                dtype=np.float32)])
                    log.info("Bi SG-SG separation bands: %d pairs",
                             len(_sg_rows) * (len(_sg_rows) - 1) // 2)
        if _rigid is not None:
            _band_payload["ring_rows"] = _rigid[0]
            _band_payload["ring_coords"] = _rigid[1]
            _band_payload["ring_bb_rows"] = _rigid[2]
            _band_payload["ring_bb_coords"] = _rigid[3]
        if _aro_dof is not None:
            _band_payload["aro_dof"] = _aro_dof
            # rebuild 模式：解析侧链重建把界面原子对齐到精确 CCD 几何，
            # 无 clash 下限时这些修正会埋进钉死的受体；默认路由无重建。
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



