"""ESMFold2 后端：容器内折叠，支持 bond 约束（含首尾环化与双环链接键）。"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest


class EsmFold2Backend(StructureBackend):
    name = "esmfold2"
    supported_constraints = frozenset({"bond"})
    supported_chirality = frozenset({"l"})

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request)
        image = self._resolve_image()
        results_root, input_dir, output_dir = self._prepare_dirs(ctx)
        yaml_path = os.path.join(input_dir, "input.yaml")
        with open(yaml_path, "w", encoding="utf-8") as handle:
            handle.write(ctx.yaml_content)

        seed = int(ctx.seed)
        command = self._docker_command(image, yaml_path, output_dir, seed)

        started = time.time()
        timeout_seconds = int(os.environ.get("ESMFOLD2_RUN_TIMEOUT_SECONDS") or 14400)
        try:
            completed = subprocess.run(command, check=False, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "stop", _container_name(command)],
                           capture_output=True)
            raise RuntimeError(f"ESMFold2 inference timed out after {timeout_seconds}s")
        print(f"ESMFold2 inference finished ({time.time() - started:.0f}s, "
              f"exit={completed.returncode})", file=__import__("sys").stderr)
        if completed.returncode != 0:
            raise RuntimeError(f"ESMFold2 inference failed (exit {completed.returncode})")

        self._postprocess_ipsae(output_dir)
        self._write_archive(results_root, input_dir, output_dir,
                            ctx.output_archive_path)

    def _resolve_image(self) -> str:
        from backend.core.config import ESMFOLD2_DOCKER_IMAGE
        from backend.worker.docker_cmd import ensure_image, image_and_python
        image = str(ESMFOLD2_DOCKER_IMAGE or "").strip()
        ensure_image(image)
        image, _ = image_and_python(image)
        return image

    def _prepare_dirs(self, ctx: BackendRunContext) -> tuple[str, str, str]:
        from backend.core.config import RESULTS_BASE_DIR
        from backend.runtime.run_single_prediction import (
            _resolve_backend_results_root,
            _resolve_backend_work_root,
        )
        results_root = _resolve_backend_results_root(self.name, ctx.task_id, ctx.temp_dir)
        input_dir = os.path.join(_resolve_backend_work_root(results_root), "input")
        output_dir = os.path.join(results_root, "output")
        os.makedirs(input_dir, exist_ok=True)
        if os.path.isdir(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)
        os.makedirs(output_dir, exist_ok=True)
        return results_root, input_dir, output_dir

    def _docker_command(self, image: str, yaml_path: str, output_dir: str,
                        seed: int) -> list:
        from backend.core.config import RESULTS_BASE_DIR
        from backend.runtime.run_single_prediction import determine_docker_gpu_arg

        hf_cache = str(Path(RESULTS_BASE_DIR).parent / "esmfold2" / "hf_cache")
        os.makedirs(hf_cache, exist_ok=True)
        project_root = str(Path(__file__).resolve().parents[3])
        container_name = f"esmfold2-predict-{uuid.uuid4().hex[:12]}"
        gpu_arg = determine_docker_gpu_arg(os.environ.get("CUDA_VISIBLE_DEVICES"))

        command = [
            "docker", "run", "--rm", "--name", container_name, "--entrypoint=",
            "--runtime", "nvidia", "--shm-size", "16g",
            "--gpus", gpu_arg,
            "--add-host", "host.docker.internal:host-gateway",
            "--volume", f"{hf_cache}:/root/.cache/huggingface",
            "--volume", f"{project_root}:/workspace/vbio:ro",
            "--volume", f"{RESULTS_BASE_DIR}:{RESULTS_BASE_DIR}",
            "--env", "HF_HOME=/root/.cache/huggingface",
            "--env", "PYTHONUNBUFFERED=1",
        ]
        for env_key in ("ESMFOLD2_WEIGHTS", "ESMFOLD2_NUM_LOOPS",
                        "ESMFOLD2_NUM_SAMPLING_STEPS"):
            value = os.environ.get(env_key)
            if value:
                command += ["--env", f"{env_key}={value}"]
        command += [
            image, "python3.12", "/workspace/vbio/capabilities/esmfold2/esmfold2_predict.py",
            "--yaml", yaml_path,
            "--output-dir", output_dir,
            "--seed", str(seed),
        ]
        samples = os.environ.get("ESMFOLD2_SAMPLES")
        if samples:
            command += ["--samples", samples]
        return command

    def _postprocess_ipsae(self, output_dir: str) -> None:
        import sys

        repo = Path(__file__).resolve().parents[3]
        p2d = str(repo / "capabilities" / "protenix2dock")
        if p2d not in sys.path:
            sys.path.insert(0, p2d)
        try:
            from core.ipsae import compute_ipsae_for_output
            compute_ipsae_for_output(Path(output_dir))
        except Exception as exc:
            print(f"ipSAE post-processing skipped: {exc}", file=sys.stderr)

    def _write_archive(self, results_root: str, input_dir: str, output_dir: str,
                       output_archive_path: str) -> None:
        if os.path.exists(output_archive_path):
            os.remove(output_archive_path)
        with zipfile.ZipFile(output_archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(output_dir):
                for name in files:
                    full = os.path.join(root, name)
                    zf.write(full, os.path.relpath(full, results_root))
            yaml_src = os.path.join(input_dir, "input.yaml")
            if os.path.exists(yaml_src):
                zf.write(yaml_src, os.path.relpath(yaml_src, results_root))


def _container_name(command: list) -> str:
    return command[command.index("--name") + 1]
