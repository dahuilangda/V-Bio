"""Protenix oracle for the ESM3 design loop.

One docker run per candidate against the production protenix2dock peptide
mode (blind peptide, pocket-guided); the staging template supplies the
receptor, pocket numbering and peptide chain. Candidates shorter than the
template's peptide chain TRIM the chain rather than pad it with glycine —
padding would move the cyclisation bond onto a residue the policy never
chose."""
from __future__ import annotations

import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from peplm.residues import AA3_OF as ONE2THREE
from peplm.score.interface import InterfaceMetrics, compute_interface_metrics

IMAGE = "vbio-protenix-v2-runtime:2.0.0"
# The vendored fork is mounted read-only at /workspace/vbio; PYTHONPATH
# must reference the CONTAINER path — a host path is silently dropped and
# the stock protenix runs instead (no receptor pinning, no blind-peptide
# init, no TFG bonds, no pocket guidance).
PROTENIX_ROOT_IN_CONTAINER = "/workspace/vbio/vendor/protenix-source"
MODEL_DIR = "/data/protenix/model"
MSA_CACHE_HOST = "/data/boltz_msa_cache"


@dataclass
class OracleConfig:
    template: Path
    pocket: str
    peptide_chain: str = "B"
    cyclic: bool = True
    timeout_s: int = 1800          # >= the peptide env-MSA budget
    image: str = IMAGE
    model_dir: str = MODEL_DIR
    msa_server_url: str | None = None
    msa_cache_host: str = MSA_CACHE_HOST
    # rediffuse refill: partial diffusion around the parent pose
    # (RFdiffusion partial_T analog in protenix2dock's modes.py)
    refill_sigma_max: float = 0.75
    refill_steps: int = 100
    diffusion_samples: int = 8


@dataclass
class OracleResult:
    sequence: str
    metrics: InterfaceMetrics | None
    min_clash_dist: float = 99.0
    pairs_deep: int = 0
    nonlocal_contacts: int = 0
    caca_min: float | None = None
    chir_mixed: int = 0
    omega_bad: int = 0
    cif: str | None = None
    error: str | None = None


def stage_complex(template: Path, sequence: str, out_pdb: Path,
                  peptide_chain: str = "B") -> int:
    """Write the template with the peptide chain set to ``sequence``.
    Returns the staged peptide length. Receptor numbering is preserved
    (the pocket selection refers to it), so ``setup_entities`` is never
    called. Only polymer residues count as peptide slots — waters riding
    on the chain are dropped (a candidate residue landing on a water
    would put the cyclisation bond on a residue with no N/C)."""
    import gemmi

    st = gemmi.read_structure(str(template))
    model = st[0]
    pep = None
    for ch in model:
        if ch.name == peptide_chain:
            pep = ch
            break
    if pep is None:
        raise ValueError(
            f"peptide chain {peptide_chain!r} absent from template {template}")
    for i in range(len(pep) - 1, -1, -1):
        # D-amino-acid residues carry het_flag='H' but ARE the peptide;
        # only drop empty (water/truncated) residues
        if pep[i].het_flag == "H" and len(pep[i]) == 0:
            del pep[i]
    if len(sequence) > len(pep):
        raise ValueError(
            f"sequence length {len(sequence)} exceeds the template's "
            f"{len(pep)} peptide slots; stage a longer template")
    for i in range(len(pep) - 1, len(sequence) - 1, -1):
        del pep[i]
    for i, res in enumerate(pep):
        res.name = ONE2THREE[sequence[i]]
    out_pdb.parent.mkdir(parents=True, exist_ok=True)
    st.write_pdb(str(out_pdb))
    return len(pep)


