from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import yaml

from backend.core.config import RFD4_CONTIG, RFD4_DATA_DIR, RFD4_DOCKER_IMAGE, RFD4_NUM_DESIGNS
from backend.runtime import bicyclic_linkers, rfd4_adapter
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
    num_designs: Optional[int] = None,
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
    spec_text = str(rfd4_spec) if rfd4_spec else rfd4_adapter.default_spec(target_length, RFD4_CONTIG)
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
        "--label", f"boltz.task_id={run_token}",
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
        "--num-designs", str(int(num_designs) if num_designs else RFD4_NUM_DESIGNS),
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


def generation_jobs(
    *,
    temp_dir: str,
    design_mode: str,
    options: Dict[str, Any],
    target_sequence: str,
    pocket_author_contacts,
    template_inputs: Optional[List[Dict[str, Any]]],
    spec: Optional[str],
    generation: int,
    num_designs: int,
) -> List[Dict[str, Any]]:
    root = Path(temp_dir) / "rfd4_generation" / f"gen_{generation:03d}"
    root.mkdir(parents=True, exist_ok=True)
    if design_mode != "bicyclic":
        return [_job(root / "linear", spec, template_inputs, num_designs)]

    seed_letters = re.sub(r"[^A-Z]", "", str(
        options.get("peptideSequenceMask") or options.get("peptide_sequence_mask") or "").upper())
    if not seed_letters:
        raise ValueError(
            "双环 rfd4 设计需要种子序列：peptide_design_options.peptideSequenceMask"
            "（一字母，如 AARFKVWW）。")
    bad_letters = sorted(
        set(seed_letters) - set(rfd4_adapter.AA1_TO_AA3) | ({"C"} & set(seed_letters)))
    if bad_letters:
        raise ValueError(
            f"种子序列含不支持字母 {bad_letters}：只允许标准氨基酸一字母且不含 C"
            "（三个 Cys 锚点由平台自动放置）。")

    ncaa_raw = options.get("peptideNcaaCcds") or options.get("peptide_ncaa_ccds") or []
    if isinstance(ncaa_raw, str):
        ncaa_list = [x.strip().upper() for x in ncaa_raw.split(",") if x.strip()]
    else:
        ncaa_list = [str(x).strip().upper() for x in ncaa_raw if str(x).strip()]

    linker_ccd = str(
        options.get("peptideBicyclicLinkerCcd")
        or options.get("peptide_bicyclic_linker_ccd") or "ZBR").strip().upper()
    bicyclic_linkers.get_linker(linker_ccd)

    len_lo = int(options.get("peptideLengthMin") or options.get("peptide_length_min") or 0)
    len_hi = int(options.get("peptideLengthMax") or options.get("peptide_length_max") or 0)
    if len_lo <= 0 or len_hi < len_lo:
        raise ValueError("双环 rfd4 设计需要 peptideLengthMin/Max（Min>0 且 Max≥Min）。")
    min_len = len(seed_letters) + 3 + MIN_CLOSURE_RESIDUES
    if len_lo < min_len:
        raise ValueError(
            f"双环总长窗口过短：种子 {seed_letters}（{len(seed_letters)}）要求总长 ≥"
            f" {min_len}（闭环环 ~8.4Å 跨距至少 {MIN_CLOSURE_RESIDUES} 残基），"
            f"得到 Min={len_lo}。")

    if not template_inputs:
        raise ValueError(
            "双环 rfd4 设计需要带坐标靶标结构（template_files）；仅有序列时请先"
            "提交预测任务，再以产物结构作为输入。")
    if not pocket_author_contacts:
        raise ValueError(
            "双环 rfd4 设计需要口袋定位：peptidePocketResidues（作者编号，如 "
            "\"A:41,D:92,...\"）或 peptidePocketCenter。")
    if not target_sequence:
        raise ValueError("rfd4 设计需要 yaml 中的靶标蛋白序列。")

    anchors, sez_atoms = rfd4_adapter.load_anchor_skeleton(Path(__file__).resolve().parent, linker_ccd)
    staged_target = rfd4_adapter.stage_target(root, template_inputs)
    pocket_center = rfd4_adapter.pocket_centroid(staged_target, pocket_author_contacts)

    ncaa_options = ncaa_list or [None]
    lengths = list(range(len_lo, len_hi + 1))
    per_job = max(1, -(-num_designs // (len(ncaa_options) * len(lengths))))
    jobs: List[Dict[str, Any]] = []
    for ncaa in ncaa_options:
        for length in lengths:
            stem = f"bicyc_{ncaa or 'none'}_L{length}"
            job_dir = root / stem
            job_dir.mkdir(parents=True, exist_ok=True)
            residues, sez, _letters, _bonds = rfd4_adapter.build_bicyclic_skeleton(
                seed_letters, ncaa, length, anchors, sez_atoms)
            motif_points = np.array([point for _chain, atoms in residues for point in atoms.values()])
            shift = pocket_center - motif_points.mean(axis=0)
            combined_cif = job_dir / f"{stem}_combined.cif"
            target_chains = rfd4_adapter.write_combined_cif(
                combined_cif, stem,
                [(chain, {name: point + shift for name, point in atoms.items()})
                 for chain, atoms in residues],
                [(name, point + shift) for name, point in sez],
                linker_ccd, staged_target)
            spec_text = rfd4_adapter.build_spec(
                stem, len(seed_letters), bool(ncaa), len(residues), target_chains,
                rfd4_adapter.chain_centroids(combined_cif))
            jobs.append(_job(
                job_dir, spec_text,
                [rfd4_adapter.combined_template_input(combined_cif)], per_job))
    if not jobs:
        raise ValueError("双环 rfd4 设计的长度窗口为空。")
    return jobs


def _job(job_dir: Path, spec: Optional[str], template_inputs, num_designs: int) -> Dict[str, Any]:
    job_dir.mkdir(parents=True, exist_ok=True)
    return {
        "dir": job_dir,
        "archive": job_dir / "generation.zip",
        "spec": spec,
        "template_inputs": template_inputs,
        "num_designs": num_designs,
    }


def read_generation_archive(archive: Path, bicyclic: bool) -> List[Dict[str, Any]]:
    with zipfile.ZipFile(archive) as generated:
        names = generated.namelist()
        if bicyclic:
            candidates = []
            for name in sorted(n for n in names if n.endswith("_generated.cif")):
                sequence, modifications, cys_positions = rfd4_adapter.binder_from_cif_text(
                    generated.read(name).decode("utf-8"))
                if len(cys_positions) != 3:
                    print(f"[rfd4] 跳过 {name}：双环肽需要恰好 3 个 Cys 锚点，得到 {cys_positions}",
                          file=sys.stderr)
                    continue
                candidates.append({
                    "sequence": sequence,
                    "modifications": modifications,
                    "cys_positions": cys_positions,
                })
            return candidates
        summary_name = next((n for n in names if n.endswith("summary.json")), None)
        if not summary_name:
            raise RuntimeError(f"RFD4 生成归档缺少 summary.json：{archive}")
        summary = json.loads(generated.read(summary_name).decode("utf-8"))
        return [
            {"sequence": candidate["sequence"], "modifications": [], "cys_positions": []}
            for candidate in rfd4_adapter.candidates_from_summary(summary)
        ]


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
