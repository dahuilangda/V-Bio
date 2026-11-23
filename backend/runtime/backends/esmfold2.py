from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

import yaml

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest
from backend.runtime.ccd import CcdCatalog, ImageCcdCatalog, apply_custom_ccd_code_remap

ESMFOLD2_HF_MOUNT = "/root/.cache/huggingface"
_ESMFOLD2_CCD_READER = "from esm.models.esmfold2.conformers import load_ccd\ncodes = load_ccd()"


def esmfold2_hf_cache_dir() -> str:
    from backend.core.config import RESULTS_BASE_DIR

    return str(Path(RESULTS_BASE_DIR).parent / "esmfold2" / "hf_cache")


class EsmFold2Backend(StructureBackend):
    name = "esmfold2"
    serializes_ccd_blocks = False

    def ccd_catalog(self) -> CcdCatalog:
        from backend.core.config import ESMFOLD2_DOCKER_IMAGE

        env = {"HF_HOME": ESMFOLD2_HF_MOUNT}
        if os.environ.get("ESMFOLD2_WEIGHTS"):
            env["ESMFOLD2_WEIGHTS"] = os.environ["ESMFOLD2_WEIGHTS"]
        return ImageCcdCatalog(
            self.name, ESMFOLD2_DOCKER_IMAGE, "python3.12", _ESMFOLD2_CCD_READER,
            volumes=[(esmfold2_hf_cache_dir(), ESMFOLD2_HF_MOUNT)], env=env)

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request, ctx.options.get("custom_ccd_molecules"))
        request = self.post_validate(request)
        yaml_content = request.source_yaml

        ccd = self.ccd_resolution(request, ctx.options)
        yaml_content = apply_custom_ccd_code_remap(yaml_content, ccd.remap)
        if ccd.remap:
            print(f"[WARN] 自定义 CCD 编码与标准冲突，已自动重命名: {ccd.remap}",
                  file=sys.stderr)
        if ccd.molecules:
            yaml_data = yaml.safe_load(yaml_content) or {}
            yaml_data["custom_ccd_molecules"] = ccd.molecules
            yaml_content = yaml.safe_dump(yaml_data, sort_keys=False, allow_unicode=True)

        image = self._resolve_image()
        results_root, input_dir, output_dir = self._prepare_dirs(ctx)
        yaml_path = os.path.join(input_dir, "input.yaml")
        with open(yaml_path, "w", encoding="utf-8") as handle:
            handle.write(yaml_content)

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
              f"exit={completed.returncode})", file=sys.stderr)
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
        from backend.runtime.backends.support import (
            resolve_backend_results_root,
            resolve_backend_work_root,
        )
        results_root = resolve_backend_results_root(self.name, ctx.task_id, ctx.temp_dir)
        input_dir = os.path.join(resolve_backend_work_root(results_root), "input")
        output_dir = os.path.join(results_root, "output")
        os.makedirs(input_dir, exist_ok=True)
        if os.path.isdir(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)
        os.makedirs(output_dir, exist_ok=True)
        return results_root, input_dir, output_dir

    def _docker_command(self, image: str, yaml_path: str, output_dir: str,
                        seed: int) -> list:
        from backend.core.config import RESULTS_BASE_DIR
        from backend.runtime.backends.support import determine_docker_gpu_arg

        hf_cache = esmfold2_hf_cache_dir()
        os.makedirs(hf_cache, exist_ok=True)
        project_root = str(Path(__file__).resolve().parents[3])
        container_name = f"esmfold2-predict-{uuid.uuid4().hex[:12]}"
        gpu_arg = determine_docker_gpu_arg(os.environ.get("CUDA_VISIBLE_DEVICES"))

        command = [
            "docker", "run", "--rm", "--name", container_name, "--entrypoint=",
            "--runtime", "nvidia", "--shm-size", "16g",
            "--gpus", gpu_arg,
            "--add-host", "host.docker.internal:host-gateway",
            "--volume", f"{hf_cache}:{ESMFOLD2_HF_MOUNT}",
            "--volume", f"{project_root}:/workspace/vbio:ro",
            "--volume", f"{RESULTS_BASE_DIR}:{RESULTS_BASE_DIR}",
            "--env", f"HF_HOME={ESMFOLD2_HF_MOUNT}",
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
                    rel = os.path.relpath(full, output_dir)
                    zf.write(full, os.path.join("esmfold2/output", rel))
            yaml_src = os.path.join(input_dir, "input.yaml")
            if os.path.exists(yaml_src):
                zf.write(yaml_src, "esmfold2/input.yaml")


def _container_name(command: list) -> str:
    return command[command.index("--name") + 1]