def _audit_geometry(cif: Path, peptide_chain: str) -> dict:
    """Interchain clash and peptide-chain shape from the predicted CIF."""
    import gemmi

    st = gemmi.read_structure(str(cif))
    st.remove_hydrogens()
    model = st[0]
    pep_ch = next((c for c in model if c.name == peptide_chain), None)
    rec_chains = [c for c in model if c.name != peptide_chain]
    if pep_ch is None or not rec_chains:
        return {"min_clash_dist": 99.0, "pairs_deep": 0,
                "nonlocal_contacts": 0, "caca_min": None,
                "chir_mixed": 0, "omega_bad": 0}
    pep = np.array([[a.pos.x, a.pos.y, a.pos.z] for r in pep_ch for a in r])
    rec = np.array([[a.pos.x, a.pos.y, a.pos.z]
                    for c in rec_chains for r in c for a in r])
    if pep.size == 0 or rec.size == 0:
        return {"min_clash_dist": 99.0, "pairs_deep": 0,
                "nonlocal_contacts": 0, "caca_min": None,
                "chir_mixed": 0, "omega_bad": 0}
    d = np.linalg.norm(rec[:, None, :] - pep[None, :, :], axis=-1)
    ca = np.array([[a.pos.x, a.pos.y, a.pos.z] for r in pep_ch
                   for a in r if a.name == "CA"])
    nonlocal_contacts = 0
    if len(ca) >= 4:
        dca = np.linalg.norm(ca[:, None, :] - ca[None, :, :], axis=-1)
        iu = np.triu_indices(len(ca), 1)
        nonlocal_contacts = int(
            ((np.abs(iu[0] - iu[1]) >= 3) & (dca[iu] < 8.0)).sum())
    # backbone integrity: consecutive CA-CA distance (ideal 3.8 A; a
    # crushed backbone < 3.2 is collapse regardless of fold confidence)
    caca_min = (float(np.linalg.norm(np.diff(ca, axis=0), axis=1).min())
                if len(ca) >= 2 else None)
    # stereochemistry: uniform CA chirality sign (L peptide -> all +) and
    # trans peptide bonds; the loop must never train on a flattened centre
    chir_mixed = 0
    if len(ca) >= 1:
        signs = []
        for r in pep_ch:
            at = {a.name: np.array([a.pos.x, a.pos.y, a.pos.z]) for a in r}
            if all(k in at for k in ("N", "CA", "C", "CB")):
                v = np.stack([at["N"] - at["CA"], at["C"] - at["CA"],
                              at["CB"] - at["CA"]])
                signs.append(float(np.linalg.det(v)))
        if signs:
            signs = np.array(signs)
            chir_mixed = int(((signs > 0) != (signs[0] > 0)).sum())
    omega_bad = 0
    def _dih(p0, p1, p2, p3):
        b0, b1v, b2v = p0 - p1, p2 - p1, p3 - p2
        b1u = b1v / np.linalg.norm(b1v)
        v = b0 - np.dot(b0, b1u) * b1u
        w = b2v - np.dot(b2v, b1u) * b1u
        return np.degrees(np.arctan2(np.dot(np.cross(b1u, v), w),
                                     np.dot(v, w)))
    res_atoms = [{a.name: np.array([a.pos.x, a.pos.y, a.pos.z]) for a in r}
                 for r in pep_ch]
    for i in range(len(res_atoms) - 1):
        a, b = res_atoms[i], res_atoms[i + 1]
        try:
            phi = _dih(a["CA"], a["C"], b["N"], b["CA"])
        except KeyError:
            continue
        if abs(abs(phi) - 180.0) > 60.0:
            omega_bad += 1
    # deep-interpenetration pairs (< 2.3 A, below the crystal bound
    # peptide's own 2.52 A floor)
    return {"min_clash_dist": float(d.min()),
            "pairs_deep": int((d < 2.3).sum()),
            "nonlocal_contacts": nonlocal_contacts,
            "caca_min": caca_min,
            "chir_mixed": chir_mixed,
            "omega_bad": omega_bad}


