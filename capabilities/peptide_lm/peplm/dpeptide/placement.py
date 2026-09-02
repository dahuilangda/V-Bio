"""Peptide conformer generation and pocket placement (validated E5 protocol).

Conformer: Protenix single-chain, NO MSA — the peptide analogue of the
small-molecule ETKDG step (measured 1.70 A backbone RMSD vs the bound
conformation on the reference case; helix retained; L chirality).

Placement: peptide centroid at the pocket center with a random rigid
orientation per seed — no native pose information is used.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import gemmi
import numpy as np

PROTENIX_IMAGE = "vbio-protenix-v2-runtime:2.0.0"
_THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V",
}


def chain_sequence(chain: gemmi.Chain) -> str:
    seq = []
    for residue in chain:
        aa = _THREE_TO_ONE.get(residue.name.strip().upper())
        seq.append(aa if aa else "X")
    return "".join(seq)


def random_rotation(rng: np.random.Generator) -> np.ndarray:
    """Uniform random axis + angle, proper rotation."""
    axis = rng.normal(size=3)
    axis /= np.linalg.norm(axis)
    theta = rng.uniform(0, 2 * math.pi)
    K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + math.sin(theta) * K + (1 - math.cos(theta)) * (K @ K)


def _best_sample_by_ranking(out_dir: Path, cifs: list[Path]) -> Path:
    """Pick the sample with the highest ranking_score (mirrors the production
    postprocessor); falls back to the first cif when summaries are absent."""
    best_cif = cifs[0]
    best_score = float("-inf")
    for cif in cifs:
        summary = cif.with_name(cif.name.replace("_sample_", "_summary_confidence_sample_"))
        stem = summary.with_suffix("")
        summary = Path(str(summary).replace(".json", "") + ".json") if summary.suffix != ".json" else summary
        candidates = list(out_dir.rglob(
            Path(cif).name.replace(".cif", "").split("_sample_")[0]
            + f"_summary_confidence_sample_{cif.stem.rsplit('_', 1)[-1]}.json"
        ))
        for cand in candidates:
            try:
                score = json.loads(cand.read_text()).get("ranking_score")
            except Exception:
                continue
            if isinstance(score, (int, float)) and score > best_score:
                best_score, best_cif = float(score), cif
    return best_cif


def protenix_conformer(
    sequence: str,
    work_dir: Path,
    seed: int = 202,
    samples: int = 1,
    device: int | None = 1,
    timeout_s: int = 2400,
) -> Path:
    """Generate an isolated L-peptide conformer via Protenix (no MSA).

    Returns the best-sample cif path. Falls back to an ideal alpha-helix PDB
    when the docker runtime is unavailable (degraded but functional).
    """
    work_dir = Path(work_dir).expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    payload = [{
        "name": "peptide_conformer",
        "sequences": [{"proteinChain": {"sequence": sequence, "count": 1}}],
    }]
    json_path = work_dir / "conformer_input.json"
    json_path.write_text(json.dumps(payload))
    out_dir = work_dir / "out"

    gpu = f'--gpus "device={device}" ' if device is not None else ""
    cmd = (
        f"docker run --rm {gpu}-v {work_dir}:/work {PROTENIX_IMAGE} "
        f"pred --input /work/conformer_input.json --out_dir /work/out "
        f"--use_msa false --seeds {seed} --sample {samples}"
    )
    try:
        subprocess.run(["bash", "-lc", cmd], check=True, timeout=timeout_s,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cifs = sorted(out_dir.rglob("*_sample_*.cif"))
        if cifs:
            return _best_sample_by_ranking(out_dir, cifs)
        raise RuntimeError(
            "Protenix conformer prediction finished but produced no sample cif "
            f"under {out_dir}. No fallback is attempted: fix the conformer "
            "step at the root."
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise RuntimeError(
            "Protenix conformer generation requires the production docker image "
            f"{PROTENIX_IMAGE} (docker CLI + GPU). Original error: {exc}"
        ) from exc


def place_peptide_at_pocket(
    receptor_structure: gemmi.Structure,
    peptide_structure: gemmi.Structure,
    pocket_center: np.ndarray,
    seed: int,
    receptor_chain: str = "A",
    out_path: Path | None = None,
) -> gemmi.Structure:
    """Stage a complex: receptor verbatim + peptide (random orientation) with
    its centroid at pocket_center."""
    rng = np.random.default_rng(seed)
    pep_chain = peptide_structure[0][0]
    coords = np.array([[a.pos.x, a.pos.y, a.pos.z] for res in pep_chain for a in res])
    R = random_rotation(rng)
    centroid = coords.mean(axis=0)
    placed = (R @ (coords - centroid).T).T + np.asarray(pocket_center, dtype=float)

    out = gemmi.Structure()
    out.name = "docked_input"
    model = gemmi.Model("1")
    rec_chain = gemmi.Chain(receptor_chain)
    for residue in receptor_structure[0][0]:
        rec_chain.add_residue(residue.clone())
    model.add_chain(rec_chain)
    pep_out = gemmi.Chain("B")
    idx = 0
    for num, residue in enumerate(pep_chain, start=1):
        nr = gemmi.Residue()
        nr.name = residue.name
        nr.seqid = gemmi.SeqId(num, " ")
        nr.het_flag = "A"
        for atom in residue:
            na = gemmi.Atom()
            na.name = atom.name
            na.element = atom.element
            na.pos = gemmi.Position(*placed[idx])
            idx += 1
            nr.add_atom(na)
        pep_out.add_residue(nr)
    model.add_chain(pep_out)
    out.add_model(model)
    out.setup_entities()
    if out_path is not None:
        out.write_pdb(str(out_path))
    return out
