"""ipSAE 后处理：逐引擎（boltz/af3/protenix/standalone）ipsae 计算、
置信载荷组装、配体注释与归档条目。

由各后端子类在容器执行完成后调用；对 run_single_prediction 的
编排符号（结构定位、亲和力管线）经模块引用访问。
"""
from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import gemmi
import numpy as np

from backend.core.config import BOLTZ2_DOCKER_EXTRA_ARGS, BOLTZ2_DOCKER_IMAGE
from backend.runtime.design_constants import AMINO_ACID_MAPPING
from backend.runtime.docker_exec import remove_container
from backend.runtime.peptide_design_backend import (
    _BOLTZ_RESULT_CONF_RE,
    _PROTENIX_SUMMARY_CONF_RE,
    IPSAE_DIST_CUTOFF,
    IPSAE_PAE_CUTOFF,
    PROJECT_ROOT,
)
from backend.worker.docker_cmd import (
    _docker_warn,
    collect_gpu_device_group_ids,
    ensure_image,
    make_scoped_container_name,
    sanitize_docker_extra_args,
)


def _iter_affinity_entries(properties: Any) -> Iterable[Dict[str, Any]]:
    """标准化 properties 字段，支持 list / dict 等多种写法。"""
    if properties is None:
        return []

    if isinstance(properties, dict):
        return [properties]

    if isinstance(properties, list):
        return [entry for entry in properties if isinstance(entry, dict)]

    return []