def stage_from_parent(parent_cif: Path, sequence: str, out_pdb: Path,
                      peptide_chain: str = "B") -> int:
    """Stage a complex from the parent's docked pose: the peptide keeps
    its docked conformation (not the GLY template) so the rediffuse
    partial-diffusion perturbs around known-good geometry."""
    import gemmi
    st = gemmi.read_structure(str(parent_cif))
    st.setup_entities()
    st.remove_hydrogens()
    pep = next((c for c in st[0] if c.name == peptide_chain), None)
    if pep is None:
        raise ValueError(f"peptide chain {peptide_chain!r} not in {parent_cif}")
    out_pdb.parent.mkdir(parents=True, exist_ok=True)
    st.write_pdb(str(out_pdb))   # gemmi auto-detects CIF input, writes PDB
    return len(pep)


def refill_candidate(sequence: str, parent_cif: Path, cfg: OracleConfig,
                     run_dir: Path, gpu: int, seed: int) -> OracleResult:
    """Partial-diffusion refill around a parent pose: sigma re-noises the
    peptide (~12 A, fold and site stay recognizable), then 100 denoise
    steps search locally around that geometry — half the compute of a
    blind restart."""
    run_dir.mkdir(parents=True, exist_ok=True)
    staged = run_dir / "staged.pdb"
    uid = f"{run_dir.name}-{uuid.uuid4().hex[:8]}"
    try:
        pep_len = stage_from_parent(parent_cif, sequence, staged,
                                    cfg.peptide_chain)
        mount = f"/data/boltz_central_results/_runtime_tmp/{uid}"
        bond = (f"{cfg.peptide_chain}:1:N,{cfg.peptide_chain}:{pep_len}:C"
                if cfg.cyclic else "")
        cmd = [
            "docker", "run", "--rm", "--name", f"esm3loop-{uid}",
            "--runtime", "nvidia", "--shm-size", "16g",
            "--gpus", f"device={gpu}",
            "-v", "/data/V-Bio:/workspace/vbio:ro",
            "-v", f"{run_dir}:{mount}",
            "-v", f"{cfg.msa_cache_host}:/cache/msa",
            "-w", "/workspace/vbio",
            "-e", f"PYTHONPATH={PROTENIX_ROOT_IN_CONTAINER}",
            "-e", "PROTENIX_ROOT_DIR=/cache",
            "-v", f"{cfg.model_dir}:/workspace/model:ro",
            "-v", "/data/protenix/common_cache:/cache/common:ro",
            "-v", "/data/protenix/module_cache:/cache/module_cache",
            "-e", "PROTENIX_MODULE_CACHE_DIR=/cache/module_cache",
            # RePaint conditioned diffusion (R=1): the receptor is pinned
            # and re-noised correctly at each step, the peptide gets a
            # resample pass to become coherent with it. Raw pinning
            # chirality-flips / collapses / ring-warps; R=1 does not.
            "--entrypoint", "", cfg.image,
            "/usr/local/micromamba/envs/protenix/bin/python",
            "/workspace/vbio/capabilities/protenix2dock/protenix2dock.py",
            "--mode", "rediffuse",
            "--sigma_max", str(cfg.refill_sigma_max),
            "--sampling_steps", str(cfg.refill_steps),
            "--diffusion_samples", str(cfg.diffusion_samples),
            "--output_dir", f"{mount}/out",
            "--work_dir", f"{mount}/work",
            "--msa_cache_dir", "/cache/msa",
            "--seed", str(seed),
            "--input", f"{mount}/staged.pdb",
            "--peptide_chain", cfg.peptide_chain,
            *(["--bond_pairs", bond] if bond else []),
            "--bond_upper", "2.2",
            "--pocket_upper", "14.0",
            "--pocket_res", cfg.pocket,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=cfg.timeout_s, check=False)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", f"esm3loop-{uid}"],
                           capture_output=True, check=False)
            return OracleResult(sequence, None, error="timeout")
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
            return OracleResult(sequence, None,
                                error=f"docker rc={proc.returncode}: {tail[0]}")
        pred = (run_dir / "out" / "protenix2dock_peptide" / f"seed_{seed}"
                / "predictions")
        if not pred.exists():
            # rediffuse mode writes under a different prefix
            pred = (run_dir / "out" / "protenix2dock_rediffuse" / f"seed_{seed}"
                    / "predictions")
        if not pred.exists():
            # scan for any predictions dir
            cands = list(run_dir.rglob("predictions"))
            pred = cands[0] if cands else pred
        if not pred.exists() or not list(pred.glob("*_sample_*.cif")):
            return OracleResult(sequence, None,
                                error="no predictions directory")
        return _best_sample(sequence, pred, pep_len, cfg.peptide_chain)
    except Exception as exc:   # noqa: BLE001 - isolation boundary
        return OracleResult(sequence, None,
                            error=f"{type(exc).__name__}: {exc}")


