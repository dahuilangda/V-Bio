from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gemmi

from backend.core.config import (
    BASE_DIR,
    BOLTZ2SCORE_SCRIPT,
    BOLTZ2_CONTAINER_CACHE_DIR,
    BOLTZ2_DOCKER_EXTRA_ARGS,
    BOLTZ2_DOCKER_IMAGE,
    BOLTZ2_DOCKER_SHM_SIZE,
    BOLTZ2_HOST_CACHE_DIR,
)
from backend.runtime.backends.support import determine_docker_gpu_arg
from backend.runtime.design_constants import AMINO_ACID_MAPPING
from backend.runtime.docker_exec import remove_container
from backend.runtime.ipsae_postprocess import (
    _find_first_existing,
    _iter_affinity_entries,
    _load_json_object,
    _tail_lines,
)
from backend.worker.docker_cmd import (
    _docker_warn,
    collect_gpu_device_group_ids,
    docker_args_has_flag,
    ensure_image,
    make_scoped_container_name,
    sanitize_docker_extra_args,
)


def extract_affinity_config_from_yaml(yaml_data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    for entry in _iter_affinity_entries(yaml_data.get("properties")):
        affinity_info = entry.get("affinity")

        if isinstance(affinity_info, dict):
            binder = affinity_info.get("binder") or affinity_info.get("chain")
            if binder:
                return {"binder": str(binder).strip()}

        elif affinity_info is True:
            binder = entry.get("binder") or entry.get("chain")
            if binder:
                return {"binder": str(binder).strip()}

            for other_entry in _iter_affinity_entries(yaml_data.get("properties")):
                binder = other_entry.get("binder") or other_entry.get("chain")
                if binder:
                    return {"binder": str(binder).strip()}

    return None


def _legacy_parse_ligand_from_text(cif_path: Path, binder_chain: str) -> Optional[str]:
    try:
        with cif_path.open("r") as cif_file:
            for line in cif_file:
                if not line.startswith("HETATM"):
                    continue
                parts = line.split()
                if len(parts) < 7:
                    continue
                comp_id = parts[5]
                chain_id = parts[6]
                if chain_id == binder_chain:
                    return comp_id
    except OSError as err:
        print(f"[WARN] 无法读取 CIF 文件 {cif_path}: {err}", file=sys.stderr)
    return None


def find_ligand_resname_in_cif(cif_path: Path, binder_chain: str) -> Optional[str]:
    try:
        import gemmi  # type: ignore
    except ImportError:
        return _legacy_parse_ligand_from_text(cif_path, binder_chain)

    try:
        structure = gemmi.read_structure(str(cif_path))
    except Exception as err:
        print(f"[WARN] 无法使用 gemmi 解析 {cif_path}: {err}", file=sys.stderr)
        return _legacy_parse_ligand_from_text(cif_path, binder_chain)

    for model in structure:
        chain = next((ch for ch in model if ch.name == binder_chain), None)
        if chain is None:
            continue
        for residue in chain:
            resname = residue.name.strip()
            if resname:
                return resname
    return None


def _sanitize_atom_name_for_affinity(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        return name

    sanitized_chars: List[str] = []
    for ch in cleaned:
        if ch.isalpha():
            sanitized_chars.append(ch.upper())
        elif ch.isdigit():
            sanitized_chars.append(ch)
        else:
            sanitized_chars.append('X')

    sanitized = ''.join(sanitized_chars)
    return sanitized or name


def prepare_structure_for_affinity(source_path: Path, work_dir: Path) -> Path:
    try:
        import gemmi  # type: ignore
    except ImportError:
        print(
            "[WARN] 未安装 gemmi，无法清理结构原子名，直接使用原始结构。",
            file=sys.stderr,
        )
        return source_path

    try:
        structure = gemmi.read_structure(str(source_path))
    except Exception as err:
        print(f"[WARN] 无法读取结构 {source_path} 进行清理: {err}", file=sys.stderr)
        return source_path

    changed = False
    for model in structure:
        for chain in model:
            for residue in chain:
                for atom in residue:
                    sanitized = _sanitize_atom_name_for_affinity(atom.name)
                    if sanitized != atom.name:
                        atom.name = sanitized
                        changed = True

    if not changed:
        return source_path

    work_dir.mkdir(parents=True, exist_ok=True)
    sanitized_path = work_dir / f"{source_path.stem}_sanitized{source_path.suffix}"

    try:
        if source_path.suffix.lower() == '.cif':
            doc = structure.make_mmcif_document()
            doc.write_file(str(sanitized_path))
        else:
            structure.write_minimal_pdb(str(sanitized_path))
    except Exception as err:
        print(f"[WARN] 写入清理后的结构失败，回退到原始结构: {err}", file=sys.stderr)
        return source_path

    print(
        f"已生成用于亲和力预测的清理结构: {sanitized_path}",
        file=sys.stderr,
    )
    return sanitized_path


def _stage_structure_for_affinity_container(source_path: Path, work_dir: Path) -> Path:
    source_path = Path(source_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    source_resolved = source_path.resolve()
    work_dir_resolved = work_dir.resolve()
    try:
        source_resolved.relative_to(work_dir_resolved)
        return source_path
    except ValueError:
        pass

    staged_path = work_dir / source_path.name
    shutil.copy2(source_path, staged_path)
    print(
        f"已将亲和力评分输入暂存到 Boltz2Score 容器挂载目录: {staged_path}",
        file=sys.stderr,
    )
    return staged_path


def _is_protein_like_residue_name(resname: str) -> bool:
    normalized = str(resname or "").strip().upper()
    return normalized in AMINO_ACID_MAPPING


def _infer_affinity_chain_plan(
    structure_path: Path,
    requested_ligand_chain: str,
) -> Optional[Dict[str, Any]]:
    solvent_names = {"HOH", "WAT", "H2O", "DOD"}
    ligand_chain_requested = str(requested_ligand_chain or "").strip()
    if not ligand_chain_requested:
        return None

    try:
        structure = gemmi.read_structure(str(structure_path))
    except Exception as err:
        print(f"[WARN] 无法解析结构以推断 affinity 链信息: {err}", file=sys.stderr)
        return None

    resolved_ligand_chain: Optional[str] = None
    target_chain_ids: List[str] = []

    for model in structure:
        for chain in model:
            chain_name = str(chain.name or "").strip()
            if not chain_name:
                continue
            residue_names = [
                str(residue.name or "").strip().upper()
                for residue in chain
                if str(residue.name or "").strip()
            ]
            if not residue_names:
                continue

            has_protein = any(_is_protein_like_residue_name(name) for name in residue_names)
            has_non_solvent_nonpolymer = any(
                name not in solvent_names and not _is_protein_like_residue_name(name)
                for name in residue_names
            )

            if chain_name == ligand_chain_requested:
                if has_non_solvent_nonpolymer:
                    resolved_ligand_chain = chain_name
                continue

            if has_protein:
                target_chain_ids.append(chain_name)

    if not resolved_ligand_chain:
        return None
    if not target_chain_ids:
        return None

    return {
        "ligand_chain": resolved_ligand_chain,
        "target_chain_ids": target_chain_ids,
    }


def _extract_ligand_smiles_list(yaml_data: Dict[str, Any]) -> List[str]:
    smiles_list: List[str] = []
    for entry in (yaml_data or {}).get("sequences") or []:
        if not isinstance(entry, dict) or "ligand" not in entry:
            continue
        ligand = entry["ligand"]
        explicit_smiles = False
        if isinstance(ligand, dict):
            if "smiles" in ligand:
                ligand = ligand.get("smiles")
                explicit_smiles = True
            else:
                ligand = ligand.get("ligand")
        if not isinstance(ligand, str):
            continue
        ligand = ligand.strip()
        if ligand and (explicit_smiles or not re.fullmatch(r"[A-Z0-9]{1,5}", ligand)):
            smiles_list.append(ligand)
    return smiles_list


def _run_boltz2score_affinity_postprocess(
    *,
    affinity_base: Path,
    model_path: Path,
    requested_ligand_chain: str,
    ligand_resname: Optional[str],
    source: str,
    archive_prefix: str,
    ligand_smiles_list: Optional[List[str]] = None,
) -> List[Tuple[Path, str]]:
    output_dir = affinity_base / "boltz2score_output"
    work_dir = affinity_base / "boltz2score_work"
    sanitized_struct_dir = affinity_base / "sanitized_structures"
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    sanitized_struct_dir.mkdir(parents=True, exist_ok=True)

    model_for_affinity = _stage_structure_for_affinity_container(
        prepare_structure_for_affinity(model_path, sanitized_struct_dir),
        sanitized_struct_dir,
    )
    chain_plan = _infer_affinity_chain_plan(model_for_affinity, requested_ligand_chain)
    if not chain_plan:
        print(
            f"[WARN] 无法从结构中解析 affinity 所需的 target/ligand 链，跳过亲和力预测: {model_for_affinity}",
            file=sys.stderr,
        )
        return []

    resolved_ligand_chain = str(chain_plan["ligand_chain"])
    target_chain_ids = [
        str(chain_id).strip()
        for chain_id in chain_plan["target_chain_ids"]
        if str(chain_id).strip()
    ]
    if not target_chain_ids:
        print("[WARN] 未识别到蛋白 target 链，跳过亲和力预测。", file=sys.stderr)
        return []

    print(
        "开始运行 Boltz2Score 亲和力后处理，"
        f"target链: {','.join(target_chain_ids)}, 配体链: {resolved_ligand_chain}",
        file=sys.stderr,
    )

    score_cmd = [
        "python",
        BOLTZ2SCORE_SCRIPT,
        "--output_dir",
        str(output_dir),
        "--work_dir",
        str(work_dir),
        "--accelerator",
        "gpu",
        "--devices",
        "1",
        "--num_workers",
        "0",
        "--mode",
        "score",
        "--compute_ipsae",
        "--input",
        str(model_for_affinity),
        "--enable_affinity",
        "--target_chain",
        ",".join(target_chain_ids),
        "--ligand_chain",
        resolved_ligand_chain,
        "--no_kernels",
    ]
    if ligand_smiles_list and ligand_resname:
        m = re.fullmatch(r"l(\d+)", ligand_resname, re.IGNORECASE)
        smiles_idx = int(m.group(1)) - 1 if m else None
        smiles = (
            ligand_smiles_list[smiles_idx]
            if smiles_idx is not None and 0 <= smiles_idx < len(ligand_smiles_list)
            else None
        )
        if smiles:
            score_cmd.extend([
                "--ligand_smiles_map",
                json.dumps({f"{resolved_ligand_chain}:{ligand_resname}": smiles}),
            ])

    try:
        gpu_arg = determine_docker_gpu_arg(os.environ.get("CUDA_VISIBLE_DEVICES"))
    except RuntimeError as err:
        print(f"[WARN] 无法准备 Boltz2Score GPU 环境，跳过亲和力预测: {err}", file=sys.stderr)
        return []

    image = str(BOLTZ2_DOCKER_IMAGE or "").strip()
    if not image:
        raise RuntimeError("BOLTZ2_DOCKER_IMAGE 未配置，无法运行 affinity 后处理 Boltz2Score。")
    ensure_image(image)

    raw_extra_args = shlex.split(BOLTZ2_DOCKER_EXTRA_ARGS) if BOLTZ2_DOCKER_EXTRA_ARGS else []
    extra_args = sanitize_docker_extra_args(raw_extra_args, log=_docker_warn)
    if raw_extra_args and len(extra_args) != len(raw_extra_args):
        print(
            f"[WARN] 已忽略部分 BOLTZ2_DOCKER_EXTRA_ARGS 参数，原始值: {raw_extra_args}",
            file=sys.stderr,
        )
    shm_size = str(BOLTZ2_DOCKER_SHM_SIZE or "").strip()

    runtime_task_id = str(os.environ.get("VBIO_TASK_ID") or affinity_base.name).strip()
    task_container_name = make_scoped_container_name(f"{runtime_task_id}-{archive_prefix}-boltz2score", prefix="boltz-af3")
    runtime_overridden = any(token == "--runtime" for token in extra_args)

    docker_command = ["docker", "run", "--rm"]
    if task_container_name:
        docker_command.extend(["--name", task_container_name])
        docker_command.extend(["--label", f"boltz.task_id={runtime_task_id}"])
        docker_command.extend(["--label", "boltz.runtime=boltz2score"])
    if not runtime_overridden:
        docker_command.extend(["--runtime", "nvidia"])
    if shm_size and not docker_args_has_flag(extra_args, "--shm-size") and not docker_args_has_flag(extra_args, "--ipc"):
        docker_command.extend(["--shm-size", shm_size])

    docker_command.extend(
        [
            "--gpus",
            gpu_arg,
            "--volume",
            f"{affinity_base}:{affinity_base}",
            "--volume",
            f"{BASE_DIR}:/workspace/vbio:ro",
            "--workdir",
            "/workspace/vbio",
            "--env",
            "PYTHONPATH=/workspace/vbio",
        ]
    )

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

    host_cache_dir = str(BOLTZ2_HOST_CACHE_DIR or "").strip()
    container_cache_dir = str(BOLTZ2_CONTAINER_CACHE_DIR or "/root/.boltz").strip() or "/root/.boltz"
    if host_cache_dir:
        os.makedirs(host_cache_dir, exist_ok=True)
        docker_command.extend(["--volume", f"{host_cache_dir}:{container_cache_dir}"])
        docker_command.extend(["--env", f"BOLTZ_CACHE={container_cache_dir}"])

    docker_command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    for gid in collect_gpu_device_group_ids():
        docker_command.extend(["--group-add", str(gid)])

    docker_command.extend(extra_args)
    docker_command.append(image)
    docker_command.extend(score_cmd)

    remove_container(task_container_name)

    score_log = affinity_base / "boltz2score.log"
    print(
        f"运行 affinity 后处理 Boltz2Score: {' '.join(shlex.quote(part) for part in docker_command)}",
        file=sys.stderr,
    )
    with score_log.open("w", encoding="utf-8") as logf:
        score_proc = subprocess.Popen(
            docker_command,
            cwd=str(BASE_DIR),
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
        )
        score_return = score_proc.wait()
    if score_return != 0:
        print(
            "[WARN] Boltz2Score affinity 后处理失败，跳过 affinity_data.json。"
            f" Tail:\n{_tail_lines(score_log, 120)}",
            file=sys.stderr,
        )
        try:
            (output_dir / "affinity_error.txt").write_text(
                f"Boltz2Score affinity postprocess failed with exit code {score_return}.\n"
                f"Log tail:\n{_tail_lines(score_log, 120)}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return []

    affinity_result_path = _find_first_existing(sorted(output_dir.rglob("affinity_*.json")))
    if affinity_result_path is None or not affinity_result_path.exists():
        print("[WARN] Boltz2Score affinity 未产生 affinity JSON，跳过 affinity_data.json。", file=sys.stderr)
        try:
            (output_dir / "affinity_error.txt").write_text(
                "Boltz2Score affinity finished but produced no affinity JSON.\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return []

    try:
        affinity_result = _load_json_object(affinity_result_path)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"[WARN] 读取 Boltz2Score affinity JSON 失败 ({exc})，跳过 affinity_data.json。", file=sys.stderr)
        return []
    if not affinity_result:
        print("[WARN] Boltz2Score affinity JSON 为空，跳过 affinity_data.json。", file=sys.stderr)
        return []

    affinity_result["source"] = source
    affinity_result["binder_chain"] = resolved_ligand_chain
    affinity_result["requested_ligand_chain"] = resolved_ligand_chain
    affinity_result["requested_target_chain"] = ",".join(target_chain_ids)
    affinity_result["target_chain"] = target_chain_ids[0]
    affinity_result["target_chain_ids"] = target_chain_ids
    if ligand_resname:
        affinity_result["ligand_resname"] = ligand_resname

    affinity_json_path = affinity_base / "affinity_data.json"
    affinity_json_path.write_text(
        json.dumps(affinity_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    affinity_entries: List[Tuple[Path, str]] = [(affinity_json_path, "affinity_data.json")]
    best_confidence_path = _find_first_existing(sorted(output_dir.rglob("best_confidence.json")))
    best_ipsae_path = _find_first_existing(sorted(output_dir.rglob("best_ipsae.json")))
    if best_confidence_path and best_confidence_path.exists():
        affinity_entries.append((best_confidence_path, f"{archive_prefix}/best_confidence.json"))
    if best_ipsae_path and best_ipsae_path.exists():
        affinity_entries.append((best_ipsae_path, f"{archive_prefix}/best_ipsae.json"))
    if score_log.exists():
        affinity_entries.append((score_log, f"{archive_prefix}/boltz2score.log"))

    print("亲和力预测完成，结果已写入 affinity_data.json。", file=sys.stderr)
    return affinity_entries


