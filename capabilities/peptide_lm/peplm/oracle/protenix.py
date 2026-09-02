"""Protenix oracle: V-Bio production docker invocation, PeptideLM interface.

Reuses the production adapter (backend.runtime.protenix_adapter.parse_yaml_for_
protenix) so the input contract (modifications / cyclic / bond constraints /
CCD ligands) is identical to the platform's Protenix path, then one docker
container per candidate (parallel across GPUs). Output parsing mirrors the
production postprocessor: best sample by ranking_score from
{stem}_summary_confidence_sample_*.json plus atom-level data.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from peplm.candidate import Candidate
from peplm.oracle.peptide_boltz import build_complex_yaml

IMAGE = "vbio-protenix-v2-runtime:2.0.0"  # production docker image
MODEL_DIR = "/data/protenix/model"
MODEL_NAME = "protenix-v2"
SOURCE_DIR = "/data/V-Bio/vendor/protenix-source"
COMMON_CACHE = "/data/protenix/common_cache"


def _parse_output(out_dir: Path, binder_residues: int,
                  use_ipsae: bool = True) -> dict:
    """Best sample by ranking_score. Chain metrics come from the summary
    (chains in input order: target=0, binder=1); per-residue binder pLDDT
    and token-level ipSAE from the full-data JSON (atom->token mapping)."""
    import numpy as np

    summaries = sorted(out_dir.rglob("*_summary_confidence_sample_*.json"))
    if not summaries:
        return {}
    scored = []
    for p in summaries:
        payload = json.loads(p.read_text())
        rs = payload.get("ranking_score")
        if rs is not None:
            scored.append((float(rs), p))
    if not scored:
        return {}
    scored.sort(key=lambda x: (-x[0], len(str(x[1]))))
    summary_path = scored[0][1]
    m = re.search(r"_summary_confidence_sample_(\d+)\.json$", summary_path.name)
    idx = m.group(1)
    stem = summary_path.name[: summary_path.name.rfind(
        f"_summary_confidence_sample_{idx}.json")]
    parent = summary_path.parent
    summary = json.loads(summary_path.read_text())
    out: dict = {
        "iptm": summary.get("iptm"),
        "ptm": summary.get("ptm"),
        "ranking_score": summary.get("ranking_score"),
        "confidence_score": summary.get("ranking_score"),
        "complex_plddt": (summary.get("plddt") / 100.0
                          if isinstance(summary.get("plddt"), (int, float))
                          else None),
    }
    try:
        out["pair_iptm"] = float(summary["chain_pair_iptm"][0][1])
    except (TypeError, ValueError, KeyError, IndexError):
        out["pair_iptm"] = summary.get("iptm")
    try:
        out["binder_avg_plddt"] = float(summary["chain_plddt"][1]) * 100.0
    except (TypeError, ValueError, KeyError, IndexError):
        pass
    fd_path = parent / f"{stem}_full_data_sample_{idx}.json"
    if use_ipsae and fd_path.exists():
        fd = json.loads(fd_path.read_text())
        asym = np.asarray(fd.get("token_asym_id") or [])
        a2t = np.asarray(fd.get("atom_to_token_idx") or [])
        aplddt = np.asarray(fd.get("atom_plddt") or [])
        pae = fd.get("token_pair_pae")
        if len(asym) and len(a2t) == len(aplddt) and isinstance(pae, list) \
                and len(pae) == len(asym):
            groups: dict[int, list[int]] = {}
            for t_i, a in enumerate(asym):
                groups.setdefault(int(a), []).append(t_i)
            # binder group: token count matches residue count; target =
            # largest group
            binder_asym = next((a for a, ix in groups.items()
                                if len(ix) == binder_residues), None)
            if binder_asym is None:
                raise ValueError(
                    "no token group matches the binder length "
                    f"({binder_residues}); groups: "
                    + ", ".join(f"{a}={len(ix)}" for a, ix in sorted(groups.items())))
            target_asym = max(groups, key=lambda a: len(groups[a]))
            # per-residue pLDDT (protein chains: one token per residue)
            if binder_asym is not None:
                tok_scores = {}
                for atom_i, t_i in enumerate(a2t):
                    tok_scores.setdefault(int(t_i), []).append(
                        float(aplddt[atom_i]))
                b_plddt = [100.0 * np.mean(tok_scores[t])
                           for t in groups[binder_asym]
                           if t in tok_scores]
                if b_plddt:
                    out["binder_plddt"] = b_plddt
                    out["binder_avg_plddt"] = float(np.mean(b_plddt))
            # geometry-based ipSAE (same formulation as the boltz path:
            # CA distances <= 10 A interfaced with PAE < 12); falls back
            # to PAE-only when the structure is unavailable
            if binder_asym is not None and binder_asym != target_asym:
                pae_m = np.asarray(pae, dtype=float)
                ti = np.asarray(groups[target_asym])
                bi = np.asarray(groups[binder_asym])
                cif_path = next((p for p in [
                    parent / f"{stem}_sample_{idx}.cif",
                    parent / f"{stem}_sample_{idx}.mmcif"] if p.exists()),
                    None)
                coords = None
                if cif_path is not None:
                    from peplm.oracle.chain_ipsae import parse_cif_ca

                    ca = parse_cif_ca(cif_path)
                    sized = {}
                    for c, xyz in ca:
                        sized.setdefault(c, []).append(xyz)
                    by_size = {len(xs): xs for xs in sized.values()}
                    # Only protein chains align (the linker ligand's
                    # tokens carry no CA block); target + binder required.
                    matched = [(g, by_size[len(groups[g])])
                               for g in groups
                               if len(groups[g]) in by_size]
                    matched = [(g, xs) for g, xs in matched if xs is not None]
                    if len(matched) >= 2:
                        matched.sort(key=lambda gm: min(groups[gm[0]]))
                        ordered = [p2 for _, blk in matched for p2 in blk]
                        n = max(int(ti.max()), int(bi.max())) + 1
                        if len(ordered) >= n and len(ordered) <= 2 * n:
                            coords = np.stack(ordered[:n])
                if coords is not None:
                    d = np.sqrt(((coords[ti][:, None] - coords[bi][None]) ** 2).sum(-1))
                    pae_tb = pae_m[np.ix_(ti, bi)]
                    valid = (d <= 10.0) & (pae_tb < 12.0)
                else:
                    # no alignable structure: PAE-only interface evidence
                    pae_tb = pae_m[np.ix_(ti, bi)]
                    valid = pae_tb < 12.0
                from peplm.oracle.chain_ipsae import _d0, _ptm

                if valid.any():
                    n0 = int(valid.any(1).sum() + valid.any(0).sum())
                    out["ipsae_dom"] = float(
                        _ptm(pae_tb[valid], _d0(n0)).mean())
                    best = max(
                        (float(_ptm(pae_tb[valid[:, j], j],
                                        _d0(int(valid[:, j].sum()))).mean())
                         for j in range(valid.shape[1]) if valid[:, j].any()),
                        default=0.0)
                    out["ligand_ipsae_max"] = best
                else:
                    out["ipsae_dom"] = 0.0
    return out


def _cif_chains(cif_path: Path) -> list[str]:
    import gemmi

    st = gemmi.read_structure(str(cif_path))
    chains = []
    for model in st:
        for chain in model:
            if chain.name not in chains:
                chains.append(chain.name)
    return chains


class ProtenixOracle:
    def __init__(self, target_sequence: str, work_dir, gpus=(0, 1, 2, 3),
                 bicyclic: dict | None = None, seed: int = 42,
                 timeout_s: int = 3600, log=print):
        self.target_sequence = str(target_sequence).upper()
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.gpus = list(gpus) or [0]
        self.bicyclic = bicyclic
        self.seed = seed
        self.timeout_s = timeout_s
        self.log = log
        self.n_calls = 0
        self.wall_s = 0.0
        sys_path_backup = "/data/V-Bio"
        import sys

        sys.path.insert(0, sys_path_backup)
        try:
            from backend.runtime.protenix_adapter import parse_yaml_for_protenix

            self._parse_yaml = parse_yaml_for_protenix
        finally:
            sys.path.remove(sys_path_backup)

    def score(self, candidates: list[Candidate], tag: str = "b") -> list[Candidate]:
        if not candidates:
            return candidates
        self.n_calls += len(candidates)
        t0 = time.time()
        base = self.work_dir / "oracle" / tag
        indexed = list(enumerate(candidates))
        chunks = [indexed[i::len(self.gpus)] for i in range(len(self.gpus))]
        with ThreadPoolExecutor(max_workers=len(self.gpus)) as pool:
            futures = []
            for gi, chunk in enumerate(chunks):
                if chunk:
                    futures.append(pool.submit(self._run_chunk, chunk,
                                               base / f"g{self.gpus[gi]}",
                                               self.gpus[gi]))
            for f in futures:
                f.result()
        self.wall_s += time.time() - t0
        return candidates

    def _run_chunk(self, indexed, out_dir: Path, gpu):
        for i, cand in indexed:
            self._run_one(i, cand, out_dir, gpu)

    def _run_one(self, i: int, cand: Candidate, out_dir: Path, gpu):
        cdir = out_dir / f"{i:04d}"
        in_dir = cdir / "input"
        od = cdir / "output"
        in_dir.mkdir(parents=True, exist_ok=True)
        od.mkdir(parents=True, exist_ok=True)
        if any(od.rglob("*_summary_confidence_sample_*.json")):
            cand.metrics.update(_parse_output(od, len(cand.residues)))
            return
        yaml_text = build_complex_yaml(self.target_sequence, cand,
                                       bicyclic=self.bicyclic)
        prep = self._parse_yaml(yaml_text, default_input_name=f"cand{i:04d}")
        payload = prep.payload
        (in_dir / "input.json").write_text(json.dumps(payload, indent=1))
        cmd = [
            "docker", "run", "--rm", "--runtime", "nvidia",
            "--gpus", f"device={gpu}",
            "--entrypoint=",  # image default is the protenix CLI wrapper
            "--env", "PYTHONPATH=/app",
            "--env", "PROTENIX_ROOT_DIR=/cache",
            "--env", "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
            "--volume", f"{in_dir.resolve()}:/workspace/protenix_input",
            "--volume", f"{od.resolve()}:/workspace/protenix_output",
            "--volume", f"{COMMON_CACHE}:/root/common",
            "--volume", f"{COMMON_CACHE}:/cache/common",
            "--volume", f"{MODEL_DIR}:/workspace/model",
            "--volume", f"{SOURCE_DIR}:/app",
            "--volume", "/dev/shm:/dev/shm",
            IMAGE,
            "micromamba", "run", "-n", "protenix",
            "python3", "/app/runner/inference.py",
            "--model_name", MODEL_NAME,
            "--load_checkpoint_dir", "/workspace/model",
            "--load_checkpoint_path", "/workspace/model/protenix-v2.pt",
            "--input_json_path", "/workspace/protenix_input/input.json",
            "--dump_dir", "/workspace/protenix_output",
            "--need_atom_confidence", "True",
            "--use_msa", "false",
            "--seeds", str(self.seed),
        ]
        env = dict(os.environ)
        env.pop("CUDA_VISIBLE_DEVICES", None)
        r = subprocess.run(cmd, env=env, timeout=self.timeout_s,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
        (cdir / "docker.log").write_text(r.stdout[-20000:])
        if r.returncode != 0:
            raise RuntimeError(
                f"protenix container failed for candidate {i:04d} "
                f"({cand.seq_str[:30]}…): rc={r.returncode}, "
                f"tail={r.stdout[-400:]}")
        metrics = _parse_output(od, len(cand.residues))
        if not metrics:
            raise RuntimeError(
                f"protenix produced no confidence output for candidate {i:04d} "
                f"({cand.seq_str[:30]}…); see {cdir / 'docker.log'}")
        cand.metrics.update(metrics)
        cand.metrics["protenix_pred_root"] = str(
            od / f"cand{i:04d}" / f"seed_{self.seed}" / "predictions")
