"""Boltz-2 后端：平台 yaml 原生透传（bond/contact/pocket）。

原 run_boltz_backend（run_single_prediction.py）整体迁入：
约束管线后处理、MSA 触发、CLI 组装、容器执行、ipSAE 后处理、归档。
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Optional

import yaml

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest


class BoltzBackend(StructureBackend):
    name = "boltz"

    def post_validate(self, request: PredictionRequest) -> PredictionRequest:
        from backend.runtime.constraints_pipeline import (
            normalize_ligand_chain_collisions,
            remap_constraints_by_template_alignment,
            sanitize_constraints_for_chain_lengths,
            validate_unique_sequence_chain_ids,
        )
        from backend.runtime.run_single_prediction import (
            _validate_amidated_terminal_constraints,
        )
        yaml_text = normalize_ligand_chain_collisions(request.source_yaml)
        validate_unique_sequence_chain_ids(yaml_text)
        yaml_text = remap_constraints_by_template_alignment(yaml_text)
        validate_unique_sequence_chain_ids(yaml_text)
        yaml_text = sanitize_constraints_for_chain_lengths(yaml_text)
        _validate_amidated_terminal_constraints(
            yaml_text, request.custom_ccd_molecules)
        return PredictionRequest.from_yaml(yaml_text)

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request, ctx.options.get("custom_ccd_molecules"))
        request = self.post_validate(request)
        normalized_yaml = request.source_yaml

        from backend.core.config import (
            BASE_DIR,
            BOLTZ2_CONTAINER_CACHE_DIR,
            BOLTZ2_DOCKER_EXTRA_ARGS,
            BOLTZ2_DOCKER_IMAGE,
            BOLTZ2_DOCKER_SHM_SIZE,
            BOLTZ2_HOST_CACHE_DIR,
        )
        from backend.runtime.constraints_pipeline import print_constraint_residue_summary
        from backend.runtime.custom_ccd_builder import (
            apply_custom_ccd_code_remap,
            resolve_custom_ccd_code_collisions,
        )
        from backend.runtime.msa_pipeline import cache_msa_files_from_temp_dir
        from backend.runtime.run_single_prediction import (
            _append_custom_residues_ccd_from_molecules,
            _assert_msa_server_configured,
            _docker_warn,
            _inject_local_msa_paths_into_yaml,
            _merge_referenced_preset_modification_molecules,
            _normalize_custom_ccd_molecules,
            _prepare_task_boltz_custom_mols_dir,
            _require_complete_external_msa,
            _run_boltz_ipsae_postprocess,
            assert_boltz_preprocessing_succeeded,
            coerce_bool,
            create_archive_with_a3m,
            find_results_dir,
            infer_use_msa_server_from_yaml_text,
        )
        from backend.runtime.backends.support import (
            determine_docker_gpu_arg,
            resolve_backend_results_root,
            resolve_backend_work_root,
        )
        from backend.runtime.docker_exec import remove_container, run_docker_streaming
        from backend.worker.docker_cmd import (
            collect_gpu_device_group_ids,
            docker_args_has_flag,
            ensure_image,
            make_scoped_container_name,
            readable_docker_failure_diagnosis,
            sanitize_docker_extra_args,
        )

        predict_args = ctx.options.get("predict_args") or {}
        model_name = ctx.options.get("model_name")
        strict_ligand_confidence_contract = bool(
            ctx.options.get("strict_ligand_confidence_contract", False))
        custom_ccd_molecules = (ctx.options.get("custom_ccd_molecules")
                                if isinstance(ctx.options.get("custom_ccd_molecules"), list) else [])
        low_vram = bool(ctx.options.get("low_vram", False))
        ipsae_ligand_chain_id = ctx.options.get("ipsae_ligand_chain_id")

        ensure_image(str(BOLTZ2_DOCKER_IMAGE or "").strip())
        print_constraint_residue_summary(normalized_yaml)

        cli_args = dict(predict_args)
        requested_use_msa = coerce_bool(cli_args.pop("use_msa_server", None), False)
        cli_args.pop("msa_server_url", None)
        # msa_mode 是 protenix2dock 的能力开关；Boltz CLI 无此选项
        cli_args.pop("msa_mode", None)
        cli_args.pop("notify_email", None)
        cli_args.pop("project_id", None)
        requires_external_msa = infer_use_msa_server_from_yaml_text(normalized_yaml)
        cli_args.pop("low_vram", None)
        if model_name:
            cli_args['model'] = model_name
        if strict_ligand_confidence_contract:
            cli_args["strict_ligand_confidence_contract"] = True
        if low_vram:
            # 大复合物的两个显存峰值开关：串行扩散样本 + bf16-mixed 精度
            cli_args["max_parallel_samples"] = 1
            if not cli_args.get("trainer_precision"):
                cli_args["trainer_precision"] = "bf16-mixed"

        if 'diffusion_samples' not in cli_args or cli_args['diffusion_samples'] is None:
            effective_model = str(cli_args.get('model') or model_name or 'boltz2').lower()
            if effective_model == 'boltz2':
                cli_args['diffusion_samples'] = 5
        if 'trainer_precision' not in cli_args or cli_args['trainer_precision'] is None:
            effective_model = str(cli_args.get('model') or model_name or 'boltz2').lower()
            if effective_model == 'boltz2':
                cli_args['trainer_precision'] = '32'

        results_root = resolve_backend_results_root(self.name, ctx.task_id, ctx.temp_dir)
        work_root = resolve_backend_work_root(results_root)

        if requires_external_msa:
            msa_server_url = _assert_msa_server_configured("boltz")
            if not requested_use_msa:
                print("Boltz2 输入缺少 MSA，已启用外部 MSA。", file=sys.stderr)
            print(f"开始使用 MSA 服务器生成多序列比对: {msa_server_url}", file=sys.stderr)
            _require_complete_external_msa(
                normalized_yaml, str(work_root), "Boltz2",
                msa_mode=str(predict_args.get("msa_mode") or "auto"))
            print("MSA 生成成功，将用于结构预测", file=sys.stderr)
            normalized_yaml, injected_count = _inject_local_msa_paths_into_yaml(
                normalized_yaml, str(work_root))
            if injected_count > 0:
                print(f"Injected local MSA paths into YAML: {injected_count}", file=sys.stderr)
            cli_args['use_msa_server'] = True
            cli_args['msa_server_url'] = msa_server_url
        else:
            print("Boltz2 输入已禁用或提供 MSA，跳过外部 MSA 生成。", file=sys.stderr)

        tmp_yaml_path = str(work_root / 'data.yaml')
        with open(tmp_yaml_path, 'w') as tmp_yaml:
            tmp_yaml.write(normalized_yaml)
        cli_args['data'] = tmp_yaml_path
        cli_args['out_dir'] = str(results_root)

        POSITIONAL_KEYS = ['data']
        cmd_positional = []
        cmd_options = []
        for key, value in cli_args.items():
            if key in POSITIONAL_KEYS:
                cmd_positional.append(str(value))
            elif value is None:
                continue
            elif isinstance(value, bool):
                if value:
                    cmd_options.append(f'--{key}')
            else:
                cmd_options.append(f'--{key}')
                cmd_options.append(str(value))
        cmd_args = cmd_positional + cmd_options

        visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
        try:
            gpu_arg = determine_docker_gpu_arg(visible_devices)
        except RuntimeError as gpu_err:
            print(f"[boltz2] GPU env unavailable: {gpu_err}", file=sys.stderr)
            raise

        image = (BOLTZ2_DOCKER_IMAGE or "").strip()
        if not image:
            raise RuntimeError("BOLTZ2_DOCKER_IMAGE 未配置，无法运行 Boltz2 Docker。")

        raw_extra_args = shlex.split(BOLTZ2_DOCKER_EXTRA_ARGS) if BOLTZ2_DOCKER_EXTRA_ARGS else []
        extra_args = sanitize_docker_extra_args(raw_extra_args, log=_docker_warn)
        if raw_extra_args and len(extra_args) != len(raw_extra_args):
            print(
                f"[WARN] 已忽略部分 BOLTZ2_DOCKER_EXTRA_ARGS 参数，原始值: {raw_extra_args}",
                file=sys.stderr,
            )
        shm_size = str(BOLTZ2_DOCKER_SHM_SIZE or "").strip()

        runtime_task_id = str(os.environ.get("VBIO_TASK_ID") or "").strip()
        task_container_name = make_scoped_container_name(runtime_task_id, prefix="boltz-af3")
        runtime_overridden = any(token == "--runtime" for token in extra_args)

        docker_command = ["docker", "run", "--rm"]
        if task_container_name:
            docker_command.extend(["--name", task_container_name])
            docker_command.extend(["--label", f"boltz.task_id={runtime_task_id}"])
            docker_command.extend(["--label", "boltz.runtime=boltz2"])
        if not runtime_overridden:
            docker_command.extend(["--runtime", "nvidia"])
        if shm_size and not docker_args_has_flag(extra_args, "--shm-size") \
                and not docker_args_has_flag(extra_args, "--ipc"):
            docker_command.extend(["--shm-size", shm_size])

        docker_command.extend([
            "--gpus", gpu_arg,
            "--volume", f"{ctx.temp_dir}:{ctx.temp_dir}",
            "--volume", f"{results_root}:{results_root}",
            "--volume", f"{BASE_DIR}:/workspace/vbio:ro",
            "--workdir", "/workspace/vbio",
            "--env", "PYTHONPATH=/workspace/vbio",
        ])

        passthrough_env_keys = [
            "VBIO_DOWNLOAD_RETRIES",
            "BOLTZ_CCD_URL",
            "BOLTZ1_MODEL_URL",
            "BOLTZ2_MOLS_URL",
            "BOLTZ2_MODEL_URL",
            "BOLTZ2_AFFINITY_MODEL_URL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ]
        for env_key in passthrough_env_keys:
            env_val = str(os.environ.get(env_key, "") or "").strip()
            if env_val:
                docker_command.extend(["--env", f"{env_key}={env_val}"])
        if runtime_task_id:
            docker_command.extend(["--env", f"VBIO_TASK_ID={runtime_task_id}"])

        custom_molecules = _merge_referenced_preset_modification_molecules(
            _normalize_custom_ccd_molecules(custom_ccd_molecules or []),
            normalized_yaml,
        )
        host_cache_dir = str(BOLTZ2_HOST_CACHE_DIR or "").strip()
        container_cache_dir = str(BOLTZ2_CONTAINER_CACHE_DIR or "/root/.boltz").strip() or "/root/.boltz"
        container_base_mols_dir = "/root/.boltz_base_mols"
        if custom_molecules and host_cache_dir:
            base_mols_dir = Path(host_cache_dir) / "mols"
            base_mol_codes = {path.stem.upper() for path in base_mols_dir.glob("*.pkl")}
            custom_molecules, ccd_remap = resolve_custom_ccd_code_collisions(
                custom_molecules, base_mol_codes)
            if ccd_remap:
                normalized_yaml = apply_custom_ccd_code_remap(normalized_yaml, ccd_remap)
                print(f"[WARN] 自定义 CCD 编码与标准冲突，已自动重命名: {ccd_remap}",
                      file=sys.stderr)
        custom_mols_dir: Optional[Path] = None
        if custom_molecules and not host_cache_dir:
            raise RuntimeError("BOLTZ2_HOST_CACHE_DIR is required when using custom drawn residue CCD molecules.")
        if custom_molecules:
            custom_mols_dir = _prepare_task_boltz_custom_mols_dir(
                ctx.temp_dir,
                custom_molecules,
                Path(host_cache_dir) / "mols",
                container_base_mols_dir,
            )

        if host_cache_dir:
            os.makedirs(host_cache_dir, exist_ok=True)
            docker_command.extend(["--volume", f"{host_cache_dir}:{container_cache_dir}"])
            docker_command.extend(["--env", f"BOLTZ_CACHE={container_cache_dir}"])
            if custom_mols_dir:
                docker_command.extend(["--volume", f"{Path(host_cache_dir) / 'mols'}:{container_base_mols_dir}:ro"])
                docker_command.extend(["--volume", f"{custom_mols_dir}:{container_cache_dir}/mols:ro"])

        host_uid = os.getuid()
        host_gid = os.getgid()
        docker_command.extend(["--user", f"{host_uid}:{host_gid}"])
        for gid in collect_gpu_device_group_ids():
            docker_command.extend(["--group-add", str(gid)])

        docker_command.extend(extra_args)
        docker_command.append(image)
        docker_command.extend([
            "python",
            "/workspace/vbio/backend/runtime/boltz_wrapper.py",
            "predict",
            *cmd_args,
        ])

        remove_container(task_container_name)

        display_command = " ".join(shlex.quote(part) for part in docker_command)
        print(f"运行 Boltz2 Docker: {display_command}", file=sys.stderr)

        boltz_log_path = str(results_root / "boltz2_docker.log")
        return_code, tail_text = run_docker_streaming(docker_command, boltz_log_path)

        if return_code != 0:
            raise RuntimeError(
                f"Boltz2 Docker run failed with exit code {return_code}. "
                f"{readable_docker_failure_diagnosis(return_code, tail_text)}\n"
                f"Last output:\n{tail_text}\n"
                f"Full log: {boltz_log_path}"
            )
        print(f"Boltz2 Docker 运行完成，日志已保存: {boltz_log_path}", file=sys.stderr)

        cache_msa_files_from_temp_dir(str(work_root), normalized_yaml)
        assert_boltz_preprocessing_succeeded(str(results_root), normalized_yaml)

        output_directory_path = find_results_dir(str(results_root))
        if not os.listdir(output_directory_path):
            raise NotADirectoryError(
                f"Prediction result directory was found but is empty: {output_directory_path}"
            )

        extra_archive_files: list[tuple[Path, str]] = []
        try:
            parsed_yaml = yaml.safe_load(normalized_yaml)
            boltz_yaml_data = parsed_yaml if isinstance(parsed_yaml, dict) else {}
        except Exception as yaml_err:
            print(f"[WARN] Boltz IPSAE 后处理解析 YAML 失败，将跳过 IPSAE: {yaml_err}", file=sys.stderr)
            boltz_yaml_data = {}
        try:
            extra_archive_files.extend(
                _run_boltz_ipsae_postprocess(
                    postprocess_base=results_root / "ipsae",
                    results_dir=Path(output_directory_path),
                    yaml_data=boltz_yaml_data,
                    explicit_ligand_chain=ipsae_ligand_chain_id,
                )
            )
        except Exception as err:
            print(f"[WARN] 运行 Boltz IPSAE 后处理失败: {err}", file=sys.stderr)

        _append_custom_residues_ccd_from_molecules(extra_archive_files, custom_molecules, ctx.temp_dir, "boltz")

        create_archive_with_a3m(
            ctx.output_archive_path,
            output_directory_path,
            normalized_yaml,
            extra_files=extra_archive_files,
        )
