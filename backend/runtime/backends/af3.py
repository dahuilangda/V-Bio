"""AlphaFold3 后端：bond 约束经适配器映射为 bondedAtomPairs。

原 run_alphafold3_backend（run_single_prediction.py）整体迁入：
AF3 JSON 构建、MSA/模板准备、容器执行、ipSAE 与亲和力后处理、归档。
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest


class Af3Backend(StructureBackend):
    name = "alphafold3"
    supported_constraints = frozenset({"bond"})

    def post_validate(self, request: PredictionRequest) -> PredictionRequest:
        from backend.runtime.run_single_prediction import (
            _validate_amidated_terminal_constraints,
        )
        yaml_text = request.source_yaml
        _validate_amidated_terminal_constraints(
            yaml_text, request.raw.get("custom_ccd_molecules") or [])
        return request

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request)
        request = self.post_validate(request)
        yaml_content = ctx.yaml_content
        yaml_data = self._load_yaml_for_affinity(yaml_content)

        from backend.runtime.run_single_prediction import (
            _append_custom_residues_ccd,
            _assert_msa_server_configured,
            _build_custom_ccd_bundle,
            _detect_bicyclic_linker_codes,
            _docker_warn,
            _extract_user_ccd_one_letter_overrides,
            _force_af3_release_date_text,
            _linker_ccd_mmcif_bundle,
            _normalize_custom_ccd_molecules,
            _require_complete_external_msa,
            _run_af3_ipsae_postprocess,
            _sanitize_release_date_text_with_gemmi,
            _validate_amidated_terminal_constraints,
            MSA_CACHE_CONFIG,
            build_af3_fasta,
            build_af3_json,
            build_af3_model_seeds,
            cache_msa_files_from_temp_dir,
            collect_chain_msa_paths,
            create_af3_archive,
            load_unpaired_msa,
            make_scoped_container_name,
            parse_yaml_for_af3,
            remove_container,
            run_af3_affinity_pipeline,
            run_docker_streaming,
            readable_docker_failure_diagnosis,
            validate_af3_database_files,
        )
        from backend.core.config import (
            ALPHAFOLD3_DATABASE_DIR,
            ALPHAFOLD3_DOCKER_EXTRA_ARGS,
            ALPHAFOLD3_DOCKER_IMAGE,
            ALPHAFOLD3_MODEL_DIR,
            COLABFOLD_JOBS_DIR,
            MSA_SERVER_URL,
        )
        from backend.runtime.backends.support import (
            determine_docker_gpu_arg,
            resolve_backend_results_root,
            resolve_backend_work_root,
        )
        from backend.services.common_utils import ProteinMsaMode
        from backend.worker.docker_cmd import (
            collect_gpu_device_group_ids,
            ensure_image,
            sanitize_docker_extra_args,
        )

        seed = ctx.seed or 101
        template_payloads = (ctx.options.get("template_payloads")
                             if isinstance(ctx.options.get("template_payloads"), list) else None)
        custom_ccd_molecules = (ctx.options.get("custom_ccd_molecules")
                                if isinstance(ctx.options.get("custom_ccd_molecules"), list) else [])
        low_vram = bool(ctx.options.get("low_vram", False))

        image = str(ALPHAFOLD3_DOCKER_IMAGE or "jurgjn/alphafold3:v3.0.2").strip()
        ensure_image(image)
        print("Using AlphaFold3 backend (AF3 input preparation)", file=sys.stderr)
        if low_vram:
            raise ValueError(
                "AlphaFold3 不支持低显存模式。如需低显存，请改用 Protenix 或 Boltz2 后端。")
        prep = parse_yaml_for_af3(yaml_content)
        required_chain_ids = [
            chain_id
            for chain_id, mode in prep.chain_id_to_msa_mode.items()
            if mode is not ProteinMsaMode.DISABLED
        ]
        external_chain_ids = [
            chain_id
            for chain_id, mode in prep.chain_id_to_msa_mode.items()
            if mode is ProteinMsaMode.EXTERNAL
        ]
        use_msa_server = bool(external_chain_ids)

        af3_results_root = resolve_backend_results_root(self.name, ctx.task_id, ctx.temp_dir)
        af3_work_root = resolve_backend_work_root(af3_results_root)
        # AF3 ships the full RCSB CCD (NLE/AIB/HYP included); preset modification
        # molecules must not be injected as userCCD (their generated cif lacks the
        # descriptor category and breaks AF3 parsing). Explicit custom molecules
        # (ligands, linkers) still pass through.
        custom_molecules = _normalize_custom_ccd_molecules(custom_ccd_molecules or [])
        _validate_amidated_terminal_constraints(yaml_content, custom_molecules)
        user_ccd_text = None
        if custom_molecules:
            user_ccd_text, _ = _build_custom_ccd_bundle(custom_molecules)
        linker_codes = _detect_bicyclic_linker_codes(yaml_content)
        if linker_codes:
            linker_mmcif = _linker_ccd_mmcif_bundle(linker_codes)
            user_ccd_text = f"{user_ccd_text}\n{linker_mmcif}" if user_ccd_text else linker_mmcif

        if use_msa_server:
            msa_server_url = _assert_msa_server_configured("alphafold3")
            print(f"开始使用 MSA 服务器生成多序列比对: {msa_server_url}", file=sys.stderr)
            _require_complete_external_msa(
                yaml_content, str(af3_work_root), "AlphaFold3", msa_mode="auto")
            print("MSA 生成成功，将用于 AF3 输入", file=sys.stderr)
            if MSA_CACHE_CONFIG['enable_cache']:
                cache_msa_files_from_temp_dir(str(af3_work_root), yaml_content)
        else:
            print("AlphaFold3 输入不需要外部 MSA 生成。", file=sys.stderr)

        cache_dir = MSA_CACHE_CONFIG['cache_dir'] if MSA_CACHE_CONFIG['enable_cache'] else None
        chain_msa_paths = collect_chain_msa_paths(prep, str(af3_work_root), cache_dir)
        missing_chain_ids = [c for c in required_chain_ids if c not in chain_msa_paths]
        if missing_chain_ids:
            raise RuntimeError(
                f"AlphaFold3 MSA assignment incomplete; missing chains: "
                f"{', '.join(sorted(set(missing_chain_ids)))}")
        unpaired_msa = load_unpaired_msa(prep, chain_msa_paths)

        fasta_content = build_af3_fasta(prep)
        model_seeds = build_af3_model_seeds(seed)
        af3_json = build_af3_json(
            prep,
            unpaired_msa,
            use_external_msa=True,
            model_seeds=model_seeds,
            user_ccd=user_ccd_text,
        )

        af3_ccd_one_letter_overrides = _extract_user_ccd_one_letter_overrides(user_ccd_text)

        if template_payloads:
            for tpl in template_payloads:
                mmcif_text = tpl.get("mmcif")
                if mmcif_text:
                    tpl["mmcif"] = _sanitize_release_date_text_with_gemmi(
                        mmcif_text, None, include_loops=True)
                    tpl["mmcif"] = _force_af3_release_date_text(tpl["mmcif"], None)
            for entry in af3_json.get("sequences", []):
                protein = entry.get("protein")
                if not isinstance(protein, dict):
                    continue
                ids = protein.get("id", [])
                if isinstance(ids, str):
                    ids = [ids]
                for tpl in template_payloads:
                    target_ids = tpl.get("target_chain_ids") or []
                    if not target_ids:
                        continue
                    if not set(ids).intersection(target_ids):
                        continue
                    if not tpl.get("queryIndices") or not tpl.get("templateIndices"):
                        continue
                    protein.setdefault("templates", []).append({
                        "mmcif": tpl["mmcif"],
                        "queryIndices": tpl["queryIndices"],
                        "templateIndices": tpl["templateIndices"],
                    })
                    # AF3 requires MSA fields to be set when templates are provided
                    protein.setdefault("unpairedMsa", "")
                    protein.setdefault("pairedMsa", "")

        input_dir = str(af3_work_root / "input")
        output_dir = str(af3_results_root / "output")
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        fasta_path = os.path.join(input_dir, f"{prep.jobname}_input.fasta")
        json_path = os.path.join(input_dir, "fold_input.json")
        sitecustomize_path = os.path.join(input_dir, "sitecustomize.py")

        with open(fasta_path, "w") as fasta_file:
            fasta_file.write(fasta_content)
        with open(json_path, "w") as json_file:
            json.dump(af3_json, json_file, indent=2, ensure_ascii=False)

        with open(sitecustomize_path, "w", encoding="utf-8") as sc_file:
            sc_file.write(_SITECUSTOMIZE_CODE)

        model_dir = ALPHAFOLD3_MODEL_DIR
        database_dir = ALPHAFOLD3_DATABASE_DIR
        docker_image = image
        raw_extra_args = shlex.split(ALPHAFOLD3_DOCKER_EXTRA_ARGS) if ALPHAFOLD3_DOCKER_EXTRA_ARGS else []
        extra_args = sanitize_docker_extra_args(raw_extra_args, log=_docker_warn)
        if raw_extra_args and len(extra_args) != len(raw_extra_args):
            print(
                f"[WARN] 已忽略部分 ALPHAFOLD3_DOCKER_EXTRA_ARGS 参数，原始值: {raw_extra_args}",
                file=sys.stderr,
            )

        if not model_dir or not os.path.isdir(model_dir):
            raise FileNotFoundError("ALPHAFOLD3_MODEL_DIR 未配置或目录不存在，无法运行 AlphaFold3 容器。")
        if not database_dir or not os.path.isdir(database_dir):
            raise FileNotFoundError("ALPHAFOLD3_DATABASE_DIR 未配置或目录不存在，无法运行 AlphaFold3 容器。")
        validate_af3_database_files(database_dir)

        visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
        try:
            gpu_arg = determine_docker_gpu_arg(visible_devices)
        except RuntimeError as gpu_err:
            print(f"[ERROR] 无法准备 AlphaFold3 GPU 环境: {gpu_err}", file=sys.stderr)
            print("   ↳ 请确认此主机安装了 NVIDIA 驱动并正确设置 CUDA_VISIBLE_DEVICES。", file=sys.stderr)
            raise

        container_input_dir = "/workspace/af_input"
        container_output_dir = "/workspace/af_output"
        container_model_dir = "/workspace/models"
        container_database_dir = "/workspace/public_databases"
        container_cache_dir = "/workspace/af_cache"
        container_colabfold_jobs_dir = "/app/jobs"
        runtime_task_id = str(ctx.task_id or os.environ.get("VBIO_TASK_ID") or "").strip()
        task_container_name = make_scoped_container_name(runtime_task_id, prefix="boltz-af3")

        runtime_overridden = any(token == "--runtime" for token in extra_args)

        docker_command = ["docker", "run", "--rm"]
        if task_container_name:
            docker_command.extend(["--name", task_container_name])
            docker_command.extend(["--label", f"boltz.task_id={runtime_task_id}"])
            docker_command.extend(["--label", "boltz.runtime=alphafold3"])
        if not runtime_overridden:
            docker_command.extend(["--runtime", "nvidia"])

        docker_command.extend([
            "--gpus", gpu_arg,
            "--env", "PYTHONPATH=/workspace/af_input",
            "--volume", f"{input_dir}:{container_input_dir}",
            "--volume", f"{output_dir}:{container_output_dir}",
            "--volume", f"{model_dir}:{container_model_dir}",
            "--volume", f"{database_dir}:{container_database_dir}",
        ])
        if af3_ccd_one_letter_overrides:
            docker_command.extend([
                "--env",
                "VBIO_AF3_CCD_ONE_LETTER_OVERRIDES=" + json.dumps(
                    af3_ccd_one_letter_overrides, sort_keys=True),
            ])

        jax_cache_host_dir = os.environ.get("ALPHAFOLD3_JAX_CACHE_DIR")
        if not jax_cache_host_dir:
            jax_cache_host_dir = os.path.join(os.getcwd(), ".af3_jax_cache")
        try:
            os.makedirs(jax_cache_host_dir, exist_ok=True)
            docker_command.extend([
                "--env",
                f"JAX_COMPILATION_CACHE_DIR={container_cache_dir}",
                "--volume",
                f"{jax_cache_host_dir}:{container_cache_dir}",
            ])
        except Exception as exc:
            print(f"[WARN] 无法创建 JAX 编译缓存目录 {jax_cache_host_dir}: {exc}", file=sys.stderr)

        if use_msa_server and MSA_SERVER_URL and COLABFOLD_JOBS_DIR and os.path.exists(COLABFOLD_JOBS_DIR):
            docker_command.extend([
                "--volume",
                f"{COLABFOLD_JOBS_DIR}:{container_colabfold_jobs_dir}",
            ])
            print(f"挂载 ColabFold jobs 目录: {COLABFOLD_JOBS_DIR} -> {container_colabfold_jobs_dir}", file=sys.stderr)
        elif use_msa_server:
            print("[WARN] 未找到 ColabFold jobs 目录或未配置 MSA 服务器", file=sys.stderr)
        else:
            print("未启用外部 MSA，跳过 ColabFold jobs 目录挂载", file=sys.stderr)

        host_uid = os.getuid()
        host_gid = os.getgid()
        docker_command += ["--user", f"{host_uid}:{host_gid}"]

        gpu_device_groups = collect_gpu_device_group_ids()
        if not gpu_device_groups:
            print("[WARN] 未能检测到 GPU 设备的所属用户组，容器可能无法访问 GPU。", file=sys.stderr)
        else:
            for gid in gpu_device_groups:
                docker_command.extend(["--group-add", str(gid)])
            print(
                f"为容器添加 GPU 相关用户组: {', '.join(str(g) for g in gpu_device_groups)}",
                file=sys.stderr,
            )

        docker_command.extend(extra_args)
        docker_command.append(docker_image)
        docker_command.extend([
            "python",
            "run_alphafold.py",
            f"--json_path={container_input_dir}/fold_input.json",
            f"--model_dir={container_model_dir}",
            f"--output_dir={container_output_dir}",
            f"--db_dir={container_database_dir}",
        ])

        display_command = " ".join(shlex.quote(part) for part in docker_command)
        remove_container(task_container_name)
        print(f"运行 AlphaFold3 Docker: {display_command}", file=sys.stderr)
        af3_log_path = str(af3_results_root / "af3_docker.log")
        return_code, tail_text = run_docker_streaming(docker_command, af3_log_path)

        if return_code != 0:
            print(f"[ERROR] AlphaFold3 Docker 运行失败: {tail_text}", file=sys.stderr)
            raise RuntimeError(
                f"AlphaFold3 Docker run failed with exit code {return_code}. "
                f"{readable_docker_failure_diagnosis(return_code, tail_text)}\n"
                f"Last output:\n{tail_text}\n"
                f"Full log: {af3_log_path}"
            )

        print(f"AlphaFold3 Docker 运行完成，日志已保存: {af3_log_path}", file=sys.stderr)

        af3_output_contents = list(Path(output_dir).rglob("*"))
        if not any(p.is_file() for p in af3_output_contents):
            print("[WARN] AlphaFold3 输出目录为空，可能推理未产生结果。", file=sys.stderr)

        extra_archive_files: list[tuple[Path, str]] = []
        try:
            extra_archive_files.extend(
                _run_af3_ipsae_postprocess(
                    postprocess_base=af3_results_root / "ipsae",
                    yaml_data=yaml_data,
                    prep=prep,
                    af3_output_dir=Path(output_dir),
                )
            )
        except Exception as err:
            print(f"[WARN] 运行 AlphaFold3 IPSAE 后处理失败: {err}", file=sys.stderr)
        extra_archive_files.extend(
            run_af3_affinity_pipeline(
                temp_dir=ctx.temp_dir,
                yaml_data=yaml_data,
                prep=prep,
                af3_output_dir=output_dir,
                results_root=af3_results_root,
            )
        )

        _append_custom_residues_ccd(extra_archive_files, user_ccd_text, ctx.temp_dir, "af3")

        create_af3_archive(
            ctx.output_archive_path,
            fasta_content,
            af3_json,
            chain_msa_paths,
            yaml_content,
            prep,
            af3_output_dir=output_dir,
            extra_files=extra_archive_files,
        )

    def _load_yaml_for_affinity(self, yaml_content: str) -> dict:
        import yaml as _yaml
        try:
            return _yaml.safe_load(yaml_content) or {}
        except _yaml.YAMLError as err:
            print(f"[WARN] 无法解析 YAML，亲和力后处理将被跳过: {err}", file=sys.stderr)
            return {}
