"""protenix2dock affinity-head training task (V-Bio task system integration).

Long-running job: runs capabilities/protenix2dock/train_affinity.py inside
the Protenix runtime image. Supports sharded epochs via --resume_ckpt so a
multi-day training run can be split across sequential tasks (each shard
persists a checkpoint the next shard resumes from).

Route: POST /api/affinity_train (backend/worker registered here; see
backend/routes/affinity.py for the endpoint).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import zipfile
from typing import Any

from celery.exceptions import Ignore

from backend.core import config
from backend.core.celery_app import celery_app
from gpu_manager import get_redis_client, release_gpu

import logging

logger = logging.getLogger(__name__)

_TRAIN_SCRIPT = "/workspace/vbio/capabilities/protenix2dock/train_affinity.py"
_TRAIN_DATA_ROOT = "/data/affinity_training"

# [progress] epoch=0/4 sample=10/1000 step=11 loss=1.2345
_PROGRESS_RE = re.compile(
    r"\[progress\] epoch=(\d+)/(\d+) sample=(\d+)/(\d+) step=(\d+) loss=([\d.eE+-]+)"
)


@celery_app.task(bind=True, name="backend.worker.tasks.affinity_train_task")
def affinity_train_task(self, train_args: dict):
    from backend.worker import tasks as _tasks

    task_id = self.request.id
    redis_client = get_redis_client()
    tracker = _tasks.TaskProgressTracker(task_id, redis_client)
    gpu_id = -1

    try:
        tracker.update_status("waiting_gpu", "Waiting for GPU allocation")
        gpu_id = _tasks._acquire_gpu_with_non_peptide_wait_registration(task_id=task_id, timeout=3600)
        tracker.update_status("running", f"Acquired GPU {gpu_id}. Starting affinity training.")
        _tasks._raise_if_task_cancelled(self, redis_client, task_id)

        task_temp_dir = os.path.join(_TRAIN_DATA_ROOT, f"train_task_{task_id}")
        os.makedirs(task_temp_dir, exist_ok=True)
        work_dir = train_args.get("work_dir") or os.path.join(task_temp_dir, "work")
        os.makedirs(work_dir, exist_ok=True)

        # NOTE: inputs live on the HOST; the training container (launched via
        # docker CLI from this worker) mounts them directly. This worker
        # container may not share those mounts, so no host-path existence
        # checks here — the training entry validates inside its container.
        index_csv = train_args.get("index_csv") or os.path.join(_TRAIN_DATA_ROOT, "curated_300k/train.csv")
        val_csv = train_args.get("val_csv") or os.path.join(_TRAIN_DATA_ROOT, "curated_300k/val.csv")

        entry: list[str] = [
            "--index_csv", index_csv,
            "--work_dir", work_dir,
            "--epochs", str(int(train_args.get("epochs", 1))),
            "--rel_weight", str(float(train_args.get("rel_weight", 2.0))),
            "--msa_prob", str(float(train_args.get("msa_prob", 0.5))),
            "--lr", str(float(train_args.get("lr", 1e-4))),
            "--max_seq_len", str(int(train_args.get("max_seq_len", 1200))),
            "--ckpt_every", str(int(train_args.get("ckpt_every", 0))),
            "--num_blocks", str(int(train_args.get("num_blocks", 2))),
            "--dropout", str(float(train_args.get("dropout", 0.1))),
            "--val_csv", val_csv,
            "--val_limit", str(int(train_args.get("val_limit", 200))),
        ]
        resume = train_args.get("resume_ckpt")
        if resume:
            entry.extend(["--resume_ckpt", str(resume)])
        # Shard override wins over the default index csv; never emit two
        # --index_csv flags (argparse would silently keep the last one).
        if train_args.get("shard_csv"):
            entry[entry.index("--index_csv") + 1] = str(train_args["shard_csv"])
        if train_args.get("msa_server_url") is not None:
            entry.extend(["--msa_server_url", str(train_args["msa_server_url"])])
        # the runtime mount exposes the shared MSA cache at /data/msa_cache
        entry.extend(["--msa_cache_dir", str(train_args.get("msa_cache_dir") or "/data/msa_cache")])

        # Docker: protenix runtime image with training data + msa cache mounted.
        from backend.worker import docker_cmd

        command, container_name = docker_cmd.build_task_docker_skeleton(
            task_id=task_id, gpu_id=gpu_id, runtime_label="affinity-train",
        )
        docker_cmd.protenix_runtime_mounts(command)
        command.extend([
            "--volume", f"{_TRAIN_DATA_ROOT}:{_TRAIN_DATA_ROOT}",
            "--workdir", "/workspace/vbio/capabilities/protenix2dock",
            # The image's default entrypoint is the protenix CLI; the
            # PROTENIX_DOCKER_EXTRA_ARGS default (--entrypoint=) overrides it
            # so we can run arbitrary python commands.
            *shlex.split(str(getattr(config, "PROTENIX_DOCKER_EXTRA_ARGS", "") or "")),
        ])
        image, python_bin = docker_cmd.image_and_python()
        command.append(image)
        command.append(python_bin)
        command.append(_TRAIN_SCRIPT)
        command.extend(entry)

        _tasks._terminate_task_container(container_name)
        logger.info("Task %s: affinity-train docker: %s", task_id,
                    " ".join(shlex.quote(p) for p in command))
        tracker.update_status("running", "Training in progress")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            bufsize=1,
        )
        tracker.register_process(process.pid)

        # Stream stdout for progress heartbeats (training logs [progress] lines).
        last_progress = ""
        tail_lines: list[str] = []
        import time

        hard_timeout = int(train_args.get("timeout_seconds", 0)) or None
        t0 = time.time()
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            tail_lines.append(line)
            if len(tail_lines) > 200:
                tail_lines.pop(0)
            m = _PROGRESS_RE.search(line)
            if m:
                last_progress = (
                    f"epoch {m.group(1)}/{m.group(2)} sample {m.group(3)}/{m.group(4)} "
                    f"loss {m.group(6)}"
                )
                tracker.update_status("running", f"Training: {last_progress}")
            _tasks._raise_if_task_cancelled(self, redis_client, task_id)
            if hard_timeout and time.time() - t0 > hard_timeout:
                process.kill()
                raise TimeoutError(f"affinity training exceeded {hard_timeout}s")
        process.wait()
        rc = process.returncode

        if rc != 0:
            tail_text = "\n".join(tail_lines[-40:])
            raise RuntimeError(
                f"affinity training failed (exit {rc}). Tail:\n{tail_text}"
            )

        # Package artifacts: checkpoints + val log.
        tracker.update_status("processing_output", "Packaging training artifacts")
        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_results.zip")
        with zipfile.ZipFile(output_archive_path, "w") as zipf:
            for fname in (
                "protenix_affinity_head.pt",
                "protenix_affinity_head_avg.pt",
            ):
                fpath = os.path.join(work_dir, fname)
                if os.path.exists(fpath):
                    zipf.write(fpath, fname)
            log_file = os.path.join(task_temp_dir, "train.log")
            with open(log_file, "w", encoding="utf-8") as fh:
                fh.write("\n".join(tail_lines))
            zipf.write(log_file, "train.log")

        tracker.update_status("uploading", "Uploading artifacts")
        if gpu_id != -1:
            release_gpu(gpu_id=gpu_id, task_id=task_id)
            gpu_id = -1
        upload_response = _tasks.upload_result_to_central_api(
            task_id, output_archive_path, os.path.basename(output_archive_path)
        )

        final_meta = {
            "status": "Complete",
            "upload_info": upload_response,
            "result_file": os.path.basename(output_archive_path),
            "last_progress": last_progress,
            "checkpoint_dir": work_dir,
        }
        self.update_state(state="SUCCESS", meta=final_meta)
        tracker.update_status("completed", f"Training shard complete: {last_progress}")
        return final_meta

    except Ignore:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Task {task_id} failed: {e}", exc_info=True)
        tracker.update_status("failed", _tasks._truncate_text(e, 4000))
        self.update_state(state="FAILURE", meta=_tasks._build_failure_meta(e))
        raise
    finally:
        _tasks._terminate_task_containers_by_task_id(task_id)
        if gpu_id != -1:
            release_gpu(gpu_id=gpu_id, task_id=task_id)
