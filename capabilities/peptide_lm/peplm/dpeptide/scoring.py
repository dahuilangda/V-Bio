"""Score-mode validation of a D-target + L-peptide complex (validated T1/E6).

Score mode never runs diffusion: coordinates pass through the Boltz2
confidence head unchanged (measured 0.000 A identity; ipTM 0.89-0.95 on
transferred/native poses). This is the ONLY validated way to certify a pose —
diffusion refinement actively destroys correct poses (E1: a perfect 0.00 A
initial drifted to 11.8 A without MSA).
"""

from __future__ import annotations

import json
from pathlib import Path

from .docking import install_fixed_receptor_sampler, set_fixed_receptor_config


def _boltz_root() -> Path:
    import os

    return Path(os.environ.get("DPEPTIDE_BOLTZ_ROOT", "/data/Boltz2Score"))


def _boltz_cache() -> Path:
    import os

    return Path(os.environ.get("DPEPTIDE_BOLTZ_CACHE", "/data/boltz_cache"))


def _ensure_sys_path() -> None:
    import sys

    root = str(_boltz_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def load_model_once(cache_dir: Path | None = None, **overrides):
    cache_dir = Path(cache_dir or __import__("os").environ.get("DPEPTIDE_BOLTZ_CACHE", "/data/boltz_cache"))
    """Load the Boltz2 confidence checkpoint (reuse the project's loader)."""
    _ensure_sys_path()
    from core.inference import load_score_model

    kwargs = dict(
        cache_dir=cache_dir,
        checkpoint=None,
        recycling_steps=3,
        sampling_steps=100,
        diffusion_samples=2,
        max_parallel_samples=2,
        structure_refine=True,
        write_full_pae=True,
        step_scale=1.5,
        no_kernels=False,
        contact_guidance=True,
        use_potentials=False,
        reference_from_input=False,
        sampling_init_from_input=False,
        input_init_noise_scale=0.0,
        sigma_max=None,
        noise_scale=None,
        gamma_0=0.0,
        gamma_min=None,
    )
    kwargs.update(overrides)
    return load_score_model(**kwargs)


def _use_msa() -> bool:
    """MSA is mandatory for the D-peptide oracle (user contract). The env knob
    only exists for the crystal-validation script to assert the negative path;
    production keeps it on."""
    import os

    return os.environ.get("DPEPTIDE_USE_MSA", "1").strip().lower() in {"1", "true", "yes", "on"}


def _assert_msa_fetched(work_dir: Path) -> None:
    """Fail fast unless every protein chain carries an MSA source.

    Ground truth is the featurized record: each protein chain must reference a
    non-sentinel msa_id whose npz exists under processed/msa (receptor MSAs may
    legitimately live in the shared sequence cache instead of the task dir, and
    a designed peptide chain may legitimately be query-only — the record view
    covers both without false positives).
    """
    import json

    records = sorted((work_dir / "processed" / "records").glob("*.json"))
    if not records:
        raise RuntimeError(
            "D-peptide oracle requires MSA (mandatory): no featurized record "
            f"found under {work_dir}."
        )
    msa_dir = work_dir / "processed" / "msa"
    missing: list[str] = []
    for record_path in records:
        record = json.loads(record_path.read_text())
        protein_chains = [c for c in record.get("chains", []) if c.get("mol_type") == 0]
        if not protein_chains:
            continue
        # the RECEPTOR (largest protein chain) must carry an MSA source; a
        # designed peptide chain may legitimately be query-only (the L-flow
        # likewise ships a _disabled.a3m for binder chains)
        receptor_chain = max(
            protein_chains, key=lambda c: int(c.get("num_residues") or 0))
        msa_id = receptor_chain.get("msa_id")
        name = str(receptor_chain.get("chain_name") or receptor_chain.get("chain_id"))
        if not msa_id or msa_id == "-1":
            missing.append(name)
            continue
        if not (msa_dir / f"{msa_id}.npz").exists():
            missing.append(f"{name} (missing {msa_id}.npz)")
    if missing:
        raise RuntimeError(
            "D-peptide oracle requires MSA (mandatory): protein chain(s) without "
            f"an MSA source: {missing}. Check MSA_SERVER_URL connectivity from "
            "the dock container."
        )


def _prepare_and_run(
    input_pdb: Path,
    work_root: Path,
    model_module,
    mode: str,
    seed: int,
    *,
    receptor_chains: tuple[str, ...] = ("A",),
    peptide_chains: tuple[str, ...] = ("B",),
    pocket_box: float = 6.0,
    anchor_max_residues: int = 30,
    run_scoring_kwargs: dict | None = None,
) -> Path:
    import shutil
    import sys

    _ensure_sys_path()
    from core.prepare_inputs import prepare_inputs
    from core.results import write_chain_map
    from core.inference import run_scoring

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # peplm root
    from peplm.dpeptide.manifest import (
        configure_peptide_pocket_constraints,
        filter_templates_to_receptor,
    )

    tag = input_pdb.stem
    output_dir = work_root / mode / tag
    work_dir = work_root / "work" / f"{mode}_{tag}"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    input_dir = work_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    staged = input_dir / input_pdb.name
    shutil.copy2(input_pdb, staged)

    use_msa = _use_msa()
    prepare_inputs(
        input_dir=input_dir,
        out_dir=work_dir,
        cache_dir=_boltz_cache(),
        recursive=False,
        use_msa_server=use_msa,
        # msa_server_url deliberately omitted: prepare_inputs defaults to the
        # MSA_SERVER_URL env, which the dock container injects.
        self_template=(mode == "fixed"),
        self_template_threshold=2.0,
    )
    if use_msa:
        _assert_msa_fetched(work_dir)
    processed = work_dir / "processed"
    if mode == "fixed":
        filter_templates_to_receptor(processed, tag, list(receptor_chains))
        configure_peptide_pocket_constraints(
            processed, tag,
            ligand_chain_letter=peptide_chains[0],
            target_chain_letters=list(receptor_chains),
            contact_cutoff=8.0,
            max_distance=6.0,
            max_residues=anchor_max_residues,
        )
    run_scoring(
        processed_dir=processed,
        output_dir=output_dir,
        cache_dir=Path("/data/boltz_cache"),
        checkpoint=None,
        devices=1,
        accelerator="gpu",
        num_workers=0,
        output_format="mmcif",
        recycling_steps=3,
        sampling_steps=100,
        diffusion_samples=2,
        max_parallel_samples=2,
        structure_refine=(mode == "fixed"),
        write_full_pae=True,
        step_scale=1.5,
        no_kernels=False,
        contact_guidance=(mode == "fixed"),
        use_potentials=False,
        reference_from_input=False,
        sampling_init_from_input=(mode == "fixed"),
        input_init_noise_scale=0.0,
        sigma_max=None,
        noise_scale=None,
        gamma_0=0.0,
        gamma_min=None,
        seed=seed,
        trainer_precision=None,
        model_module=model_module,
    )
    write_chain_map(processed_dir=processed, output_dir=output_dir, record_id=tag)
    return output_dir


def score_complex(
    input_pdb: Path,
    work_root: Path,
    model_module=None,
    seed: int = 7,
) -> dict:
    """Validate an existing pose (score mode, no diffusion, no damage)."""
    if model_module is None:
        model_module = load_model_once(structure_refine=False, sampling_steps=1, diffusion_samples=1)
    out = _prepare_and_run(input_pdb, work_root, model_module, "score", seed)
    return _parse_confidence(out, input_pdb.stem)


def dock_peptide(
    input_pdb: Path,
    work_root: Path,
    model_module=None,
    seed: int = 7,
    pocket_box: float = 6.0,
) -> dict:
    """Fixed-D-target pocket docking (E5 protocol)."""
    set_fixed_receptor_config(
        enabled=True, peptide_init="input",
        pocket_box_radius=float(pocket_box), debug=False,
    )
    if model_module is None:
        model_module = load_model_once()
    install_fixed_receptor_sampler(model_module)
    out = _prepare_and_run(input_pdb, work_root, model_module, "fixed", seed,
                           pocket_box=pocket_box)
    return _parse_confidence(out, input_pdb.stem)


def _parse_confidence(output_dir: Path, tag: str) -> dict:
    inner = output_dir / tag
    conf_files = sorted(inner.glob(f"confidence_{tag}_model_*.json"))
    if not conf_files:
        raise RuntimeError(f"no confidence outputs under {inner}")
    best, best_key = None, -1.0
    for path in conf_files:
        payload = json.loads(path.read_text())
        key = float(payload.get("iptm") or 0.0)
        if key > best_key:
            best, best_key = payload, key
    if best is None:
        raise RuntimeError(f"all confidence payloads empty under {inner}")
    best["structure_dir"] = str(inner)
    return best
