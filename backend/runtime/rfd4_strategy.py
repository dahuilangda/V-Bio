from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
import yaml

from backend.core.config import (
    RFD4_CONTIG,
    RFD4_DATA_DIR,
    RFD4_DOCKER_IMAGE,
    RFD4_NUM_DESIGNS,
    VBIO_API_TOKEN,
    VBIO_GATEWAY_URL,
)
from backend.runtime import bicyclic_linkers, rfd4_adapter
from backend.runtime.peptide_design_backend import (
    _dpeptide_target_sequence,
    _normalize_peptide_design_mode,
    _pocket_contacts_for_staged_space,
)
from backend.runtime.rfd4_adapter import MIN_CLOSURE_RESIDUES
from backend.runtime.backends.support import (
    determine_docker_gpu_arg,
    resolve_backend_results_root,
    resolve_backend_work_root,
    safe_runtime_token,
)
from backend.worker.docker_cmd import ensure_image


def run_rfd4_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    template_inputs: Optional[List[dict]] = None,
    rfd4_spec: Optional[str] = None,
    seed: Optional[int] = None,
    task_id: Optional[str] = None,
) -> None:
    rfd4_image = str(RFD4_DOCKER_IMAGE or "").strip()
    ensure_image(rfd4_image)
    print("Using RFD4-Proteina backend", file=sys.stderr)

    results_root = resolve_backend_results_root("rfd4", task_id, temp_dir)
    work_root = resolve_backend_work_root(results_root)
    input_dir = work_root / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (results_root / "output").mkdir(parents=True, exist_ok=True)

    rfd4_adapter.stage_target(input_dir, template_inputs)

    spec_payload = yaml.safe_load(yaml_content) or {}
    target_entry = next(
        (s["protein"] for s in spec_payload.get("sequences", [])
         if isinstance(s, dict) and "protein" in s),
        None,
    )
    if target_entry is None:
        raise ValueError("RFD4 生成任务需要在 yaml 中提供靶点蛋白链。")
    target_length = len(str(target_entry.get("sequence", "")))
    if rfd4_spec:
        spec_text = str(rfd4_spec)
    else:
        spec_text = rfd4_adapter.default_spec(target_length, RFD4_CONTIG)
    spec_path = input_dir / "rfd4_spec.yaml"
    spec_path.write_text(spec_text, encoding="utf-8")

    seed_value = int(seed) if seed is not None else 101
    from backend.worker.docker_cmd import image_and_python

    image, _ = image_and_python(rfd4_image)
    gpu_arg = determine_docker_gpu_arg(os.environ.get("CUDA_VISIBLE_DEVICES"))
    container_name = f"rfd4-generate-{uuid.uuid4().hex[:12]}"
    run_token = safe_runtime_token(task_id or Path(temp_dir).name)
    command = [
        "docker", "run", "--rm", "--name", container_name,
        "--runtime", "nvidia", "--gpus", gpu_arg,
        "-e", "HF_HOME=/workspace/hf_cache",
        "-e", "HF_ENDPOINT=https://hf-mirror.com",
        "-v", f"{RFD4_DATA_DIR}/hf_cache:/workspace/hf_cache",
        "-v", f"{RFD4_DATA_DIR}/runs:/workspace/runs",
        "-v", f"{input_dir}:/workspace/input:ro",
        "-w", "/workspace",
        "--entrypoint", "python3",
        image, "/app/rfd4_generate.py",
        "--spec", "/workspace/input/rfd4_spec.yaml",
        "--out-dir", f"/workspace/runs/{run_token}",
        "--num-designs", str(RFD4_NUM_DESIGNS),
        "--seed", str(seed_value),
        "--timeout-min", "120",
    ]
    print(f"RFD4 docker: {shlex.join(command)}", file=sys.stderr)
    proc = subprocess.run(command, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"rfd4-generate exited {proc.returncode}")

    archive_root = work_root / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    engine_out_dir = Path(RFD4_DATA_DIR) / "runs" / run_token
    for src in engine_out_dir.rglob("*.cif"):
        dst = archive_root / src.relative_to(engine_out_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    if not any(archive_root.rglob("*.cif")):
        raise RuntimeError(
            f"RFD4 生成未产出结构: {engine_out_dir} 不存在或为空"
            "（检查 worker 容器是否挂载 RFD4_DATA_DIR）")
    summary_src = engine_out_dir / "summary.json"
    if summary_src.exists():
        shutil.copy2(summary_src, archive_root / "summary.json")
    if os.path.exists(output_archive_path):
        os.remove(output_archive_path)
    with zipfile.ZipFile(output_archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(archive_root):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                zf.write(file_path, os.path.relpath(file_path, archive_root))
    print(f"RFD4 generation archive written: {output_archive_path}", file=sys.stderr)


def run_rfd4_design_strategy(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    *,
    score_backend: str,
    project_id: str,
    predict_args: Dict[str, Any],
    seed: Optional[int],
    target_chain_id: Optional[str],
    template_inputs: Optional[List[Dict[str, Any]]] = None,
    options: Optional[Dict[str, Any]] = None,
) -> None:
    options = options or {}
    design_mode = _normalize_peptide_design_mode(
        options.get("peptideDesignMode") or options.get("peptide_design_mode") or "linear")
    gateway_base = VBIO_GATEWAY_URL
    platform_token = VBIO_API_TOKEN.strip()
    if not gateway_base or not platform_token:
        raise RuntimeError(
            "RFD4 设计策略需要网关地址与平台令牌（VBIO_GATEWAY_URL / VBIO_API_TOKEN）。")

    if design_mode == "bicyclic":
        run_bicyclic_strategy(
            temp_dir=temp_dir,
            yaml_content=yaml_content,
            output_archive_path=output_archive_path,
            score_backend=score_backend,
            project_id=project_id,
            predict_args=predict_args,
            seed=seed,
            target_chain_id=target_chain_id,
            template_inputs=template_inputs,
            options=options,
        )
        return

    gen_dir = Path(temp_dir) / "rfd4_strategy"
    gen_dir.mkdir(parents=True, exist_ok=True)
    gen_archive = gen_dir / "generation.zip"
    run_rfd4_backend(
        temp_dir=str(gen_dir),
        yaml_content=yaml_content,
        output_archive_path=str(gen_archive),
        template_inputs=template_inputs if isinstance(template_inputs, list) else None,
        rfd4_spec=predict_args.get("rfd4_spec"),
        seed=seed,
        task_id=str(predict_args.get("task_id") or ""),
    )

    with zipfile.ZipFile(gen_archive) as zf:
        summary_name = next((n for n in zf.namelist() if n.endswith("summary.json")), None)
        if not summary_name:
            raise RuntimeError("RFD4 生成归档缺少 summary.json，无法提取候选")
        summary = json.loads(zf.read(summary_name).decode("utf-8"))
        archived = {n: zf.read(n) for n in zf.namelist() if not n.endswith("summary.json")}

    candidates = rfd4_adapter.candidates_from_summary(summary)
    if not candidates:
        raise RuntimeError("RFD4 生成未产出可用候选序列")

    base_yaml = yaml.safe_load(yaml_content) or {}
    target_sequence = _dpeptide_target_sequence(base_yaml, target_chain_id or "")

    verify_seed = int(seed) if isinstance(seed, int) else 101
    manifest: Dict[str, Any] = {
        "strategy": "rfd4",
        "score_backend": score_backend,
        "project_id": project_id,
        "verifications": [],
    }
    headers = {"X-API-Token": platform_token}
    dispatched = 0
    for cand in candidates:
        cyclic_bond = ""
        if design_mode == "cyclic":
            seq_len = len(cand["sequence"])
            cyclic_bond = (
                "constraints:\n"
                "- bond:\n"
                "    atom1: [B, 1, N]\n"
                f"    atom2: [B, {seq_len}, C]\n"
            )
        verify_yaml = (
            "version: 1\nsequences:\n"
            "- protein:\n    id: A\n"
            f"    sequence: {target_sequence}\n"
            "- protein:\n    id: B\n"
            f"    sequence: {cand['sequence']}\n"
            + cyclic_bond
        )
        fields = {
            "project_id": project_id,
            "backend": score_backend,
            "workflow": "prediction",
            "use_msa_server": "true",
            "msa_mode": "uniref",
            "seed": str(verify_seed),
            "task_name": f"RFD4 verify {score_backend} {cand['label']} {cand['sequence'][:16]}",
        }
        entry = {
            "design": cand["label"],
            "sequence": cand["sequence"],
            "backend": score_backend,
        }
        try:
            response = requests.post(
                f"{gateway_base}/predict",
                data=fields,
                files={"yaml_file": ("design.yaml", verify_yaml, "application/x-yaml")},
                headers=headers,
                timeout=(10, 120),
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
            task_id = str(payload.get("task_id") or "").strip()
            if not task_id:
                raise RuntimeError(f"gateway response missing task_id: {str(payload)[:160]}")
            entry["task_id"] = task_id
            dispatched += 1
            print(f"[rfd4-strategy] {cand['label']} {score_backend} -> {task_id}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — 单条失败不阻断其余候选
            entry["error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
            print(f"[rfd4-strategy] {cand['label']} dispatch FAILED: {entry['error']}",
                  file=sys.stderr)
        manifest["verifications"].append(entry)

    if dispatched == 0:
        raise RuntimeError(
            f"RFD4 候选验证派发全部失败（{len(candidates)} 条）；检查网关可达性后重试。")

    if os.path.exists(output_archive_path):
        os.remove(output_archive_path)
    with zipfile.ZipFile(output_archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in archived.items():
            zf.writestr(name, data)
        zf.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=1))
        zf.writestr("verification_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    print(f"[rfd4-strategy] {dispatched}/{len(candidates)} verification tasks dispatched",
          file=sys.stderr)


def _assert_bond_capable_scorer(design_mode: str, score_backend: str) -> None:
    if design_mode not in ("cyclic", "bicyclic"):
        return
    from backend.core.engine_capabilities import engine_constraint_quality
    backend = str(score_backend or "").strip().lower()
    aliases = {"mol-1": "mol1", "protenix-2-dock": "protenix", "esm-fold-2": "esmfold2"}
    if engine_constraint_quality(aliases.get(backend, backend), "bond") == "hard":
        return
    raise ValueError(
        f"模式 {design_mode} 的成环约束需要 hard 级共价键先验：打分后端 {score_backend} "
        "为 soft 先验或不支持；可用 mol1 / protenix / protenix2dock / alphafold3 / esmfold2。")


def _pair_arms_by_distance(anchors: Dict[int, Dict[str, np.ndarray]],
                           linker_atoms: List[Tuple[str, np.ndarray]],
                           linker_ccd: str) -> List[str]:
    import numpy as _np
    linker = bicyclic_linkers.get_linker(linker_ccd)
    by_name = dict(linker_atoms)
    arms = [(name, by_name[name]) for name in linker.arm_atoms if name in by_name]
    if len(arms) != 3:
        raise ValueError(f"linker {linker_ccd} 臂原子在骨架资产中不完整: "
                         f"{[n for n, _ in arms]}")
    pairs: List[Tuple[float, int, int]] = []
    for si, s_idx in enumerate((1, 2, 3)):
        sg = anchors[s_idx]["SG"]
        for ai, (_name, pos) in enumerate(arms):
            pairs.append((float(_np.linalg.norm(sg - pos)), si, ai))
    pairs.sort()
    used_s, used_a, order = set(), set(), [None] * 3
    for _d, si, ai in pairs:
        if si in used_s or ai in used_a:
            continue
        used_s.add(si)
        used_a.add(ai)
        order[si] = arms[ai][0]
    if len(used_a) != 3:
        raise ValueError("linker 臂与 Cys SG 的一对一配对失败")
    return order


def run_bicyclic_strategy(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    *,
    score_backend: str,
    project_id: str,
    predict_args: Dict[str, Any],
    seed: Optional[int],
    target_chain_id: Optional[str],
    template_inputs: Optional[List[Dict[str, Any]]],
    options: Dict[str, Any],
) -> None:
    from backend.runtime import rfd4_adapter
    from backend.runtime.rfd4_adapter import MIN_CLOSURE_RESIDUES

    base_yaml = yaml.safe_load(yaml_content) or {}
    seed_letters = re.sub(r"[^A-Z]", "",
                          str(options.get("peptideSequenceMask")
                              or options.get("peptide_sequence_mask") or "").upper())
    if not seed_letters:
        raise ValueError(
            "双环 rfd4 策略需要种子序列：peptide_design_options.peptideSequenceMask"
            "（一字母，如 AARFKVWW）。")
    bad_letters = sorted(set(seed_letters) - set(rfd4_adapter.AA1_TO_AA3) | ({"C"} & set(seed_letters)))
    if bad_letters:
        raise ValueError(
            f"种子序列含不支持字母 {bad_letters}：只允许标准氨基酸一字母且不含 C"
            "（三个 Cys 锚点由平台自动放置）。")

    ncaa_raw = options.get("peptideNcaaCcds") or options.get("peptide_ncaa_ccds") or []
    if isinstance(ncaa_raw, str):
        ncaa_list = [x.strip().upper() for x in ncaa_raw.split(",") if x.strip()]
    else:
        ncaa_list = [str(x).strip().upper() for x in ncaa_raw if str(x).strip()]

    linker_ccd = str(options.get("peptideBicyclicLinkerCcd")
                     or options.get("peptide_bicyclic_linker_ccd") or "ZBR").strip().upper()
    bicyclic_linkers.get_linker(linker_ccd)

    len_lo = int(options.get("peptideLengthMin") or options.get("peptide_length_min") or 0)
    len_hi = int(options.get("peptideLengthMax") or options.get("peptide_length_max") or 0)
    if len_lo <= 0 or len_hi < len_lo:
        raise ValueError("双环 rfd4 策略需要 peptideLengthMin/Max（Min>0 且 Max≥Min）。")
    min_len = len(seed_letters) + 3 + MIN_CLOSURE_RESIDUES
    if len_lo < min_len:
        raise ValueError(
            f"双环总长窗口过短：种子 {seed_letters}（{len(seed_letters)}）要求总长 ≥"
            f" {min_len}（闭环环 ~8.4Å 跨距至少 {MIN_CLOSURE_RESIDUES} 残基），"
            f"得到 Min={len_lo}。")

    if not template_inputs:
        raise ValueError(
            "双环 rfd4 策略需要带坐标靶标结构（template_files）；仅有序列时请先"
            "提交预测任务，再以产物结构作为输入。")
    pocket_author_contacts, _pocket_seq_contacts = _pocket_contacts_for_staged_space(
        base_yaml, options, target_chain_id)
    if not pocket_author_contacts:
        raise ValueError(
            "双环 rfd4 策略需要口袋定位：peptidePocketResidues（作者编号，如 "
            "\"A:41,D:92,...\"）或 peptidePocketCenter。无口袋的 motif 漂浮生成"
            "无意义。")

    target_sequence = _dpeptide_target_sequence(base_yaml, target_chain_id or "")
    if not target_sequence:
        raise ValueError("双环 rfd4 策略需要 yaml 中的靶标蛋白序列。")

    runtime_dir = Path(__file__).resolve().parent
    anchors, sez_atoms = rfd4_adapter.load_anchor_skeleton(runtime_dir, linker_ccd)

    prep_dir = Path(temp_dir) / "bicyclic_prep"
    prep_dir.mkdir(parents=True, exist_ok=True)
    staged_target = rfd4_adapter.stage_target(prep_dir, template_inputs)
    pocket_center = rfd4_adapter.pocket_centroid(staged_target, pocket_author_contacts)
    parent_task_id = str(predict_args.get("task_id")
                         or os.environ.get("VBIO_TASK_ID") or "")

    gateway_base = VBIO_GATEWAY_URL
    platform_token = VBIO_API_TOKEN.strip()
    if not gateway_base or not platform_token:
        raise RuntimeError(
            "RFD4 bicyclic strategy needs the gateway URL and API token "
            "(VBIO_GATEWAY_URL / VBIO_API_TOKEN).")
    design_mode = "bicyclic"

    verify_seed = int(seed) if isinstance(seed, int) else 101
    headers = {"X-API-Token": platform_token}
    manifest: Dict[str, Any] = {
        "strategy": "rfd4-bicyclic",
        "score_backend": score_backend,
        "linker_ccd": linker_ccd,
        "seed": seed_letters,
        "ncaa": ncaa_list,
        "project_id": project_id,
        "generations": [],
        "verifications": [],
    }
    dispatched = 0
    archive_files: Dict[str, bytes] = {}

    for ncaa in (ncaa_list or [None]):
        for length in range(len_lo, len_hi + 1):
            stem = f"bicyc_{ncaa or 'none'}_L{length}"
            gen_dir = Path(temp_dir) / "rfd4_strategy" / stem
            gen_dir.mkdir(parents=True, exist_ok=True)

            residues, sez, _letters, bonds = rfd4_adapter.build_bicyclic_skeleton(
                seed_letters, ncaa, length, anchors, sez_atoms)
            pep_len = len(residues)
            motif_pts = np.array([p for _c, atoms in residues for p in atoms.values()])
            shift = pocket_center - motif_pts.mean(axis=0)
            shifted = [(c, {k: v + shift for k, v in atoms.items()})
                       for c, atoms in residues]
            shifted_sez = [(n, p + shift) for n, p in sez]
            combined_cif = gen_dir / f"{stem}_combined.cif"
            target_chains = rfd4_adapter.write_combined_cif(
                combined_cif, stem, shifted, shifted_sez, linker_ccd, staged_target)
            spec = rfd4_adapter.build_spec(stem, len(seed_letters), bool(ncaa), pep_len,
                                           target_chains, rfd4_adapter.chain_centroids(combined_cif))
            spec_path = gen_dir / f"{stem}_spec.yaml"
            spec_path.write_text(spec, encoding="utf-8")
            archive_files[f"{stem}_combined.cif"] = combined_cif.read_bytes()
            archive_files[f"{stem}_spec.yaml"] = spec.encode("utf-8")

            gen_archive = gen_dir / "generation.zip"
            print(f"[rfd4-bicyclic] {stem}: generating (bonds={bonds})", file=sys.stderr)
            run_rfd4_backend(
                temp_dir=str(gen_dir),
                yaml_content=yaml_content,
                output_archive_path=str(gen_archive),
                template_inputs=[rfd4_adapter.combined_template_input(combined_cif)],
                rfd4_spec=spec,
                seed=seed,
                task_id=f"{parent_task_id}-{stem}",
            )
            if not gen_archive.is_file():
                raise RuntimeError(f"{stem}: 生成归档缺失")
            manifest["generations"].append({
                "stem": stem, "ncaa": ncaa, "length": length, "bonds": bonds})

            with zipfile.ZipFile(gen_archive) as zf:
                cifs = sorted(n for n in zf.namelist() if n.endswith("_generated.cif"))
                archived = {n: zf.read(n) for n in cifs}
            if not cifs:
                raise RuntimeError(f"{stem}: 生成未产出任何设计")
            for name in cifs:
                archive_files[f"gen/{stem}/{Path(name).name}"] = archived[name]
            arm_order = _pair_arms_by_distance(anchors, sez, linker_ccd)
            for name in cifs:
                label = Path(name).parent.name
                entry = {"design": label, "backend": score_backend}
                try:
                    binder_seq, mods, cys_positions = rfd4_adapter.binder_from_cif_text(
                        archived[name].decode("utf-8"))
                    entry["sequence"] = binder_seq
                    entry["modifications"] = mods
                    verify_yaml = rfd4_adapter.build_verify_yaml(
                        target_sequence, binder_seq, mods, cys_positions,
                        linker_ccd, arm_order)
                    fields = {
                        "project_id": project_id,
                        "backend": score_backend,
                        "workflow": "prediction",
                        "use_msa_server": "true",
                        "msa_mode": "uniref",
                        "seed": str(verify_seed),
                        "task_name": f"RFD4 verify {score_backend} {label} {binder_seq[:16]}",
                    }
                    last_error: Optional[Exception] = None
                    for attempt in range(3):
                        try:
                            response = requests.post(
                                f"{gateway_base}/predict",
                                data=fields,
                                files={"yaml_file": ("design.yaml", verify_yaml,
                                                     "application/x-yaml")},
                                headers=headers, timeout=(10, 120))
                            response.raise_for_status()
                            payload = response.json() if response.content else {}
                            task = str(payload.get("task_id") or "").strip()
                            if not task:
                                raise RuntimeError(
                                    f"gateway response missing task_id: {str(payload)[:160]}")
                            entry["task_id"] = task
                            dispatched += 1
                            print(f"[rfd4-bicyclic] {label} {score_backend} -> {task}",
                                  file=sys.stderr)
                            last_error = None
                            break
                        except requests.HTTPError as exc:
                            body = ""
                            try:
                                body = str(response.text[:200])
                            except Exception:
                                pass
                            last_error = RuntimeError(
                                f"HTTP {response.status_code}: {body or exc}")
                            if attempt < 2:
                                time.sleep(5 * (attempt + 1))
                        except Exception as exc:  # noqa: BLE001 — 网关瞬时抖动重试
                            last_error = exc
                            if attempt < 2:
                                time.sleep(5 * (attempt + 1))
                    if last_error is not None:
                        raise last_error
                except Exception as exc:  # noqa: BLE001 — 单条失败不阻断其余候选
                    entry["error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
                    print(f"[rfd4-bicyclic] {label} dispatch FAILED: {entry['error']}"
                          f" | fields keys={sorted(fields)} project_id={project_id!r}",
                          file=sys.stderr)
                manifest["verifications"].append(entry)

    if dispatched == 0:
        raise RuntimeError("双环候选验证派发全部失败；检查网关可达性后重试。")

    if os.path.exists(output_archive_path):
        os.remove(output_archive_path)
    with zipfile.ZipFile(output_archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in archive_files.items():
            zf.writestr(name, data)
        zf.writestr("verification_manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=1))
    print(f"[rfd4-bicyclic] {dispatched} verification tasks dispatched",
          file=sys.stderr)