def _load_json_object(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON payload at {path} is not an object: {type(payload).__name__}")
    return payload




def _run_boltz2score_ipsae_postprocess_in_docker(
    *,
    stage_root: Path,
    record_id: str,
    log_path: Path,
) -> None:
    image = str(BOLTZ2_DOCKER_IMAGE or "").strip()
    if not image:
        raise RuntimeError("BOLTZ2_DOCKER_IMAGE 未配置，无法运行 IPSAE 后处理。")
    ensure_image(image)

    raw_extra_args = shlex.split(BOLTZ2_DOCKER_EXTRA_ARGS) if BOLTZ2_DOCKER_EXTRA_ARGS else []
    extra_args = sanitize_docker_extra_args(raw_extra_args, log=_docker_warn)
    runtime_task_id = str(os.environ.get("VBIO_TASK_ID") or record_id).strip()
    task_container_name = make_scoped_container_name(f"{runtime_task_id}-{record_id}-ipsae", prefix="boltz-af3")

    docker_command = ["docker", "run", "--rm"]
    if task_container_name:
        docker_command.extend(["--name", task_container_name])
        docker_command.extend(["--label", f"boltz.task_id={runtime_task_id}"])
        docker_command.extend(["--label", "boltz.runtime=boltz2score-ipsae"])

    docker_command.extend(
        [
            "--volume",
            f"{stage_root}:{stage_root}",
            "--volume",
            f"{PROJECT_ROOT}:/workspace/vbio:ro",
            "--workdir",
            "/workspace/vbio",
            "--env",
            "PYTHONPATH=/workspace/vbio",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
        ]
    )
    for gid in collect_gpu_device_group_ids():
        docker_command.extend(["--group-add", str(gid)])

    docker_command.extend(extra_args)
    docker_command.append(image)
    docker_command.extend(
        [
            "python",
            "-c",
            (
                "import sys; "
                "sys.path.insert(0, '/workspace/vbio/capabilities/boltz2score'); "
                "from core.results import compute_and_write_ipsae, rerank_diffusion_samples; "
                "from pathlib import Path; "
                "output_dir = Path(sys.argv[1]); "
                "record_id = sys.argv[2]; "
                "pae_cutoff = float(sys.argv[3]); "
                "dist_cutoff = float(sys.argv[4]); "
                "compute_and_write_ipsae(output_dir=output_dir, record_id=record_id, pae_cutoff=pae_cutoff, dist_cutoff=dist_cutoff); "
                "rerank_diffusion_samples(output_dir, record_id)"
            ),
            str(stage_root),
            record_id,
            str(IPSAE_PAE_CUTOFF),
            str(IPSAE_DIST_CUTOFF),
        ]
    )

    remove_container(task_container_name)

    with log_path.open("w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            docker_command,
            cwd=str(PROJECT_ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return_code = proc.wait()

    if return_code != 0:
        raise RuntimeError(
            "Boltz Docker IPSAE 后处理失败。"
            f" Tail:\n{_tail_lines(log_path, 120)}\n"
            f"Full log: {log_path}"
        )




def _to_finite_float(value: Any) -> Optional[float]:
    from backend.services.common_utils import to_finite_float
    return to_finite_float(value)




def _choose_preferred_existing_path(paths: Iterable[Path]) -> Optional[Path]:
    candidates = [Path(path) for path in paths if Path(path).exists()]
    if not candidates:
        return None
    candidates.sort(
        key=lambda path: (
            1 if "seed-" in str(path).lower() else 0,
            len(str(path)),
            str(path),
        )
    )
    return candidates[0]




def _collect_structure_chain_ids(structure_path: Path) -> List[str]:
    try:
        structure = gemmi.read_structure(str(structure_path))
    except Exception:
        return []

    chain_ids: List[str] = []
    seen: set[str] = set()
    for model in structure:
        for chain in model:
            chain_id = str(chain.name or "").strip()
            if not chain_id or chain_id in seen:
                continue
            seen.add(chain_id)
            chain_ids.append(chain_id)
        if chain_ids:
            break
    return chain_ids




def _build_structure_chain_map(structure_path: Path) -> Dict[str, str]:
    return {
        str(index): chain_id
        for index, chain_id in enumerate(_collect_structure_chain_ids(structure_path))
    }




def _cif_chain_has_nonpolymer_atom_site(structure_path: Path, chain_id: str) -> Optional[bool]:
    if structure_path.suffix.lower() not in {".cif", ".mmcif"}:
        return None

    chain_id = str(chain_id or "").strip()
    if not chain_id:
        return False

    try:
        lines = structure_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    solvent_names = {"HOH", "WAT", "H2O", "DOD"}
    atom_fields: List[str] = []
    atom_field_index: Dict[str, int] = {}
    inside_atom_loop = False
    saw_chain = False

    for line in lines:
        stripped = line.strip()
        if stripped == "loop_":
            atom_fields = []
            atom_field_index = {}
            inside_atom_loop = False
            continue

        if line.startswith("_atom_site."):
            inside_atom_loop = True
            atom_fields.append(line.strip().split(".", 1)[1])
            atom_field_index = {name: index for index, name in enumerate(atom_fields)}
            continue

        if inside_atom_loop and atom_fields and (line.startswith("ATOM") or line.startswith("HETATM")):
            parts = line.split()
            if len(parts) < len(atom_fields):
                continue
            chain_values = []
            for field_name in ("auth_asym_id", "label_asym_id"):
                field_index = atom_field_index.get(field_name)
                if field_index is not None and field_index < len(parts):
                    chain_values.append(parts[field_index])
            if chain_id not in chain_values:
                continue
            saw_chain = True
            residue_name = ""
            comp_index = atom_field_index.get("label_comp_id")
            if comp_index is not None and comp_index < len(parts):
                residue_name = str(parts[comp_index]).strip().upper()
            seq_index = atom_field_index.get("label_seq_id")
            seq_id = parts[seq_index] if seq_index is not None and seq_index < len(parts) else ""
            if seq_id == "." and residue_name not in solvent_names:
                return True
            continue

        if inside_atom_loop and atom_fields and stripped and not stripped.startswith("_atom_site."):
            if not (line.startswith("ATOM") or line.startswith("HETATM")):
                inside_atom_loop = False
                atom_fields = []
                atom_field_index = {}

    return False if saw_chain else None




def _structure_chain_has_nonpolymer_ligand(structure_path: Path, chain_id: str) -> bool:
    chain_id = str(chain_id or "").strip()
    if not chain_id:
        return False

    cif_result = _cif_chain_has_nonpolymer_atom_site(structure_path, chain_id)
    if cif_result is not None:
        return cif_result

    solvent_names = {"HOH", "WAT", "H2O", "DOD"}
    polymer_like_names = set(AMINO_ACID_MAPPING.keys()) | {
        "A", "C", "G", "U", "I",
        "DA", "DC", "DG", "DT", "DI", "DU",
    }

    try:
        structure = gemmi.read_structure(str(structure_path))
    except Exception:
        return False

    for model in structure:
        for chain in model:
            current_chain_id = str(chain.name or "").strip()
            if current_chain_id != chain_id:
                continue
            residue_names = [
                str(residue.name or "").strip().upper()
                for residue in chain
                if str(residue.name or "").strip()
            ]
            return any(
                residue_name not in solvent_names and residue_name not in polymer_like_names
                for residue_name in residue_names
            )
    return False




def _structure_chain_exists(structure_path: Path, chain_id: str) -> bool:
    chain_key = str(chain_id or "").strip()
    if not chain_key:
        return False
    return chain_key in set(_collect_structure_chain_ids(structure_path))




def _extract_ligand_chain_ids_from_yaml_data(
    yaml_data: Dict[str, Any],
    alias_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    if not isinstance(yaml_data, dict):
        return []

    resolved: List[str] = []
    seen: set[str] = set()

    def add_chain_id(value: Any) -> None:
        chain_id = str(value or "").strip()
        if not chain_id:
            return
        if alias_map:
            chain_id = (
                alias_map.get(chain_id)
                or alias_map.get(chain_id.upper())
                or alias_map.get(chain_id.lower())
                or chain_id
            )
        if chain_id in seen:
            return
        seen.add(chain_id)
        resolved.append(chain_id)

    for entry in yaml_data.get("sequences", []) or []:
        if not isinstance(entry, dict):
            continue
        ligand = entry.get("ligand")
        if not isinstance(ligand, dict):
            continue
        raw_ids = ligand.get("id")
        if isinstance(raw_ids, list):
            values = raw_ids
        elif raw_ids is None:
            values = []
        else:
            values = [raw_ids]
        for value in values:
            add_chain_id(value)

    for entry in _iter_affinity_entries(yaml_data.get("properties")):
        add_chain_id(entry.get("ligand"))
        add_chain_id(entry.get("binder"))
        affinity_info = entry.get("affinity")
        if isinstance(affinity_info, dict):
            add_chain_id(affinity_info.get("binder") or affinity_info.get("chain"))
    return resolved




def _resolve_ligand_chain_annotations(
    requested_chain_ids: Iterable[str],
    structure_path: Path,
) -> Optional[Dict[str, str]]:
    requested = [str(chain_id or "").strip() for chain_id in requested_chain_ids if str(chain_id or "").strip()]
    valid_requested = [
        chain_id
        for chain_id in requested
        if _structure_chain_has_nonpolymer_ligand(structure_path, chain_id)
    ]
    if len(valid_requested) == 1:
        requested_ligand_chain = requested[0] if len(requested) == 1 else valid_requested[0]
        return {
            "requested_ligand_chain_id": requested_ligand_chain,
            "model_ligand_chain_id": valid_requested[0],
        }

    existing_requested = [
        chain_id
        for chain_id in requested
        if _structure_chain_exists(structure_path, chain_id)
    ]
    if len(existing_requested) == 1:
        return {
            "requested_ligand_chain_id": existing_requested[0],
            "model_ligand_chain_id": existing_requested[0],
        }

    inferred = _find_ligand_chain_and_resname_in_structure(structure_path)
    if not inferred:
        return None

    inferred_chain = str(inferred[0] or "").strip()
    if not inferred_chain:
        return None

    if not requested:
        requested_ligand_chain = inferred_chain
    elif len(requested) == 1:
        requested_ligand_chain = requested[0]
    elif inferred_chain in requested:
        requested_ligand_chain = inferred_chain
    else:
        return None

    return {
        "requested_ligand_chain_id": requested_ligand_chain,
        "model_ligand_chain_id": inferred_chain,
    }




def _log_ipsae_ligand_annotation_skip(
    source: str,
    requested_chain_ids: Iterable[str],
    structure_path: Path,
) -> None:
    requested = [str(chain_id or "").strip() for chain_id in requested_chain_ids if str(chain_id or "").strip()]
    structure_chain_ids = _collect_structure_chain_ids(structure_path)
    requested_text = ",".join(requested) if requested else "未声明"
    structure_text = ",".join(structure_chain_ids) if structure_chain_ids else "未解析到"
    print(
        f"[WARN] {source} IPSAE 后处理跳过：YAML 未声明可在结构中解析的 ligand/binder 链。"
        f" requested={requested_text}; structure_chains={structure_text}。"
        "请在 yaml_file properties 中明确写入 target、ligand、binder。",
        file=sys.stderr,
    )




def _convert_pair_iptm_matrix_to_map(matrix: Any, chain_map: Dict[str, str]) -> Dict[str, Dict[str, float]]:
    if not isinstance(matrix, list):
        return {}

    pair_map: Dict[str, Dict[str, float]] = {}
    chain_count = len(chain_map)
    for row_index, row in enumerate(matrix[:chain_count]):
        if not isinstance(row, list):
            continue
        row_map: Dict[str, float] = {}
        for col_index, value in enumerate(row[:chain_count]):
            parsed = _to_finite_float(value)
            if parsed is None:
                continue
            row_map[str(col_index)] = parsed
        if row_map:
            pair_map[str(row_index)] = row_map
    return pair_map




def _write_ipsae_json(path: Path, payload: Dict[str, Any]) -> None:
    def _normalize_json_value(value: Any) -> Any:
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, dict):
            return {str(key): _normalize_json_value(nested) for key, nested in value.items()}
        if isinstance(value, (list, tuple)):
            return [_normalize_json_value(item) for item in value]
        return value

    path.write_text(
        json.dumps(_normalize_json_value(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )




def _write_pae_matrix(path: Path, pae_matrix: Any) -> None:
    matrix = np.asarray(pae_matrix, dtype=float)
    np.savez_compressed(path, pae=matrix)




def _copy_structure_with_ipsae_ligand_layout(
    source_path: Path,
    dest_path: Path,
    ligand_chain_id: str,
) -> None:
    ligand_chain_id = str(ligand_chain_id or "").strip()
    if source_path.suffix.lower() not in {".cif", ".mmcif"} or not ligand_chain_id:
        shutil.copy2(source_path, dest_path)
        return
    if not _structure_chain_has_nonpolymer_ligand(source_path, ligand_chain_id):
        shutil.copy2(source_path, dest_path)
        return

    try:
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        shutil.copy2(source_path, dest_path)
        return

    output_lines: List[str] = []
    atom_fields: List[str] = []
    atom_field_index: Dict[str, int] = {}
    inside_atom_loop = False

    for line in lines:
        stripped = line.strip()
        if stripped == "loop_":
            atom_fields = []
            atom_field_index = {}
            inside_atom_loop = False
            output_lines.append(line)
            continue

        if line.startswith("_atom_site."):
            inside_atom_loop = True
            atom_fields.append(line.strip().split(".", 1)[1])
            atom_field_index = {name: index for index, name in enumerate(atom_fields)}
            output_lines.append(line)
            continue

        if inside_atom_loop and (line.startswith("ATOM") or line.startswith("HETATM")):
            parts = line.split()
            required_fields = {"label_seq_id", "auth_asym_id"}
            if required_fields.issubset(atom_field_index) and len(parts) >= len(atom_fields):
                chain_value = parts[atom_field_index.get("auth_asym_id", -1)]
                if chain_value == ligand_chain_id:
                    parts[atom_field_index["label_seq_id"]] = "."
                    line = " ".join(parts)
            output_lines.append(line)
            continue

        if inside_atom_loop and atom_fields and stripped and not stripped.startswith("_atom_site."):
            output_lines.append(line)
            if not (line.startswith("ATOM") or line.startswith("HETATM")):
                inside_atom_loop = False
                atom_fields = []
                atom_field_index = {}
            continue

        output_lines.append(line)

    dest_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")




def _finalize_ipsae_archive_entries(record_dir: Path) -> List[Tuple[Path, str]]:
    best_confidence_path = record_dir / "best_confidence.json"
    best_structure_path = _find_first_existing(
        [record_dir / "best_model.cif", record_dir / "best_model.mmcif"]
    )
    if best_confidence_path.exists():
        confidence_best_model_path = record_dir / "confidence_best_model.json"
        shutil.copy2(best_confidence_path, confidence_best_model_path)

    entries: List[Tuple[Path, str]] = []
    for candidate_name in (
        "best_ipsae.json",
        "best_confidence.json",
        "confidence_best_model.json",
        "best_model.cif",
        "best_model.mmcif",
    ):
        candidate_path = record_dir / candidate_name
        if candidate_path.exists():
            entries.append((candidate_path, candidate_name))
    if best_structure_path and best_structure_path.exists():
        best_structure_alias = record_dir / best_structure_path.name
        if best_structure_alias.exists():
            entries.append((best_structure_alias, best_structure_alias.name))

    seen_arcnames: set[str] = set()
    deduped: List[Tuple[Path, str]] = []
    for file_path, arcname in entries:
        if arcname in seen_arcnames:
            continue
        seen_arcnames.add(arcname)
        deduped.append((file_path, arcname))
    return deduped




def _run_standalone_ipsae_postprocess(
    *,
    postprocess_base: Path,
    source: str,
    record_id: str,
    model_entries: List[Dict[str, Any]],
) -> List[Tuple[Path, str]]:
    if not model_entries:
        print(f"{source} 未收集到可用于 IPSAE 的模型结果，跳过后处理。", file=sys.stderr)
        return []

    postprocess_base.mkdir(parents=True, exist_ok=True)
    stage_root = postprocess_base / "staged_output"
    record_dir = stage_root / record_id
    if record_dir.exists():
        shutil.rmtree(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)

    first_structure = Path(model_entries[0]["structure_path"])
    chain_map = _build_structure_chain_map(first_structure)
    if chain_map:
        _write_ipsae_json(record_dir / "chain_map.json", chain_map)

    for entry in model_entries:
        model_index = int(entry["model_index"])
        structure_path = Path(entry["structure_path"])
        structure_suffix = structure_path.suffix.lower() or ".cif"
        staged_structure_path = record_dir / f"{record_id}_model_{model_index}{structure_suffix}"
        _copy_structure_with_ipsae_ligand_layout(
            structure_path,
            staged_structure_path,
            str(entry["confidence_payload"].get("model_ligand_chain_id") or ""),
        )
        _write_ipsae_json(
            record_dir / f"confidence_{record_id}_model_{model_index}.json",
            dict(entry["confidence_payload"]),
        )
        _write_pae_matrix(
            record_dir / f"pae_{record_id}_model_{model_index}.npz",
            entry["pae_matrix"],
        )

    ipsae_log_path = record_dir / "boltz2score_ipsae.log"
    _run_boltz2score_ipsae_postprocess_in_docker(
        stage_root=stage_root,
        record_id=record_id,
        log_path=ipsae_log_path,
    )

    entries = _finalize_ipsae_archive_entries(record_dir)
    if ipsae_log_path.exists():
        entries.append((ipsae_log_path, ipsae_log_path.name))
    if not entries:
        print(f"[WARN] {source} IPSAE 后处理未生成可归档文件。", file=sys.stderr)
        return []

    print(f"{source} IPSAE 后处理完成，生成 {len(entries)} 个归档文件。", file=sys.stderr)
    return entries




def _run_boltz_ipsae_postprocess(
    *,
    postprocess_base: Path,
    results_dir: Path,
    yaml_data: Dict[str, Any],
    explicit_ligand_chain: Optional[str] = None,
) -> List[Tuple[Path, str]]:
    requested_chain_ids = _extract_ligand_chain_ids_from_yaml_data(yaml_data)
    # Same as the protenix side: peptide-design candidates declare their binder chain
    # explicitly — without it the ligand-only YAML extraction skips interface scoring.
    explicit_chain = str(explicit_ligand_chain or "").strip()
    if explicit_chain and explicit_chain not in requested_chain_ids:
        requested_chain_ids.insert(0, explicit_chain)
    model_entries: List[Dict[str, Any]] = []
    record_id: Optional[str] = None

    for confidence_path in sorted(results_dir.glob("confidence_*_model_*.json")):
        match = _BOLTZ_RESULT_CONF_RE.match(confidence_path.name)
        if not match:
            continue
        current_record_id = str(match.group(1))
        model_index = int(match.group(2))
        structure_path = _find_first_existing(
            [
                results_dir / f"{current_record_id}_model_{model_index}.cif",
                results_dir / f"{current_record_id}_model_{model_index}.mmcif",
            ]
        )
        pae_path = results_dir / f"pae_{current_record_id}_model_{model_index}.npz"
        if not structure_path or not structure_path.exists() or not pae_path.exists():
            continue

        annotations = _resolve_ligand_chain_annotations(requested_chain_ids, structure_path)
        if not annotations:
            _log_ipsae_ligand_annotation_skip("boltz", requested_chain_ids, structure_path)
            continue

        confidence_payload = _load_json_object(confidence_path)
        if not confidence_payload:
            continue
        confidence_payload.update(annotations)
        pae_matrix = np.load(pae_path)["pae"]
        model_entries.append(
            {
                "model_index": model_index,
                "structure_path": structure_path,
                "confidence_payload": confidence_payload,
                "pae_matrix": pae_matrix,
            }
        )
        if record_id is None:
            record_id = current_record_id

    if not record_id:
        return []

    return _run_standalone_ipsae_postprocess(
        postprocess_base=postprocess_base,
        source="boltz",
        record_id=record_id,
        model_entries=model_entries,
    )




def _build_af3_ipsae_confidence_payload(
    *,
    summary_payload: Dict[str, Any],
    confidences_payload: Dict[str, Any],
    chain_map: Dict[str, str],
    annotations: Dict[str, str],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(annotations)

    for metric_key in ("ptm", "iptm", "ranking_score", "fraction_disordered"):
        parsed = _to_finite_float(summary_payload.get(metric_key))
        if parsed is not None:
            payload[metric_key] = parsed
    if "ranking_score" in payload and "confidence_score" not in payload:
        payload["confidence_score"] = payload["ranking_score"]

    pair_chains_iptm = _convert_pair_iptm_matrix_to_map(summary_payload.get("chain_pair_iptm"), chain_map)
    if pair_chains_iptm:
        payload["pair_chains_iptm"] = pair_chains_iptm

    atom_plddts = [
        float(value)
        for value in confidences_payload.get("atom_plddts", []) or []
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    atom_chain_ids = [str(value) for value in confidences_payload.get("atom_chain_ids", []) or []]
    if atom_plddts:
        payload["complex_plddt"] = float(sum(atom_plddts) / len(atom_plddts))
    if atom_plddts and len(atom_plddts) == len(atom_chain_ids):
        chain_values: Dict[str, List[float]] = {}
        for chain_id, atom_plddt in zip(atom_chain_ids, atom_plddts):
            chain_values.setdefault(chain_id, []).append(atom_plddt)
        if chain_values:
            payload["chain_mean_plddt"] = {
                chain_id: float(sum(values) / len(values))
                for chain_id, values in chain_values.items()
                if values
            }
        ligand_chain_id = str(annotations.get("model_ligand_chain_id") or "").strip()
        ligand_values = chain_values.get(ligand_chain_id) or []
        if ligand_values:
            payload["ligand_atom_plddts"] = ligand_values
            payload["ligand_atom_plddts_by_chain"] = {ligand_chain_id: ligand_values}

    return payload




def _run_af3_ipsae_postprocess(
    *,
    postprocess_base: Path,
    yaml_data: Dict[str, Any],
    prep: AF3Preparation,
    af3_output_dir: Path,
) -> List[Tuple[Path, str]]:
    from backend.runtime.run_single_prediction import locate_af3_structure_file

    structure_path = locate_af3_structure_file(af3_output_dir, prep.jobname)
    if not structure_path or not structure_path.exists():
        return []

    job_dir = structure_path.parent
    summary_path = _choose_preferred_existing_path(job_dir.rglob("*summary_confidences.json"))
    confidences_path = _choose_preferred_existing_path(
        path
        for path in job_dir.rglob("*confidences.json")
        if "summary_confidences" not in path.name.lower()
    )
    if not summary_path or not confidences_path:
        return []

    summary_payload = _load_json_object(summary_path)
    confidences_payload = _load_json_object(confidences_path)
    pae_matrix = confidences_payload.get("pae")
    if not isinstance(pae_matrix, list):
        return []

    requested_chain_ids = _extract_ligand_chain_ids_from_yaml_data(
        yaml_data,
        alias_map=prep.chain_id_label_map,
    )
    annotations = _resolve_ligand_chain_annotations(requested_chain_ids, structure_path)
    if not annotations:
        _log_ipsae_ligand_annotation_skip("alphafold3", requested_chain_ids, structure_path)
        return []

    chain_map = _build_structure_chain_map(structure_path)
    confidence_payload = _build_af3_ipsae_confidence_payload(
        summary_payload=summary_payload,
        confidences_payload=confidences_payload,
        chain_map=chain_map,
        annotations=annotations,
    )

    return _run_standalone_ipsae_postprocess(
        postprocess_base=postprocess_base,
        source="alphafold3",
        record_id=prep.jobname,
        model_entries=[
            {
                "model_index": 0,
                "structure_path": structure_path,
                "confidence_payload": confidence_payload,
                "pae_matrix": pae_matrix,
            }
        ],
    )




def _build_protenix_ipsae_confidence_payload(
    *,
    summary_payload: Dict[str, Any],
    full_data_payload: Dict[str, Any],
    chain_map: Dict[str, str],
    annotations: Dict[str, str],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(annotations)

    for metric_key in ("ptm", "iptm", "ranking_score", "plddt"):
        parsed = _to_finite_float(summary_payload.get(metric_key))
        if parsed is not None:
            payload[metric_key] = parsed
    if "ranking_score" in payload and "confidence_score" not in payload:
        payload["confidence_score"] = payload["ranking_score"]

    pair_chains_iptm = _convert_pair_iptm_matrix_to_map(summary_payload.get("chain_pair_iptm"), chain_map)
    if pair_chains_iptm:
        payload["pair_chains_iptm"] = pair_chains_iptm

    atom_plddts = [
        float(value)
        for value in full_data_payload.get("atom_plddt", []) or []
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    if atom_plddts:
        payload["complex_plddt"] = float(sum(atom_plddts) / len(atom_plddts))

    atom_to_token_idx = full_data_payload.get("atom_to_token_idx", []) or []
    token_asym_id = full_data_payload.get("token_asym_id", []) or []
    ligand_chain_id = str(annotations.get("model_ligand_chain_id") or "").strip()
    if atom_plddts and isinstance(atom_to_token_idx, list) and isinstance(token_asym_id, list):
        ligand_values: List[float] = []
        for atom_index, atom_plddt in enumerate(atom_plddts):
            if atom_index >= len(atom_to_token_idx):
                break
            token_index = atom_to_token_idx[atom_index]
            if not isinstance(token_index, int) or token_index < 0 or token_index >= len(token_asym_id):
                continue
            chain_pos = token_asym_id[token_index]
            chain_id = chain_map.get(str(chain_pos))
            if chain_id != ligand_chain_id:
                continue
            ligand_values.append(atom_plddt)
        if ligand_values:
            payload["ligand_atom_plddts"] = ligand_values
            payload["ligand_atom_plddts_by_chain"] = {ligand_chain_id: ligand_values}

    return payload




def _run_protenix_ipsae_postprocess(
    *,
    postprocess_base: Path,
    yaml_data: Dict[str, Any],
    prep: ProtenixPreparation,
    protenix_output_dir: Path,
    explicit_ligand_chain: Optional[str] = None,
) -> List[Tuple[Path, str]]:
    summary_candidates = list(protenix_output_dir.rglob("*_summary_confidence_sample_*.json"))
    if not summary_candidates:
        return []

    scored_candidates: List[Tuple[float, Path]] = []
    for summary_path in summary_candidates:
        payload = _load_json_object(summary_path)
        ranking_score = _to_finite_float(payload.get("ranking_score"))
        if ranking_score is None:
            continue
        scored_candidates.append((ranking_score, summary_path))
    if not scored_candidates:
        return []

    scored_candidates.sort(key=lambda item: (-item[0], len(str(item[1]))))
    summary_path = scored_candidates[0][1]
    sample_match = _PROTENIX_SUMMARY_CONF_RE.search(summary_path.name)
    if not sample_match:
        return []

    sample_index = sample_match.group(1)
    summary_payload = _load_json_object(summary_path)
    structure_stem = summary_path.name[: summary_path.name.rfind(f"_summary_confidence_sample_{sample_index}.json")]
    structure_path = _find_first_existing(
        [
            summary_path.parent / f"{structure_stem}_sample_{sample_index}.cif",
            summary_path.parent / f"{structure_stem}_sample_{sample_index}.mmcif",
        ]
    )
    full_data_path = summary_path.parent / f"{structure_stem}_full_data_sample_{sample_index}.json"
    if not structure_path or not structure_path.exists() or not full_data_path.exists():
        return []

    full_data_payload = _load_json_object(full_data_path)
    pae_matrix = full_data_payload.get("token_pair_pae")
    if not isinstance(pae_matrix, list):
        return []

    requested_chain_ids = _extract_ligand_chain_ids_from_yaml_data(
        yaml_data,
        alias_map=prep.chain_alias_map,
    )
    # Peptide-design candidates carry no small-molecule ligand, so the YAML-based
    # extraction finds nothing and interface scoring was silently skipped for every
    # candidate (all rows shipped ligand_ipsae_max=None and the UI showed "IPSAE -").
    # The design loop declares the binder chain explicitly; it resolves through the
    # existing chain-exists branch (a polymer chain is a valid interface "ligand").
    explicit_chain = str(explicit_ligand_chain or "").strip()
    if explicit_chain and explicit_chain not in requested_chain_ids:
        requested_chain_ids.insert(0, explicit_chain)
    annotations = _resolve_ligand_chain_annotations(requested_chain_ids, structure_path)
    if not annotations:
        _log_ipsae_ligand_annotation_skip("protenix", requested_chain_ids, structure_path)
        return []

    chain_map = _build_structure_chain_map(structure_path)
    confidence_payload = _build_protenix_ipsae_confidence_payload(
        summary_payload=summary_payload,
        full_data_payload=full_data_payload,
        chain_map=chain_map,
        annotations=annotations,
    )

    return _run_standalone_ipsae_postprocess(
        postprocess_base=postprocess_base,
        source="protenix",
        record_id=prep.input_name,
        model_entries=[
            {
                "model_index": 0,
                "structure_path": structure_path,
                "confidence_payload": confidence_payload,
                "pae_matrix": pae_matrix,
            }
        ],
    )




def _find_ligand_chain_and_resname_in_structure(path: Path) -> Optional[Tuple[str, str]]:
    """Fallback ligand locator when binder chain ID does not match output chain naming."""
    polymer_like_names = set(AMINO_ACID_MAPPING.keys()) | {
        "A", "C", "G", "U", "I",
        "DA", "DC", "DG", "DT", "DI", "DU",
    }
    solvent_names = {"HOH", "WAT", "H2O", "DOD"}

    try:
        structure = gemmi.read_structure(str(path))
        for model in structure:
            for chain in model:
                chain_id = (chain.name or "").strip()
                for residue in chain:
                    resname = residue.name.strip().upper()
                    if not resname or resname in solvent_names or resname in polymer_like_names:
                        continue
                    return (chain_id, residue.name.strip())
    except Exception:
        pass

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.startswith("HETATM"):
                    continue

                if len(line) >= 22:
                    chain_id = line[21].strip()
                    resname = line[17:20].strip().upper()
                    if resname and resname not in solvent_names:
                        return (chain_id, resname)

                parts = line.split()
                if len(parts) >= 7:
                    resname = parts[5].strip().upper()
                    chain_id = parts[6].strip()
                    if resname and resname not in solvent_names:
                        return (chain_id, resname)
    except OSError:
        return None

    return None




def _tail_lines(path: Path, count: int = 80) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-count:])




def _find_first_existing(paths: List[Path]) -> Optional[Path]:
    for path in paths:
        if path.exists():
            return path
    return None






def __getattr__(name: str):
    """迁移期符号解析：函数体保留原引用，运行时从调度模块取编排符号。"""
    from backend.runtime import run_single_prediction as _rsp
    value = getattr(_rsp, name)
    globals()[name] = value
    return value