def dock_candidate(sequence: str, cfg: OracleConfig, run_dir: Path,
                   gpu: int, seed: int) -> OracleResult:
    """Dock one candidate; returns metrics or an error-tagged result.

    Any per-candidate failure (bad staging, docker error, unparsable
    output) is converted to an error result so one bad candidate can
    never abort the round.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    staged = run_dir / "staged.pdb"
    # unique per dock: a reused docker name collides with a container
    # still running from a previous timeout (--rm only frees names of
    # EXITED containers)
    uid = f"{run_dir.name}-{uuid.uuid4().hex[:8]}"
    try:
        pep_len = stage_complex(cfg.template, sequence, staged,
                                cfg.peptide_chain)
        mount = f"/data/boltz_central_results/_runtime_tmp/{uid}"
        bond = (f"{cfg.peptide_chain}:1:N,{cfg.peptide_chain}:{pep_len}:C"
                if cfg.cyclic else "")
        cmd = [
            "docker", "run", "--rm", "--name", f"esm3loop-{uid}",
            "--runtime", "nvidia", "--shm-size", "16g",
            "--gpus", f"device={gpu}",
            "-v", "/data/V-Bio:/workspace/vbio:ro",
            "-v", f"{run_dir}:{mount}",
            "-v", f"{cfg.msa_cache_host}:/cache/msa",
            "-w", "/workspace/vbio",
            "-e", f"PYTHONPATH={PROTENIX_ROOT_IN_CONTAINER}",
            "-e", "PROTENIX_ROOT_DIR=/cache",
            "-v", f"{cfg.model_dir}:/workspace/model:ro",
            "-v", "/data/protenix/common_cache:/cache/common:ro",
            "-v", "/data/protenix/module_cache:/cache/module_cache",
            "-e", "PROTENIX_MODULE_CACHE_DIR=/cache/module_cache",
            # RePaint conditioned diffusion (R=1): the receptor is pinned
            # and re-noised correctly at each step, the peptide gets a
            # resample pass to become coherent with it. Raw pinning
            # chirality-flips / collapses / ring-warps; R=1 does not.
            "--entrypoint", "", cfg.image,
            "/usr/local/micromamba/envs/protenix/bin/python",
            "/workspace/vbio/capabilities/protenix2dock/protenix2dock.py",
            "--mode", "peptide",
            "--output_dir", f"{mount}/out",
            "--work_dir", f"{mount}/work",
            "--msa_cache_dir", "/cache/msa",
            *(["--msa_server_url", cfg.msa_server_url]
              if cfg.msa_server_url else []),
            "--seed", str(seed),
            "--input", f"{mount}/staged.pdb",
            "--peptide_chain", cfg.peptide_chain,
            *(["--bond_pairs", bond] if bond else []),
            "--bond_upper", "2.2",
            # peptide mode reads this as the ANCHOR-BOX radius: each binder
            # atom within R of the solvent-side anchor point (pocket
            # centroid + outward*8)
            "--pocket_upper", "14.0",
            "--pocket_res", cfg.pocket,
            "--diffusion_samples", str(cfg.diffusion_samples),
            "--blind_peptide",
            # no --interface_chains: protenix2dock derives the peptide
            # entity letter itself
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=cfg.timeout_s, check=False)
        except subprocess.TimeoutExpired:
            # the container outlives the killed CLI; free its name AND gpu
            subprocess.run(["docker", "rm", "-f", f"esm3loop-{uid}"],
                           capture_output=True, check=False)
            return OracleResult(sequence, None, error="timeout")
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
            return OracleResult(sequence, None,
                                error=f"docker rc={proc.returncode}: {tail[0]}")

        pred = (run_dir / "out" / "protenix2dock_peptide" / f"seed_{seed}"
                / "predictions")
        if not pred.exists():
            return OracleResult(sequence, None,
                                error="no predictions directory")
        return _best_sample(sequence, pred, pep_len, cfg.peptide_chain)
    except Exception as exc:   # noqa: BLE001 - isolation boundary
        return OracleResult(sequence, None,
                            error=f"{type(exc).__name__}: {exc}")


def _best_sample(sequence: str, pred: Path, pep_len: int,
                 peptide_chain: str) -> OracleResult:
    """Pick the sample the loop should train on: clash-clean samples
    first, most confident interface (lowest mean interface PAE) among
    them. Pure-PAE selection is a reward-hacking vector — a fold gains
    interface confidence by burying the peptide INTO the receptor, so
    PAE alone prefers the deepest overlap."""
    # tier floors against the bound-peptide envelope: heavy-atom contacts
    # at 2.5-2.7 A are physical, a crushed backbone marks collapse
    CLASH_FLOOR = 2.3
    CACA_FLOOR = 3.2
    OMEGA_BAD_MAX = 0
    best: OracleResult | None = None
    best_key = None
    for full in sorted(pred.glob("*full_data_sample_*.json")):
        idx = full.stem.rsplit("_sample_", 1)[1]
        cif = next(pred.glob(f"*_sample_{idx}.cif"), None)
        if cif is None:
            continue
        m = compute_interface_metrics(full, peptide_len=pep_len)
        if m is None:
            continue
        geo = _audit_geometry(cif, peptide_chain)
        intact = (geo["caca_min"] or 0.0) >= CACA_FLOOR
        clean = geo["min_clash_dist"] >= CLASH_FLOOR
        stereo = (geo.get("chir_mixed", 0) == 0
                  and geo.get("omega_bad", 0) <= OMEGA_BAD_MAX)
        key = (0 if stereo and intact and clean else 1 if intact else 2,
               m.mean_interface_pae)
        if best_key is None or key < best_key:
            best_key = key
            best = OracleResult(sequence, m, cif=str(cif), **geo)
    if best is None:
        return OracleResult(sequence, None, error="no parsable sample")
    return best


def dock_batch(sequences: list[str], cfg: OracleConfig, run_root: Path,
               gpus: list[int], seed: int,
               parents: list[Path | None] | None = None,
               log=print) -> list[OracleResult]:
    """Dock a round's candidates, one docker run per GPU at a time."""
    run_root.mkdir(parents=True, exist_ok=True)

    def _one(entry):
        i, seq, parent = entry
        gpu = gpus[i % len(gpus)]
        run_dir = run_root / f"c{i:02d}"
        if parent is not None and parent.exists():
            res = refill_candidate(seq, parent, cfg, run_dir, gpu, seed + i)
        else:
            res = dock_candidate(seq, cfg, run_dir, gpu, seed + i)
        if res.error:
            # one retry with a fresh run dir: shared-GPU CUDA OOM is
            # usually transient
            res = dock_candidate(seq, cfg, run_root / f"c{i:02d}r", gpu,
                                 seed + i + 5000)
        if res.error:
            log(f"[oracle] c{i:02d} {seq}: {res.error}")
        else:
            m = res.metrics
            log(f"[oracle] c{i:02d} {seq}: ipSAE={m.ipsae:.4f} "
                f"iPAE={m.mean_interface_pae:.2f} pLDDT={m.binder_plddt:.1f} "
                f"clash={res.min_clash_dist:.2f}")
        return i, res

    out: list[OracleResult | None] = [None] * len(sequences)
    entries = [(i, seq, (parents[i] if parents else None))
               for i, seq in enumerate(sequences)]
    with ThreadPoolExecutor(max_workers=len(gpus)) as pool:
        for i, res in pool.map(_one, entries):
            out[i] = res
    return [r for r in out if r is not None]
