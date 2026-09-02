"""Boltz2Score oracle client: multi-GPU parallel score-mode invocations.

Candidates are posed into the pocket (MCS alignment to the co-crystal
reference) and scored in score mode (confidence + affinity + ipSAE),
~8 s/ligand on an RTX 4090. Batches are split across GPUs; one CLI
process per GPU chunk amortizes model loading.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from halo import REPO_ROOT

BOLTZ2SCORE_CLI = REPO_ROOT / "capabilities" / "boltz2score" / "boltz2score.py"
from halo.data.targets import Target
from halo.oracle.pose import build_poses, reference_ligand_with_conformer


class BoltzOracle:
    def __init__(self, target: Target, work_dir: Path, gpus=(1, 2, 3), score_batch_size=24,
                 recycling_steps=1, affinity_recycling_steps=1, precision="bf16-mixed",
                 timeout_s=3600):
        self.target = target
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.gpus = list(gpus) or ["0"]
        self.score_batch_size = score_batch_size
        self.recycling_steps = recycling_steps
        self.affinity_recycling_steps = affinity_recycling_steps
        self.precision = precision
        self.timeout_s = timeout_s
        self.ref_ligand = reference_ligand_with_conformer(target.ligands_sdf)
        self.n_calls = 0
        self.n_gpu_seconds = 0.0

    # ------------------------------------------------------------------
    def score_smiles(self, smiles_list: list[str], tag: str = "batch") -> pd.DataFrame:
        """Pose + score a list of SMILES. Returns one row per molecule."""
        if not smiles_list:
            return pd.DataFrame()
        self.n_calls += len(smiles_list)
        t0 = time.time()
        posed_infos = []
        remaining = list(enumerate(smiles_list))
        base = self.work_dir / tag
        base.mkdir(parents=True, exist_ok=True)

        # split into per-GPU chunks
        chunks = [remaining[i:: len(self.gpus)] for i in range(len(self.gpus))]
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=len(self.gpus)) as pool:
            futures = []
            for gi, chunk in enumerate(chunks):
                if not chunk:
                    continue
                gpu = self.gpus[gi]
                for bi in range(0, len(chunk), self.score_batch_size):
                    sub = chunk[bi : bi + self.score_batch_size]
                    futures.append(pool.submit(self._run_chunk, sub, base / f"g{gpu}_b{bi}", gpu))
            for f in futures:
                rows.extend(f.result())
        self.n_gpu_seconds += time.time() - t0
        df = pd.DataFrame(rows)
        df.attrs["wall_s"] = time.time() - t0
        return df

    # ------------------------------------------------------------------
    def _run_chunk(self, indexed_smiles: list[tuple[int, str]], out_dir: Path, gpu) -> list[dict]:
        """Pose + score a chunk; on numerical failures bisect to isolate bad molecules.

        A single degenerate molecule can crash Boltz's SVD for the whole batch
        (ill-conditioned), so failed batches are split in half recursively and
        only the offending molecules are dropped.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        sdf = out_dir / "poses.sdf"
        infos = build_poses([s for _, s in indexed_smiles], self.ref_ligand, sdf)
        n_poseable = sum(1 for i in infos if i["method"] != "failed")
        if n_poseable == 0:
            return self._parse_output(infos, out_dir / "score")
        score_out = self._invoke_cli(sdf, out_dir, gpu, expect=n_poseable)
        if score_out is None and len(indexed_smiles) > 1:
            # bisect: score halves separately, isolating the offender(s)
            mid = len(indexed_smiles) // 2
            rows = []
            for part, name in ((indexed_smiles[:mid], "h1"), (indexed_smiles[mid:], "h2")):
                if part:
                    rows.extend(self._run_chunk(part, out_dir / name, gpu))
            return rows
        return self._parse_output(infos, out_dir / "score")

    # ------------------------------------------------------------------
    def _invoke_cli(self, sdf: Path, out_dir: Path, gpu, expect: int) -> Path | None:
        """Run the scoring CLI; returns score dir on success, None on failure."""
        cmd = [
            sys.executable, str(BOLTZ2SCORE_CLI), "--mode", "score",
            "--protein_file", str(self.target.protein_pdb),
            "--ligand_file", str(sdf),
            "--output_dir", str(out_dir / "score"),
            "--compute_ipsae",
            "--enable_affinity",
            "--target_chain", self.target.target_chain,
            "--ligand_chain", self.target.ligand_chain,
            "--recycling_steps", str(self.recycling_steps),
            "--affinity_recycling_steps", str(self.affinity_recycling_steps),
            "--trainer_precision", self.precision,
            "--accelerator", "gpu",
            "--devices", "1",
            "--seed", "42",
        ]
        env = dict(os.environ)
        env["CUDA_VISIBLE_DEVICES"] = str(gpu)
        score_out = out_dir / "score"
        for attempt in range(2):
            use_cmd = cmd if attempt == 0 else cmd[: cmd.index("--trainer_precision") + 1] + ["32"]
            try:
                r = subprocess.run(
                    use_cmd, env=env, cwd=str(BOLTZ2SCORE_CLI.parent), timeout=self.timeout_s,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                try:
                    (out_dir / f"cli{'' if attempt == 0 else '_retry32'}.log").write_text(r.stdout[-20000:])
                except OSError:
                    pass
                n_ok = len(list(score_out.glob("*/best_confidence.json"))) if score_out.exists() else 0
                if r.returncode == 0 and n_ok >= expect:
                    return score_out
                # numerical failures (ill-conditioned SVD) or silent crashes:
                # retry once in full fp32, caller bisects if still failing
            except subprocess.TimeoutExpired:
                try:
                    (out_dir / f"cli{'' if attempt == 0 else '_retry32'}.log").write_text("TIMEOUT")
                except OSError:
                    pass
        return None

    # ------------------------------------------------------------------
    def _parse_output(self, infos: list[dict], score_dir: Path) -> list[dict]:
        rows = []
        score_dir = Path(score_dir)
        record_dirs = {}
        if score_dir.exists():
            for d in score_dir.iterdir():
                if not d.is_dir():
                    continue
                m = re.search(r"__(\d{4})_", d.name)
                if m:
                    record_dirs[int(m.group(1))] = d
        # records exist only for non-failed poses, numbered 1..n in order
        record_dir_iter = iter(record_dirs[i] for i in sorted(record_dirs))
        for info in infos:
            row = {"smiles": info["smiles"], "pose_method": info["method"], "pose_rmsd": info["rmsd"]}
            d = next(record_dir_iter, None) if info["method"] != "failed" else None
            if d is None:
                row.update({k: None for k in (
                    "affinity_pic50", "affinity_pic50_mw", "affinity_probability_binary",
                    "ipsae", "ligand_plddt_mean", "confidence_score", "iptm", "ligand_iptm")})
                rows.append(row)
                continue
            conf = d / "best_confidence.json"
            if conf.exists():
                c = json.loads(conf.read_text())
                row.update({
                    "ipsae": c.get("ipsae_dom"),
                    "ligand_plddt_mean": c.get("ligand_plddt_mean"),
                    "confidence_score": c.get("confidence_score"),
                    "iptm": c.get("iptm"),
                    "ligand_iptm": c.get("ligand_iptm"),
                })
                # per-atom pLDDT in canonical-SMILES order -> structure-guided editing
                plddts = c.get("ligand_atom_plddts__smiles_order_")
                if isinstance(plddts, list) and plddts:
                    row["atom_plddts_smiles_order"] = json.dumps([round(float(v), 1) for v in plddts])
                    low = [i for i, v in enumerate(plddts) if float(v) < 60.0]
                    row["low_plddt_atoms"] = json.dumps(low)
            aff = None
            for f in d.glob("affinity_*.json"):
                aff = f
                break
            if aff is not None:
                a = json.loads(aff.read_text())
                row.update({
                    "affinity_pic50": a.get("affinity_pic50"),
                    "affinity_pic50_mw": a.get("affinity_pic50_mw"),
                    "affinity_probability_binary": a.get("affinity_probability_binary"),
                })
            rows.append(row)
        return rows
