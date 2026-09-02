"""HALO generative lead-optimization task (V-Bio task system integration).

Runs the closed-loop engine in capabilities/halo (de novo / fragment
replacement / scaffold hopping) with the native prediction oracle: candidates
are scored by the platform's own structure-prediction engines — protenix2dock
(default), boltz2dock, or alphafold3 — with the Boltz2Score affinity
post-process.

Route: POST /api/lead_optimization/halo_optimize (backend/routes/lead_opt_halo.py).
Queue: cap.lead_opt.* (CPU worker; the oracle submits GPU predictions itself).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any

from backend.core import config
from backend.core.celery_app import celery_app
from backend.routes.lead_opt_helpers import reference_ligand_sdf
from gpu_manager import get_redis_client

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="backend.worker.tasks.lead_optimization_halo_task")
def lead_optimization_halo_task(self, optimization_args: dict):
    from backend.worker import tasks as _tasks

    task_id = self.request.id
    redis_client = get_redis_client()
    tracker = _tasks.TaskProgressTracker(task_id, redis_client)
    task_temp_dir: str | None = None

    try:
        tracker.start_heartbeat()
        tracker.update_status("running", "Preparing HALO optimization inputs.")

        os.environ["VBIO_API_URL"] = str(getattr(config, "CENTRAL_API_URL", "http://127.0.0.1:5000"))
        os.environ["VBIO_API_TOKEN"] = str(getattr(config, "BOLTZ_API_TOKEN", "") or "")

        output_root = Path(getattr(config, "LEAD_OPTIMIZATION_OUTPUT_DIR", "/data/boltz_lead_optimization_results"))
        task_temp_dir = str(output_root / f"halo_task_{task_id}")
        run_dir = Path(task_temp_dir) / "run"
        run_dir.mkdir(parents=True, exist_ok=True)

        # Stage uploaded protein / reference structures into the run directory.
        staged: dict[str, str] = {}
        for key, default_name in (("protein", "target.pdb"), ("reference_sdf", "reference.sdf")):
            upload = optimization_args.get(f"{key}_upload")
            if isinstance(upload, dict) and upload.get("content_base64"):
                import base64

                dest = run_dir / default_name
                raw = base64.b64decode(upload["content_base64"])
                if key == "reference_sdf":
                    # 引擎只读 SDF；其余上传格式（mol2/mol/pdb/cif）在此转成 SDF
                    dest.write_text(
                        reference_ligand_sdf(str(upload.get("file_name") or ""), raw.decode("utf-8", errors="replace")),
                        encoding="utf-8",
                    )
                else:
                    dest.write_bytes(raw)
                staged[key] = str(dest)

        payload: dict[str, Any] = {
            "mode": optimization_args.get("mode", "fragment"),
            "backend": optimization_args.get("backend", "protenix2dock"),
            "protein_path": staged.get("protein") or optimization_args.get("protein_path", ""),
            "reference_smiles": optimization_args.get("reference_smiles", ""),
            "keep_fragment_smiles": optimization_args.get("keep_fragment_smiles", ""),
            "edit_atom_indices": optimization_args.get("edit_atom_indices", ""),
            "pocket": optimization_args.get("pocket", ""),
            "scaffold_hop_ratio": optimization_args.get("scaffold_hop_ratio", 0.4),
            "rounds": optimization_args.get("rounds", 6),
            "budget_per_round": optimization_args.get("budget_per_round", 48),
            "oracle_concurrency": optimization_args.get("oracle_concurrency", 8),
            "oracle_timeout_s": optimization_args.get("oracle_timeout_s", 7200),
            "seed": optimization_args.get("seed", 0),
            "prior_dir": optimization_args.get("prior_dir", ""),
            "target_name": optimization_args.get("target_name", ""),
            "target_chain": optimization_args.get("target_chain", "A"),
            "priority": optimization_args.get("priority", "default"),
        }
        if not payload["protein_path"]:
            raise ValueError("protein structure (upload or path) is required.")
        if staged.get("reference_sdf"):
            payload["reference_sdf_path"] = staged["reference_sdf"]

        sys_path_hint = str(Path(__file__).resolve().parents[2])
        import sys

        if sys_path_hint not in sys.path:
            sys.path.insert(0, sys_path_hint)
        capabilities_hint = str(Path(sys_path_hint) / "capabilities")
        if capabilities_hint not in sys.path:
            sys.path.insert(0, capabilities_hint)

        def report(progress: dict) -> None:
            tracker.update_status(
                "running",
                str(progress.get("message") or progress.get("stage") or "optimizing"),
                payload={"halo": progress},
            )

        from halo.vbio_runner import run_halo_optimization

        tracker.update_status("running", "Running HALO closed loop.")
        summary = run_halo_optimization(payload, run_dir, progress_cb=report, log=logger.info)

        # Results land in RESULTS_BASE_DIR (same GC coverage as every other
        # task's root zip); model weights (.pt) stay out — the loop artifacts
        # (candidates, oracle scores, config, checkpoints of state) are the
        # deliverable, 200 MB+ of prior weights are not.
        # Structured artifact for the SPA result parser (confidence.lead_opt_halo).
        # Named halo_results.json so the peptide design_results scanner can never
        # sweep it up; carries an engine marker for future disambiguation.
        # MUST be written before the result zip is sealed — it rides inside it.
        try:
            import pandas as pd

            halo_rows = []
            candidates_csv = run_dir / "candidates.csv"
            if candidates_csv.exists():
                frame = pd.read_csv(candidates_csv)
                if "final_reward" in frame.columns:
                    frame = frame.sort_values("final_reward", ascending=False)
                keep_columns = (
                    "round", "smiles", "source", "affinity_pic50", "ipsae",
                    "ligand_plddt_mean", "machine_reward", "pref_bonus",
                    "final_reward", "desc",
                )
                for _, row in frame.head(300).iterrows():
                    entry = {}
                    for column in keep_columns:
                        if column not in frame.columns:
                            continue
                        value = row[column]
                        if pd.isna(value):
                            continue
                        if hasattr(value, "item"):
                            value = value.item()
                        entry[column] = value
                    if entry.get("smiles"):
                        halo_rows.append(entry)
            (run_dir / "halo_results.json").write_text(
                json.dumps(
                    {
                        "engine": "halo",
                        "mode": summary.get("mode"),
                        "backend": summary.get("backend"),
                        "rounds_completed": summary.get("rounds_completed"),
                        "total_rounds": summary.get("total_rounds"),
                        "rounds_log": summary.get("rounds_log") or [],
                        "candidates": halo_rows,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("HALO results artifact write failed for %s: %s", task_id, exc)

        archive_path = Path(getattr(config, "RESULTS_BASE_DIR", "/data/boltz_central_results")) / f"{task_id}_lead_optimization_results.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for member in sorted(run_dir.rglob("*")):
                if member.is_file() and member.suffix in {".csv", ".json", ".jsonl", ".png", ".sdf", ".yaml"} and "oracle_work" not in member.parts:
                    zf.write(member, member.relative_to(run_dir))

        upload_response = _tasks.upload_result_to_central_api(
            task_id, str(archive_path), archive_path.name
        )
        tracker.update_status(
            "success",
            "HALO optimization complete.",
            payload={
                "summary": {
                    "mode": summary.get("mode"),
                    "backend": summary.get("backend"),
                    "n_candidates": summary.get("n_candidates"),
                    "upload": upload_response,
                }
            },
        )
        return {
            "status": "success",
            "task_id": task_id,
            "summary": summary,
            "result_file": archive_path.name,
        }
    except Exception as exc:
        logger.exception("HALO lead-optimization task %s failed", task_id)
        tracker.update_status("failed", f"HALO optimization failed: {exc}")
        from celery.exceptions import Ignore

        raise Ignore()
    finally:
        if task_temp_dir and os.path.exists(task_temp_dir):
            shutil.rmtree(task_temp_dir, ignore_errors=True)
        tracker.stop_heartbeat()
