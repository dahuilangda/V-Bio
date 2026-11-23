from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Optional

import yaml

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest
from backend.runtime.ccd import CcdCatalog, MmcifCcdCatalog, apply_custom_ccd_code_remap

_PROTENIX_INPUT_CONTAINER_DIR = "/workspace/protenix_input"
_PROTENIX_TEMPLATE_CONTAINER_DIR = f"{_PROTENIX_INPUT_CONTAINER_DIR}/template_mmcif"


class ProtenixBackend(StructureBackend):
    name = "protenix"
    ccd_code_pattern = r"[A-Z0-9]{1,12}"
    injects_referenced_presets = True
    validates_ccd_mmcif = True

    def ccd_catalog(self) -> CcdCatalog:
        from backend.core.config import PROTENIX_COMMON_CACHE_DIR
        return MmcifCcdCatalog(self.name, Path(PROTENIX_COMMON_CACHE_DIR) / "components.cif")

    def post_validate(self, request: PredictionRequest) -> PredictionRequest:
        from backend.runtime.constraints_pipeline import (
            remap_constraints_by_template_alignment,
            sanitize_constraints_for_chain_lengths,
        )
        from backend.runtime.run_single_prediction import (
            _validate_amidated_terminal_constraints,
        )
        yaml_text = request.source_yaml
        yaml_text = remap_constraints_by_template_alignment(yaml_text)
        yaml_text = sanitize_constraints_for_chain_lengths(yaml_text)
        _validate_amidated_terminal_constraints(
            yaml_text, request.custom_ccd_molecules)
        return PredictionRequest.from_yaml(yaml_text)

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request, ctx.options.get("custom_ccd_molecules"))
        request = self.post_validate(request)
        yaml_content = request.source_yaml

        from backend.core.config import (
            PROTENIX_COMMON_CACHE_DIR,
            PROTENIX_CONTAINER_APP_DIR,
            PROTENIX_CONTAINER_CHECKPOINT_PATH,
            PROTENIX_CONTAINER_MODEL_DIR,
            PROTENIX_DOCKER_EXTRA_ARGS,
            PROTENIX_DOCKER_IMAGE,
            PROTENIX_INFER_EXTRA_ARGS,
            PROTENIX_MODEL_DIR,
            PROTENIX_MODEL_NAME,
            PROTENIX_PYTHON_BIN,
            PROTENIX_SOURCE_DIR,
            PROTENIX_USE_HOST_USER,
        )
        from backend.runtime.run_single_prediction import (
            _assert_msa_server_configured,
            _docker_warn,
            _detect_bicyclic_linker_codes,
            _linker_ccd_mmcif_bundle,
            _linker_ccd_mols,
            _merge_custom_ccd_with_existing_cache,
            _normalize_protenix_output_permissions,
            _PROTENIX_INPUT_CONTAINER_DIR,
            _PROTENIX_TEMPLATE_CONTAINER_DIR,
            _raise_if_protenix_reported_error_without_structure,
            _require_complete_external_msa,
            _run_protenix_ipsae_postprocess,
            _stage_protenix_user_templates,
            _validate_amidated_terminal_constraints,
            apply_protein_msa_paths,
            create_protenix_archive,
            parse_yaml_for_protenix,
            run_protenix_affinity_pipeline,
            MSA_CACHE_CONFIG,
        )
        from backend.runtime.af3_adapter import (
            collect_chain_msa_paths,
            parse_yaml_for_af3 as _parse_af3,
            safe_filename,
        )
        from backend.runtime.bicyclic_linkers import get_linker
        from backend.runtime.backends.support import (
            determine_docker_gpu_arg,
            resolve_backend_results_root,
            resolve_backend_work_root,
        )
        from backend.runtime.custom_ccd_builder import _append_custom_residues_ccd_from_molecules
        from backend.runtime.docker_exec import remove_container, run_docker_streaming
        from backend.runtime.msa_pipeline import cache_msa_files_from_temp_dir
        from backend.services.common_utils import ProteinMsaMode
        from backend.worker.docker_cmd import (
            collect_gpu_device_group_ids,
            ensure_image,
            make_scoped_container_name,
            readable_docker_failure_diagnosis,
            sanitize_docker_extra_args,
        )

        msa_mode = str(ctx.options.get("msa_mode") or "auto")
        ipsae_ligand_chain_id = ctx.options.get("ipsae_ligand_chain_id")
        low_vram = bool(ctx.options.get("low_vram", False))
        template_payloads = (ctx.options.get("template_payloads")
                             if isinstance(ctx.options.get("template_payloads"), list) else None)
        seed = ctx.seed or 101
        temp_dir = ctx.temp_dir

        ensure_image(str(PROTENIX_DOCKER_IMAGE or "vbio-protenix-v2-runtime:2.0.0").strip())
        print("Using Protenix backend", file=sys.stderr)

        ccd = self.ccd_resolution(request, ctx.options)
        custom_molecules = ccd.molecules
        if ccd.remap:
            yaml_content = apply_custom_ccd_code_remap(yaml_content, ccd.remap)
            print(f"[WARN] 自定义 CCD 编码与标准冲突，已自动重命名: {ccd.remap}", file=sys.stderr)
        protenix_common_dir = Path(str(PROTENIX_COMMON_CACHE_DIR).strip())

        prep = parse_yaml_for_protenix(yaml_content)
        protenix_json = prep.payload
        protein_entity_indices = {
            entity_index
            for entity_index, kind in prep.entity_kinds.items()
            if str(kind).lower() == "protein"
        }
        if not protein_entity_indices:
            raise RuntimeError("Protenix input does not contain protein entities.")
        required_msa_entity_indices = {
            entity_index
            for entity_index in protein_entity_indices
            if prep.entity_msa_modes[entity_index] is not ProteinMsaMode.DISABLED
        }
        external_msa_entity_indices = {
            entity_index
            for entity_index in protein_entity_indices
            if prep.entity_msa_modes[entity_index] is ProteinMsaMode.EXTERNAL
        }
        use_msa_server = bool(external_msa_entity_indices)

        chain_msa_paths_local: dict = {}
        host_msa_paths_for_archive: dict = {}

        protenix_results_root = resolve_backend_results_root(self.name, ctx.task_id, temp_dir)
        protenix_work_root = resolve_backend_work_root(protenix_results_root)
        _validate_amidated_terminal_constraints(yaml_content, custom_molecules)
        linker_codes = [c for c in _detect_bicyclic_linker_codes(yaml_content)
                        if get_linker(c).protenix_overlay]
        linker_extra_cif = _linker_ccd_mmcif_bundle(linker_codes) if linker_codes else ""
        linker_extra_mols = _linker_ccd_mols(linker_codes) if linker_codes else {}
        protenix_common_overlay_root: Optional[Path] = None
        if custom_molecules or linker_extra_cif:
            protenix_common_overlay_root = _merge_custom_ccd_with_existing_cache(
                protenix_common_dir,
                protenix_work_root / "protenix_common_overlay",
                custom_molecules,
                extra_cif_text=linker_extra_cif,
                extra_mols=linker_extra_mols,
            )

        if use_msa_server:
            msa_server_url = _assert_msa_server_configured("protenix")
            print(f"开始使用 MSA 服务器生成多序列比对: {msa_server_url}", file=sys.stderr)
            _require_complete_external_msa(
                yaml_content, str(protenix_work_root), "Protenix", msa_mode=msa_mode)
            print("MSA 生成成功，将用于 Protenix 输入", file=sys.stderr)
            if MSA_CACHE_CONFIG["enable_cache"]:
                cache_msa_files_from_temp_dir(str(protenix_work_root), yaml_content)
        else:
            print("Protenix 输入不需要外部 MSA 生成。", file=sys.stderr)

        protenix_input_dir = str(protenix_work_root / "input")
        protenix_output_dir = str(protenix_results_root / "output")
        protenix_msa_dir = os.path.join(protenix_input_dir, "msa")
        os.makedirs(protenix_input_dir, exist_ok=True)
        os.makedirs(protenix_output_dir, exist_ok=True)
        os.makedirs(protenix_msa_dir, exist_ok=True)

        try:
            msa_yaml_data = yaml.safe_load(yaml_content) or {}
            if isinstance(msa_yaml_data, dict):
                msa_yaml_data.pop("constraints", None)
            msa_yaml_content = yaml.safe_dump(msa_yaml_data, sort_keys=False)
            af3_prep = _parse_af3(msa_yaml_content, default_jobname=prep.input_name)
            cache_dir = MSA_CACHE_CONFIG["cache_dir"] if MSA_CACHE_CONFIG["enable_cache"] else None
            chain_msa_paths = collect_chain_msa_paths(af3_prep, str(protenix_work_root), cache_dir)
            for chain_id, path in chain_msa_paths.items():
                if not path or not path.exists():
                    continue
                dst_name = f"{safe_filename(chain_id)}.a3m"
                dst_host_path = os.path.join(protenix_msa_dir, dst_name)
                shutil.copyfile(str(path), dst_host_path)
                chain_msa_paths_local[chain_id] = f"/workspace/protenix_input/msa/{dst_name}"
                host_msa_paths_for_archive[chain_id] = str(path)
        except Exception as msa_err:
            raise RuntimeError(f"Protenix MSA path resolution failed: {msa_err}") from msa_err

        effective_use_msa = bool(required_msa_entity_indices)
        disabled_msa_path_local: Optional[str] = None
        if effective_use_msa and required_msa_entity_indices != protein_entity_indices:
            disabled_msa_host_path = Path(protenix_msa_dir) / "_disabled.a3m"
            disabled_msa_host_path.write_text("")
            disabled_msa_path_local = "/workspace/protenix_input/msa/_disabled.a3m"

        assigned_count = apply_protein_msa_paths(
            prep,
            chain_msa_paths_local,
            disabled_msa_path=disabled_msa_path_local,
        )
        protenix_json = prep.payload
        required_protein_entities = len(required_msa_entity_indices)
        if assigned_count != required_protein_entities:
            raise RuntimeError(
                f"Protenix external MSA assignment incomplete: assigned={assigned_count}, "
                f"required={required_protein_entities}")
        if assigned_count:
            print(f"已为 {assigned_count} 个蛋白实体挂载 MSA", file=sys.stderr)

        use_template = _stage_protenix_user_templates(
            template_payloads or [],
            protenix_json,
            protenix_input_dir,
            prep,
        )
        if use_template:
            print(
                "Protenix 模板输入已启用：用户模板写入 template_mmcif 并挂载到对应蛋白实体。",
                file=sys.stderr,
            )

        input_json_path = os.path.join(protenix_input_dir, "input.json")
        with open(input_json_path, "w", encoding="utf-8") as f:
            json.dump(protenix_json, f, indent=2, ensure_ascii=False)

        model_dir = PROTENIX_MODEL_DIR
        source_dir = PROTENIX_SOURCE_DIR
        container_app_dir = str(PROTENIX_CONTAINER_APP_DIR or "/app").strip() or "/app"
        container_model_dir = str(PROTENIX_CONTAINER_MODEL_DIR or "/workspace/model").strip() or "/workspace/model"
        model_name_raw = (PROTENIX_MODEL_NAME or "protenix-v2").strip()
        model_name = model_name_raw[:-3] if model_name_raw.endswith(".pt") else model_name_raw
        if not model_name:
            raise ValueError("PROTENIX_MODEL_NAME 不能为空。")
        checkpoint_filename = f"{model_name}.pt"
        image = PROTENIX_DOCKER_IMAGE or os.environ.get("P2D_IMAGE", "vbio-protenix-v2-runtime:2.0.0")
        raw_extra_args = shlex.split(PROTENIX_DOCKER_EXTRA_ARGS) if PROTENIX_DOCKER_EXTRA_ARGS else []
        extra_args = sanitize_docker_extra_args(raw_extra_args, log=_docker_warn)
        infer_extra_args = shlex.split(PROTENIX_INFER_EXTRA_ARGS) if PROTENIX_INFER_EXTRA_ARGS else []
        if not model_dir or not os.path.isdir(model_dir):
            raise FileNotFoundError("PROTENIX_MODEL_DIR 未配置或目录不存在，无法运行 Protenix 容器。")
        if not source_dir or not os.path.isdir(source_dir):
            raise FileNotFoundError(
                "PROTENIX_SOURCE_DIR 未配置或目录不存在。"
                "官方 Protenix Docker 镜像默认不内置源码，请先 clone Protenix 并配置该路径。"
            )
        inference_script_path = os.path.join(source_dir, "runner", "inference.py")
        if not os.path.isfile(inference_script_path):
            raise FileNotFoundError(
                f"在 PROTENIX_SOURCE_DIR 下未找到 runner/inference.py: {inference_script_path}")

        checkpoint_path = os.path.join(model_dir, checkpoint_filename)
        if not os.path.isfile(checkpoint_path):
            raise FileNotFoundError(
                f"未找到 Protenix 模型文件: {checkpoint_path}. "
                "请确认 PROTENIX_MODEL_DIR 与 PROTENIX_MODEL_NAME 配置正确。")
        protenix_common_cache_dir = os.path.abspath(str(PROTENIX_COMMON_CACHE_DIR).strip())
        os.makedirs(protenix_common_cache_dir, exist_ok=True)
        protenix_common_cache_mount = protenix_common_cache_dir
        if protenix_common_overlay_root is not None:
            protenix_common_cache_mount = str(protenix_common_overlay_root / "common")

        visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
        try:
            gpu_arg = determine_docker_gpu_arg(visible_devices)
        except RuntimeError as gpu_err:
            print(f"[protenix] GPU env unavailable: {gpu_err}", file=sys.stderr)
            raise

        runtime_task_id = str(ctx.task_id or os.environ.get("VBIO_TASK_ID") or "").strip()
        task_container_name = make_scoped_container_name(runtime_task_id, prefix="boltz-af3")

        runtime_overridden = any(token == "--runtime" for token in extra_args)
        docker_command = ["docker", "run", "--rm"]
        if task_container_name:
            docker_command.extend(["--name", task_container_name])
            docker_command.extend(["--label", f"boltz.task_id={runtime_task_id}"])
            docker_command.extend(["--label", "boltz.runtime=protenix"])
        if not runtime_overridden:
            docker_command.extend(["--runtime", "nvidia"])

        protenix_container_env = [
            f"PYTHONPATH={container_app_dir}",
            "PROTENIX_ROOT_DIR=/cache",
            "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
        ]
        if low_vram:
            protenix_container_env.append("PROTENIX_LOW_VRAM=1")

        docker_command.extend(["--gpus", gpu_arg])
        for env_kv in protenix_container_env:
            docker_command.extend(["--env", env_kv])
        docker_command.extend([
            "--volume", f"{protenix_input_dir}:/workspace/protenix_input",
            "--volume", f"{protenix_output_dir}:/workspace/protenix_output",
            "--volume", f"{protenix_common_cache_mount}:/root/common",
            "--volume", f"{protenix_common_cache_mount}:/cache/common",
            "--volume", f"{model_dir}:{container_model_dir}",
            "--volume", f"{source_dir}:{container_app_dir}",
        ])
        if os.path.exists("/dev/shm"):
            docker_command.extend(["--volume", "/dev/shm:/dev/shm"])

        use_host_user = str(PROTENIX_USE_HOST_USER or "").strip().lower() in {"1", "true", "yes", "on"}
        if use_host_user:
            host_uid = os.getuid()
            host_gid = os.getgid()
            docker_command.extend(["--user", f"{host_uid}:{host_gid}"])
            for gid in collect_gpu_device_group_ids():
                docker_command.extend(["--group-add", str(gid)])
            print(f"Protenix 容器使用宿主机用户: {host_uid}:{host_gid}", file=sys.stderr)
        else:
            print("Protenix 容器使用默认 root 用户（官方镜像推荐）", file=sys.stderr)
        print("Protenix 资源模式: host-mounted（源码 + 权重 + common）", file=sys.stderr)
        print(f"Protenix 缓存挂载: {protenix_common_cache_mount} -> /cache/common", file=sys.stderr)
        if protenix_common_overlay_root is not None:
            print(f"Protenix 原始 common cache: {protenix_common_cache_dir}", file=sys.stderr)

        docker_command.extend(extra_args)

        container_checkpoint_path = str(PROTENIX_CONTAINER_CHECKPOINT_PATH or "").strip()
        if not container_checkpoint_path:
            container_checkpoint_path = f"{container_model_dir}/{checkpoint_filename}"

        docker_command.append(image)
        docker_command.extend([
            (PROTENIX_PYTHON_BIN or "python3"),
            f"{container_app_dir}/runner/inference.py",
            "--model_name", model_name,
            "--load_checkpoint_dir", container_model_dir,
            "--load_checkpoint_path", container_checkpoint_path,
            "--input_json_path", "/workspace/protenix_input/input.json",
            "--dump_dir", "/workspace/protenix_output",
            "--need_atom_confidence", "True",
            "--use_msa", "true" if effective_use_msa else "false",
        ])
        if use_template:
            docker_command.extend([
                "--use_template", "True",
                "--data.template.prot_template_mmcif_dir", _PROTENIX_TEMPLATE_CONTAINER_DIR,
                "--data.template.release_dates_path",
                f"{_PROTENIX_INPUT_CONTAINER_DIR}/template_release_dates.json",
            ])
        if seed is not None:
            docker_command.extend(["--seeds", str(int(seed))])
        if linker_codes and os.environ.get("PROTENIX_DISABLE_TFG", "") != "1":
            docker_command.extend(["--sample_diffusion.guidance.enable", "True"])
        docker_command.extend(infer_extra_args)

        display_command = " ".join(shlex.quote(part) for part in docker_command)
        remove_container(task_container_name)

        print(f"运行 Protenix Docker: {display_command}", file=sys.stderr)
        protenix_log_path = str(protenix_results_root / "protenix_docker.log")
        return_code, output_tail_text = run_docker_streaming(docker_command, protenix_log_path)

        _normalize_protenix_output_permissions(
            temp_dir=temp_dir,
            image=image,
            paths=[protenix_input_dir, protenix_output_dir, protenix_log_path],
        )

        if protenix_common_overlay_root is not None and protenix_common_overlay_root.is_dir():
            overlay_unique_bytes = 0
            try:
                for p in protenix_common_overlay_root.rglob("*"):
                    if p.is_file() and not p.is_symlink() and p.stat().st_nlink == 1:
                        overlay_unique_bytes += p.stat().st_size
            except OSError:
                pass
            shutil.rmtree(protenix_common_overlay_root, ignore_errors=True)
            print(
                f"已清理 Protenix common overlay: {protenix_common_overlay_root} "
                f"(独占数据 {overlay_unique_bytes / 1e6:.1f}MB)",
                file=sys.stderr,
            )

        if return_code != 0:
            tail_text = output_tail_text
            traceback_text = ""
            try:
                if os.path.isfile(protenix_log_path):
                    with open(protenix_log_path, "r", encoding="utf-8", errors="replace") as f:
                        full_lines = f.readlines()
                    trace_idx = -1
                    for idx in range(len(full_lines) - 1, -1, -1):
                        if "Traceback (most recent call last):" in full_lines[idx]:
                            trace_idx = idx
                            break
                    if trace_idx >= 0:
                        traceback_text = "".join(full_lines[trace_idx:]).strip()
            except Exception:
                traceback_text = ""

            hint = ""
            if "python: not found" in tail_text or "python3: not found" in tail_text:
                hint = (
                    "\nHint: container Python executable not found. "
                    "Set PROTENIX_PYTHON_BIN (e.g. python3 or python)."
                )
            elif "No module named 'torch'" in tail_text or 'No module named "torch"' in tail_text:
                hint = (
                    "\nHint: torch is missing in the selected container Python env. "
                    "For official Protenix image, keep PROTENIX_USE_HOST_USER=false "
                    "(run as container default root user)."
                )
            traceback_suffix = ""
            if traceback_text:
                traceback_suffix = f"\nTraceback:\n{traceback_text}"
            raise RuntimeError(
                f"Protenix Docker run failed with exit code {return_code}. "
                f"{readable_docker_failure_diagnosis(return_code, tail_text)}\n"
                f"Last output:\n{tail_text}"
                f"{traceback_suffix}"
                f"{hint}\nFull log: {protenix_log_path}"
            )

        _raise_if_protenix_reported_error_without_structure(Path(protenix_output_dir), prep.input_name)

        yaml_data: dict = {}
        try:
            parsed_yaml = yaml.safe_load(yaml_content)
            if isinstance(parsed_yaml, dict):
                yaml_data = parsed_yaml
        except Exception as yaml_err:
            print(f"[WARN] Protenix 亲和力流程解析 YAML 失败，将跳过亲和力预测: {yaml_err}", file=sys.stderr)

        extra_files: list = [(Path(protenix_log_path), "protenix/protenix_docker.log")]
        try:
            ipsae_entries = _run_protenix_ipsae_postprocess(
                postprocess_base=protenix_results_root / "ipsae",
                yaml_data=yaml_data,
                prep=prep,
                protenix_output_dir=Path(protenix_output_dir),
                explicit_ligand_chain=ipsae_ligand_chain_id,
            )
            extra_files.extend(ipsae_entries)
            if ipsae_entries:
                ipsae_mirror_dir = Path(protenix_output_dir) / "ipsae"
                ipsae_mirror_dir.mkdir(parents=True, exist_ok=True)
                for entry_path, _arcname in ipsae_entries:
                    if entry_path.suffix.lower() == ".json":
                        shutil.copyfile(entry_path, ipsae_mirror_dir / entry_path.name)
        except Exception as err:
            print(f"[WARN] 运行 Protenix IPSAE 后处理失败: {err}", file=sys.stderr)
        extra_files.extend(
            run_protenix_affinity_pipeline(
                temp_dir=temp_dir,
                yaml_data=yaml_data,
                prep=prep,
                protenix_output_dir=protenix_output_dir,
                results_root=protenix_results_root,
            )
        )

        _append_custom_residues_ccd_from_molecules(extra_files, custom_molecules, temp_dir, "protenix")

        create_protenix_archive(
            output_archive_path=ctx.output_archive_path,
            protenix_json=protenix_json,
            yaml_content=yaml_content,
            input_name=prep.input_name,
            chain_msa_paths=host_msa_paths_for_archive,
            protenix_output_dir=protenix_output_dir,
            extra_files=extra_files,
        )
