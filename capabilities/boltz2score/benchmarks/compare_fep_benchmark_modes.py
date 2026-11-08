#!/usr/bin/env python3
"""Compare Boltz2Score modes against FEP-benchmark baselines and ligand activity."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import gemmi
import pandas as pd
from rdkit import Chem
from rdkit.Chem import Descriptors
import torch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SUPPORTED_ACTIVITY_KINDS = ("IC50", "KI", "KD", "EC50")
DEFAULT_MODE_ORDER = ("score", "pose", "refine", "interface")
INTERESTING_MODE_METRICS = (
    "affinity_pred_value",
    "affinity_probability_binary",
)
AFFINITY_MW_MODEL_COEF = 1.03525938
AFFINITY_MW_COEF = -0.59992683
AFFINITY_MW_BIAS = 2.83288489


def affinity_log10_um_to_pic50(value: float) -> float:
    return 6.0 - float(value)


def affinity_apply_mw_correction(value: float, molecular_weight: float) -> float:
    return (
        AFFINITY_MW_MODEL_COEF * float(value)
        + AFFINITY_MW_COEF * (float(molecular_weight) ** 0.3)
        + AFFINITY_MW_BIAS
    )


def normalize_ligand_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"-?\d+\.0+", text):
        return str(int(float(text)))
    try:
        numeric = float(text)
    except Exception:
        normalized = re.sub(r"[\s/]+", "_", text)
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized)
        normalized = re.sub(r"_+", "_", normalized).strip("_")
        return normalized or text
    if numeric.is_integer():
        return str(int(numeric))
    return text


def slugify_metric(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(name).strip()).strip("_").lower()
    return text or "metric"


def parse_mode_arg(raw_value: str) -> tuple[str, Path]:
    if "=" not in raw_value:
        raise ValueError(f"Invalid --mode input {raw_value!r}; expected name=path.")
    name, path_str = raw_value.split("=", 1)
    mode_name = name.strip()
    if not mode_name:
        raise ValueError(f"Invalid --mode input {raw_value!r}; empty mode name.")
    mode_path = Path(path_str).expanduser().resolve()
    if not mode_path.exists():
        raise FileNotFoundError(f"Mode path not found: {mode_path}")
    return mode_name, mode_path


def parse_gpu_ids(raw_value: str | None) -> tuple[int, ...]:
    if not raw_value:
        return ()
    gpu_ids: list[int] = []
    seen: set[int] = set()
    for chunk in str(raw_value).split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError as exc:
            raise ValueError(f"Invalid --gpu_ids entry {token!r}; expected comma-separated integers.") from exc
        if value < 0:
            raise ValueError("--gpu_ids values must be non-negative integers.")
        if value not in seen:
            gpu_ids.append(value)
            seen.add(value)
    return tuple(gpu_ids)


def default_gpu_ids(limit: int = 4) -> tuple[int, ...]:
    env_value = os.environ.get("COMPARE_FEP_GPU_IDS") or os.environ.get("CUDA_VISIBLE_DEVICES")
    gpu_ids = parse_gpu_ids(env_value)
    if gpu_ids:
        return gpu_ids[:limit]
    if not torch.cuda.is_available():
        return ()
    return tuple(range(min(limit, torch.cuda.device_count())))


def discover_benchmark_dirs(root_path: Path) -> list[Path]:
    if (root_path / "ligands.sdf").exists():
        return [root_path]
    return sorted(
        child
        for child in root_path.iterdir()
        if child.is_dir() and (child / "ligands.sdf").exists()
    )


def default_mode_candidates(benchmark_dir: Path, mode_name: str) -> list[Path]:
    benchmark_name = benchmark_dir.name
    repo_results = REPO_ROOT / "results"
    return [
        benchmark_dir / "boltz2score_modes" / mode_name,
        repo_results / f"{benchmark_name}_{mode_name}",
        repo_results / benchmark_name / mode_name,
        benchmark_dir / mode_name,
        benchmark_dir / f"{benchmark_name}_{mode_name}",
        benchmark_dir / "results" / mode_name,
        benchmark_dir / "results" / f"{benchmark_name}_{mode_name}",
    ]


def resolve_default_modes_for_benchmark(benchmark_dir: Path) -> list[tuple[str, Path]]:
    resolved: list[tuple[str, Path]] = []
    for mode_name in DEFAULT_MODE_ORDER:
        for candidate in default_mode_candidates(benchmark_dir, mode_name):
            if candidate.exists():
                resolved.append((mode_name, candidate.resolve()))
                break
    return resolved


def generated_mode_output_dir(benchmark_dir: Path, mode_name: str) -> Path:
    return benchmark_dir / "boltz2score_modes" / mode_name


def expected_ligand_ids_from_sdf(sdf_path: Path) -> set[str]:
    supplier = Chem.SDMolSupplier(str(sdf_path), removeHs=False)
    ligand_ids: set[str] = set()
    for mol in supplier:
        if mol is None:
            continue
        if mol.HasProp("Name"):
            raw_name = mol.GetProp("Name")
        elif mol.HasProp("_Name"):
            raw_name = mol.GetProp("_Name")
        else:
            raw_name = ""
        ligand_id = normalize_ligand_id(raw_name)
        if ligand_id:
            ligand_ids.add(ligand_id)
    return ligand_ids


def detect_protein_file(benchmark_dir: Path) -> Path:
    candidates = sorted(
        path
        for path in benchmark_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".pdb", ".cif", ".mmcif", ".ent"}
    )
    if not candidates:
        raise FileNotFoundError(f"No protein structure file found in {benchmark_dir}")
    if len(candidates) > 1:
        prepared = [path for path in candidates if "prepared" in path.name.lower()]
        if len(prepared) == 1:
            return prepared[0]
    return candidates[0]


def detect_target_chains(protein_file: Path) -> tuple[str, ...]:
    structure = gemmi.read_structure(str(protein_file))
    chain_names: list[str] = []
    seen: set[str] = set()
    for model in structure:
        for chain in model:
            chain_name = str(chain.name or "").strip()
            if not chain_name or chain_name in seen:
                continue
            seen.add(chain_name)
            chain_names.append(chain_name)
    if not chain_names:
        raise RuntimeError(f"Failed to detect target chains from {protein_file}")
    return tuple(chain_names)


def extract_ligand_id_from_record_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "__" not in text:
        return normalize_ligand_id(text)
    suffix = text.split("__", 1)[1]
    match = re.match(r"^\d+_(.+)$", suffix)
    if match:
        return normalize_ligand_id(match.group(1))
    return normalize_ligand_id(suffix)

def choose_affinity_json(record_dir: Path) -> Path | None:
    affinity_files = sorted(record_dir.glob("affinity_*.json"))
    return affinity_files[0] if affinity_files else None


def load_completed_ligand_ids_from_record_dirs(mode_dir: Path) -> set[str]:
    ligand_ids: set[str] = set()
    for record_dir in discover_mode_record_dirs(mode_dir):
        ligand_id = extract_ligand_id_from_record_id(record_dir.name)
        if not ligand_id:
            continue
        affinity_path = choose_affinity_json(record_dir)
        if affinity_path is None:
            continue
        ligand_ids.add(ligand_id)
    return ligand_ids


def mode_result_exists(
    mode_dir: Path,
    expected_ligand_ids: set[str],
) -> bool:
    if not mode_dir.exists():
        return False
    if not expected_ligand_ids:
        return False
    completed_ligand_ids = load_completed_ligand_ids_from_record_dirs(mode_dir)
    return expected_ligand_ids.issubset(completed_ligand_ids)


def mode_completion_status(
    mode_dir: Path,
    expected_ligand_ids: set[str],
) -> tuple[int, int]:
    completed_ligand_ids = load_completed_ligand_ids_from_record_dirs(mode_dir)
    return len(completed_ligand_ids & expected_ligand_ids), len(expected_ligand_ids)


def ensure_default_mode_results(benchmark_dir: Path, args: argparse.Namespace) -> list[tuple[str, Path]]:
    resolved: list[tuple[str, Path]] = []
    expected_ligand_ids = expected_ligand_ids_from_sdf(benchmark_dir / "ligands.sdf")
    missing_jobs: list[tuple[str, Path, list[str], str]] = []
    for mode_name in DEFAULT_MODE_ORDER:
        output_dir = generated_mode_output_dir(benchmark_dir, mode_name)
        if not mode_result_exists(output_dir, expected_ligand_ids):
            completed_count, total_count = mode_completion_status(
                output_dir,
                expected_ligand_ids,
            )
            if completed_count > 0:
                print(
                    f"[{benchmark_dir.name}] Incomplete mode `{mode_name}`: "
                    f"{completed_count}/{total_count} ligands complete. Re-running -> {output_dir}"
                )
            else:
                print(f"[{benchmark_dir.name}] Running mode `{mode_name}` -> {output_dir}")
            cmd = build_boltz2score_command(benchmark_dir, mode_name, output_dir, args)
            missing_jobs.append((mode_name, output_dir, cmd, benchmark_dir.name))
        else:
            completed_count, total_count = mode_completion_status(
                output_dir,
                expected_ligand_ids,
            )
            print(
                f"[{benchmark_dir.name}] Reusing existing mode `{mode_name}` "
                f"({completed_count}/{total_count}) -> {output_dir}"
            )
        resolved.append((mode_name, output_dir))
    run_missing_mode_jobs(missing_jobs, args)
    return resolved


def build_boltz2score_command(
    benchmark_dir: Path,
    mode_name: str,
    output_dir: Path,
    args: argparse.Namespace,
) -> list[str]:
    protein_file = detect_protein_file(benchmark_dir)
    ligand_file = benchmark_dir / "ligands.sdf"
    target_chains = detect_target_chains(protein_file)
    ligand_chain = str(getattr(args, "ligand_chain", None) or "L").strip() or "L"
    cmd = [
        sys.executable,
        str((REPO_ROOT / "boltz2score.py").resolve()),
        "--mode",
        mode_name,
        "--protein_file",
        str(protein_file),
        "--ligand_file",
        str(ligand_file),
        "--output_dir",
        str(output_dir),
        "--devices",
        str(args.devices),
        "--num_workers",
        str(args.num_workers),
        "--trainer_precision",
        str(args.trainer_precision),
        "--enable_affinity",
        "--target_chain",
        ",".join(target_chains),
        "--ligand_chain",
        ligand_chain,
    ]
    if args.accelerator:
        cmd.extend(["--accelerator", str(args.accelerator)])
    if args.cache:
        cmd.extend(["--cache", str(args.cache)])
    if args.checkpoint:
        cmd.extend(["--checkpoint", str(args.checkpoint)])
    if args.compute_ipsae:
        cmd.append("--compute_ipsae")
    return cmd


def run_single_mode_job(
    benchmark_name: str,
    mode_name: str,
    output_dir: Path,
    cmd: list[str],
    *,
    gpu_id: int | None,
) -> None:
    env = os.environ.copy()
    launch_cmd = list(cmd)
    if gpu_id is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        devices_index = launch_cmd.index("--devices") + 1
        launch_cmd[devices_index] = "1"
        if "--accelerator" not in launch_cmd:
            launch_cmd.extend(["--accelerator", "gpu"])
        print(f"[{benchmark_name}] Launching mode `{mode_name}` on GPU {gpu_id} -> {output_dir}")
    subprocess.run(launch_cmd, check=True, env=env)


def run_missing_mode_jobs(
    jobs: list[tuple[str, Path, list[str], str]],
    args: argparse.Namespace,
) -> None:
    if not jobs:
        return
    gpu_ids = parse_gpu_ids(getattr(args, "gpu_ids", None)) or default_gpu_ids()
    parallel_jobs = int(
        getattr(args, "parallel_mode_jobs", None) or min(len(gpu_ids), len(DEFAULT_MODE_ORDER)) or 1
    )
    if gpu_ids:
        print(
            f"[AutoRun] Using GPU IDs {list(gpu_ids)} with parallel_mode_jobs={parallel_jobs} "
            f"for modes {list(DEFAULT_MODE_ORDER)}"
        )
    else:
        print("[AutoRun] No GPU IDs selected; running mode auto-runs without explicit GPU pinning.")
    if len(gpu_ids) <= 1 or parallel_jobs <= 1 or len(jobs) == 1:
        gpu_id = gpu_ids[0] if len(gpu_ids) == 1 else None
        for mode_name, output_dir, cmd, benchmark_name in jobs:
            run_single_mode_job(
                benchmark_name,
                mode_name,
                output_dir,
                cmd,
                gpu_id=gpu_id,
            )
        return

    if len(gpu_ids) >= len(DEFAULT_MODE_ORDER) and parallel_jobs >= len(DEFAULT_MODE_ORDER):
        futures: dict[concurrent.futures.Future[None], tuple[str, str, int]] = {}
        mode_to_gpu = {
            mode_name: gpu_ids[index]
            for index, mode_name in enumerate(DEFAULT_MODE_ORDER)
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(DEFAULT_MODE_ORDER)) as executor:
            for mode_name, output_dir, cmd, benchmark_name in jobs:
                gpu_id = mode_to_gpu.get(mode_name, gpu_ids[0])
                future = executor.submit(
                    run_single_mode_job,
                    benchmark_name,
                    mode_name,
                    output_dir,
                    cmd,
                    gpu_id=gpu_id,
                )
                futures[future] = (benchmark_name, mode_name, gpu_id)
            for future in concurrent.futures.as_completed(futures):
                future.result()
        return

    worker_count = min(len(jobs), len(gpu_ids), parallel_jobs)
    futures: dict[concurrent.futures.Future[None], tuple[str, str, int]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        for index, (mode_name, output_dir, cmd, benchmark_name) in enumerate(jobs[:worker_count]):
            gpu_id = gpu_ids[index]
            future = executor.submit(
                run_single_mode_job,
                benchmark_name,
                mode_name,
                output_dir,
                cmd,
                gpu_id=gpu_id,
            )
            futures[future] = (benchmark_name, mode_name, gpu_id)
        next_job_index = worker_count
        while futures:
            done, _ = concurrent.futures.wait(
                futures,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                benchmark_name, mode_name, gpu_id = futures.pop(future)
                future.result()
                if next_job_index < len(jobs):
                    next_mode_name, next_output_dir, next_cmd, next_benchmark_name = jobs[next_job_index]
                    next_job_index += 1
                    next_future = executor.submit(
                        run_single_mode_job,
                        next_benchmark_name,
                        next_mode_name,
                        next_output_dir,
                        next_cmd,
                        gpu_id=gpu_id,
                    )
                    futures[next_future] = (next_benchmark_name, next_mode_name, gpu_id)




def parse_numeric(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def infer_activity_kind(property_name: str) -> str | None:
    for kind in SUPPORTED_ACTIVITY_KINDS:
        if re.search(kind, property_name, flags=re.IGNORECASE):
            return kind
    return None


def infer_activity_unit(property_name: str) -> str | None:
    match = re.search(r"\[([^\]]+)\]", property_name)
    if match:
        return match.group(1).strip()
    normalized = property_name.replace("µ", "u")
    for unit in ("mM", "uM", "nM", "pM", "M"):
        pattern = rf"(^|[^A-Za-z]){re.escape(unit)}([^A-Za-z]|$)"
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return unit
    return None


def unit_to_molar_factor(unit: str | None) -> float | None:
    if unit is None:
        return None
    normalized = unit.strip().replace("µ", "u").lower()
    mapping = {
        "m": 1.0,
        "mm": 1e-3,
        "um": 1e-6,
        "nm": 1e-9,
        "pm": 1e-12,
    }
    return mapping.get(normalized)


def ligand_activity_to_dg_kcal(molar_value: float, temperature_kelvin: float) -> float:
    gas_constant_kcal = 0.00198720425864083
    return gas_constant_kcal * temperature_kelvin * math.log(molar_value)


def find_activity_property(mol: Chem.Mol) -> tuple[str, str] | None:
    candidates: list[tuple[int, str, str]] = []
    for prop_name in mol.GetPropNames():
        stripped = str(prop_name).strip()
        kind = infer_activity_kind(stripped)
        if kind is None:
            continue
        value = mol.GetProp(prop_name)
        if not str(value).strip():
            continue
        priority = SUPPORTED_ACTIVITY_KINDS.index(kind)
        candidates.append((priority, stripped, str(value)))
    if candidates:
        candidates.sort(key=lambda item: item[0])
        _, prop_name, value = candidates[0]
        return prop_name, value
    return None


def load_ligand_activity_table(sdf_path: Path, temperature_kelvin: float) -> tuple[pd.DataFrame, str | None]:
    supplier = Chem.SDMolSupplier(str(sdf_path), removeHs=False)
    rows: list[dict[str, object]] = []
    chosen_property_name: str | None = None

    for mol in supplier:
        if mol is None:
            continue
        ligand_id = normalize_ligand_id(
            mol.GetProp("Name") if mol.HasProp("Name") else mol.GetProp("_Name")
        )
        if not ligand_id:
            continue
        smiles = Chem.MolToSmiles(Chem.RemoveHs(Chem.Mol(mol)), isomericSmiles=True)

        activity_property = find_activity_property(mol)
        activity_name = None
        activity_kind = None
        activity_raw = None
        activity_unit = None
        activity_molar = None
        activity_pic50 = None
        activity_dg = None
        if activity_property is not None:
            activity_name, activity_raw = activity_property
            chosen_property_name = chosen_property_name or activity_name
            activity_kind = infer_activity_kind(activity_name)
            activity_numeric = parse_numeric(activity_raw)
            activity_unit = infer_activity_unit(activity_name)
            unit_factor = unit_to_molar_factor(activity_unit)
            if activity_numeric is not None and unit_factor is not None and activity_numeric > 0:
                activity_molar = activity_numeric * unit_factor
                activity_pic50 = -math.log10(activity_molar)
                activity_dg = ligand_activity_to_dg_kcal(activity_molar, temperature_kelvin)

        rows.append(
            {
                "ligand_id": ligand_id,
                "ligand_smiles": smiles,
                "activity_kind": activity_kind or "",
                "activity_property": activity_name or "",
                "activity_raw_value": activity_raw or "",
                "activity_unit": activity_unit or "",
                "activity_molar": activity_molar,
                "activity_pic50": activity_pic50,
                "activity_pvalue": activity_pic50,
                "activity_dg_kcal": activity_dg,
            }
        )

    return pd.DataFrame(rows), chosen_property_name


def load_fep_relative_csv(csv_path: Path, label: str) -> pd.DataFrame:
    if not csv_path.exists():
        return pd.DataFrame(columns=["ligand_id"])
    frame = pd.read_csv(csv_path)
    rows: list[dict[str, object]] = []
    for _, row in frame.iterrows():
        ligand_id = normalize_ligand_id(row.get("Ligand"))
        if not ligand_id:
            continue
        rows.append(
            {
                "ligand_id": ligand_id,
                f"{label}_pred_dg": parse_numeric(row.get("Pred. ΔG")),
                f"{label}_pred_error": parse_numeric(row.get("Pred. Error")),
                f"{label}_exp_dg": parse_numeric(row.get("Exp. ΔG")),
            }
        )
    return pd.DataFrame(rows)


def load_simple_score_csv(
    csv_path: Path,
    id_column: str,
    column_map: dict[str, str],
) -> pd.DataFrame:
    if not csv_path.exists():
        return pd.DataFrame(columns=["ligand_id"])
    frame = pd.read_csv(csv_path)
    rows: list[dict[str, object]] = []
    for _, row in frame.iterrows():
        ligand_id = normalize_ligand_id(row.get(id_column))
        if not ligand_id:
            continue
        payload = {"ligand_id": ligand_id}
        for source_column, target_column in column_map.items():
            payload[target_column] = parse_numeric(row.get(source_column))
        rows.append(payload)
    return pd.DataFrame(rows)


def find_generic_id_column(columns: list[str]) -> str | None:
    priorities = ["record_id", "ligand_id", "id", "Ligand", "ligand", "Title", "title", "Name", "name"]
    for candidate in priorities:
        if candidate in columns:
            return candidate
    return None


def load_generic_mode_csv(mode_name: str, csv_path: Path) -> pd.DataFrame:
    frame = pd.read_csv(csv_path)
    id_column = find_generic_id_column(list(frame.columns))
    if id_column is None:
        raise ValueError(f"Cannot infer ligand ID column from {csv_path}")
    rows: list[dict[str, object]] = []
    for _, row in frame.iterrows():
        ligand_id = normalize_ligand_id(row.get("ligand_id")) if "ligand_id" in frame.columns else ""
        if not ligand_id:
            if id_column == "record_id":
                ligand_id = extract_ligand_id_from_record_id(row.get(id_column))
            else:
                ligand_id = normalize_ligand_id(row.get(id_column))
        if not ligand_id:
            continue
        payload: dict[str, object] = {"ligand_id": ligand_id}
        for column in frame.columns:
            if column == id_column:
                continue
            normalized_column = slugify_metric(column)
            if not normalized_column.startswith("affinity"):
                continue
            numeric = parse_numeric(row.get(column))
            if numeric is None:
                continue
            payload[f"{mode_name}__{normalized_column}"] = numeric
        rows.append(payload)
    return pd.DataFrame(rows)


def discover_mode_record_dirs(mode_dir: Path) -> list[Path]:
    record_dirs: set[Path] = set()
    for pattern in ("affinity_*.json",):
        for path in mode_dir.rglob(pattern):
            record_dirs.add(path.parent)
    return sorted(record_dirs)


def load_mode_directory(mode_name: str, mode_dir: Path) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for record_dir in discover_mode_record_dirs(mode_dir):
        ligand_id = extract_ligand_id_from_record_id(record_dir.name)
        if not ligand_id:
            continue
        affinity_path = choose_affinity_json(record_dir)
        if affinity_path is None:
            continue
        payload = json.loads(affinity_path.read_text())
        row: dict[str, object] = {"ligand_id": ligand_id}
        for key in INTERESTING_MODE_METRICS:
            if key in payload:
                numeric = parse_numeric(payload.get(key))
                if numeric is not None:
                    row[f"{mode_name}__{key}"] = numeric
                    if key == "affinity_pred_value":
                        row[f"{mode_name}__affinity_pic50"] = affinity_log10_um_to_pic50(numeric)
        if len(row) > 1:
            rows.append(row)
    return pd.DataFrame(rows)


def load_mode_table(mode_name: str, mode_path: Path) -> pd.DataFrame:
    if mode_path.is_file():
        return load_generic_mode_csv(mode_name, mode_path)
    return load_mode_directory(mode_name, mode_path)


def merge_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    merged = frames[0].copy()
    for frame in frames[1:]:
        if frame.empty:
            continue
        merged = merged.merge(frame, on="ligand_id", how="outer")
    return merged


def add_baseline_score_columns(frame: pd.DataFrame) -> pd.DataFrame:
    score_map = {
        "activity_score_from_dg": ("activity_dg_kcal", -1.0),
        "fep_5ns_score": ("fep_5ns_pred_dg", -1.0),
        "fep_20ns_score": ("fep_20ns_pred_dg", -1.0),
        "mmgbsa_score": ("mmgbsa_dg_bind", -1.0),
        "docking_glide_score_better": ("docking_glide_gscore", -1.0),
        "docking_inplace_glide_score_better": ("docking_inplace_glide_gscore", -1.0),
    }
    for target_column, (source_column, factor) in score_map.items():
        if source_column in frame.columns:
            frame[target_column] = frame[source_column] * factor
    return frame


def add_mw_corrected_affinity_columns(frame: pd.DataFrame, mode_names: list[str]) -> pd.DataFrame:
    if "ligand_smiles" not in frame.columns:
        return frame

    def smiles_to_molwt(smiles: object) -> float | None:
        text = str(smiles or "").strip()
        if not text or text.lower() == "nan":
            return None
        mol = Chem.MolFromSmiles(text)
        if mol is None:
            return None
        return float(Descriptors.MolWt(mol))

    molecular_weight = frame["ligand_smiles"].map(smiles_to_molwt)
    for mode_name in mode_names:
        source_column = f"{mode_name}__affinity_pred_value"
        if source_column not in frame.columns:
            continue
        corrected_column = f"{mode_name}__affinity_pred_value_mw"
        corrected_pic50_column = f"{mode_name}__affinity_pic50_mw"
        source_values = pd.to_numeric(frame[source_column], errors="coerce")
        frame[corrected_column] = [
            affinity_apply_mw_correction(value, mw)
            if pd.notna(value) and pd.notna(mw)
            else math.nan
            for value, mw in zip(source_values, molecular_weight)
        ]
        frame[corrected_pic50_column] = frame[corrected_column].map(
            lambda value: affinity_log10_um_to_pic50(value) if pd.notna(value) else math.nan
        )
    return frame


def collect_metric_columns(frame: pd.DataFrame, mode_names: list[str]) -> list[str]:
    baseline_columns = [
        "activity_pic50",
        "activity_score_from_dg",
        "fep_5ns_score",
        "fep_20ns_score",
        "mmgbsa_score",
        "docking_glide_score_better",
        "docking_inplace_glide_score_better",
    ]
    mode_columns = [
        column
        for column in frame.columns
        if any(column.startswith(f"{mode_name}__affinity") for mode_name in mode_names)
    ]
    return [column for column in baseline_columns + sorted(mode_columns) if column in frame.columns]


def correlation_row(frame: pd.DataFrame, metric_column: str, target_column: str) -> dict[str, object] | None:
    subset = frame[[metric_column, target_column]].dropna()
    if len(subset) < 3:
        return None
    pearson = subset[metric_column].corr(subset[target_column], method="pearson")
    spearman = subset[metric_column].corr(subset[target_column], method="spearman")
    return {
        "metric": metric_column,
        "target": target_column,
        "n": int(len(subset)),
        "pearson": float(pearson) if pearson is not None else math.nan,
        "spearman": float(spearman) if spearman is not None else math.nan,
    }


def build_correlation_table(frame: pd.DataFrame, mode_names: list[str]) -> pd.DataFrame:
    metric_columns = collect_metric_columns(frame, mode_names)
    target_columns = [
        column
        for column in (
            "activity_pic50",
            "activity_score_from_dg",
        )
        if column in frame.columns
    ]
    rows: list[dict[str, object]] = []
    for metric_column in metric_columns:
        for target_column in target_columns:
            if metric_column == target_column:
                continue
            row = correlation_row(frame, metric_column, target_column)
            if row is not None:
                rows.append(row)
    correlations = pd.DataFrame(rows)
    if not correlations.empty:
        correlations = correlations.sort_values(
            by=["target", "spearman", "pearson", "n"],
            ascending=[True, False, False, False],
        )
    return correlations


def write_summary_markdown(
    output_path: Path,
    benchmark_dir: Path,
    merged: pd.DataFrame,
    correlations: pd.DataFrame,
    mode_names: list[str],
    activity_property_name: str | None,
) -> None:
    lines = [
        f"# Benchmark Comparison: {benchmark_dir.name}",
        "",
        f"- Ligands in merged table: {len(merged)}",
        f"- Modes compared: {', '.join(mode_names) if mode_names else '(none supplied)'}",
        f"- Activity property from SDF: {activity_property_name or 'not found'}",
        "- Mode comparison uses affinity outputs, not confidence/IPSAE outputs.",
        "- `mode__affinity_pic50` is derived from Boltz `affinity_pred_value` as `6 - log10(IC50 in uM)`.",
        "- `mode__affinity_pred_value_mw` follows the Boltz molecular-weight correction formula used for the affinity value head.",
        "- `mode__affinity_pic50_mw` is derived from that value as `6 - log10(IC50 in uM)`.",
        "",
    ]
    for target in ("activity_pic50", "activity_score_from_dg"):
        subset = correlations[correlations["target"] == target]
        if subset.empty:
            continue
        lines.append(f"## Top correlations vs `{target}`")
        lines.append("")
        primary_rows = subset.head(10)
        lines.append("| metric | n | pearson | spearman |")
        lines.append("| --- | ---: | ---: | ---: |")
        for _, row in primary_rows.iterrows():
            lines.append(
                f"| `{row['metric']}` | {int(row['n'])} | {row['pearson']:.3f} | {row['spearman']:.3f} |"
            )
        lines.append("")
    output_path.write_text("\n".join(lines).rstrip() + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare 4 Boltz2Score mode affinity predictions against ligand activity, FEP, MMGBSA, and docking baselines."
    )
    parser.add_argument(
        "--benchmark_dir",
        type=str,
        default="data",
        help="Benchmark directory, or a root containing multiple benchmark subdirectories. Default: data",
    )
    parser.add_argument(
        "--mode",
        action="append",
        default=[],
        help="Mode input as name=path. Path can be a result directory or a CSV. Repeat for score/pose/refine/interface.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=None,
        help="Output directory. Default: <benchmark_dir>/mode_benchmark_compare",
    )
    parser.add_argument(
        "--temperature_kelvin",
        type=float,
        default=298.15,
        help="Temperature used when converting activity to approximate ΔG (default: 298.15 K).",
    )
    parser.add_argument(
        "--compare_only",
        action="store_true",
        help="Do not run missing Boltz2Score mode results; compare only existing result directories/CSVs.",
    )
    parser.add_argument("--cache", type=str, default=None, help="Optional cache dir passed through to boltz2score.py.")
    parser.add_argument("--checkpoint", type=str, default=None, help="Optional checkpoint path passed through to boltz2score.py.")
    parser.add_argument("--devices", type=int, default=1, help="Devices passed through when auto-running modes.")
    parser.add_argument("--accelerator", type=str, default=None, help="Accelerator passed through when auto-running modes.")
    parser.add_argument(
        "--gpu_ids",
        type=str,
        default=None,
        help="Comma-separated GPU IDs for parallel auto-runs. Default: auto-detect up to 4 visible GPUs. With 4 IDs, score/pose/refine/interface are pinned in that order.",
    )
    parser.add_argument(
        "--parallel_mode_jobs",
        type=int,
        default=None,
        help="Maximum number of auto-run mode subprocesses to launch in parallel. Default: auto = number of selected GPUs, capped at 4.",
    )
    parser.add_argument("--num_workers", type=int, default=0, help="num_workers passed through when auto-running modes.")
    parser.add_argument("--trainer_precision", type=str, default="32", help="trainer_precision passed through when auto-running modes.")
    parser.add_argument(
        "--ligand_chain",
        type=str,
        default="L",
        help="Ligand chain passed through when auto-running modes. Default: L",
    )
    parser.set_defaults(compute_ipsae=True)
    parser.add_argument(
        "--compute_ipsae",
        dest="compute_ipsae",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no_compute_ipsae",
        dest="compute_ipsae",
        action="store_false",
        help="Disable IPSAE when auto-running modes. Default behavior is to compute IPSAE.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    benchmark_root = Path(args.benchmark_dir).expanduser().resolve()
    if not benchmark_root.exists():
        raise FileNotFoundError(f"Benchmark directory not found: {benchmark_root}")

    benchmark_dirs = discover_benchmark_dirs(benchmark_root)
    if not benchmark_dirs:
        raise FileNotFoundError(
            f"No benchmark directories with ligands.sdf found under {benchmark_root}"
        )

    explicit_modes = [parse_mode_arg(raw_mode) for raw_mode in args.mode]
    batch_output_root = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else None
    )
    if batch_output_root is not None:
        batch_output_root.mkdir(parents=True, exist_ok=True)

    for benchmark_dir in benchmark_dirs:
        output_dir = (
            (batch_output_root / benchmark_dir.name)
            if batch_output_root is not None and len(benchmark_dirs) > 1
            else (
                batch_output_root
                if batch_output_root is not None
                else (benchmark_dir / "mode_benchmark_compare")
            )
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        ligands_sdf = benchmark_dir / "ligands.sdf"
        activity_df, activity_property_name = load_ligand_activity_table(
            ligands_sdf,
            temperature_kelvin=float(args.temperature_kelvin),
        )

        frames = [
            activity_df,
            load_fep_relative_csv(benchmark_dir / "results_5ns.csv", "fep_5ns"),
            load_fep_relative_csv(benchmark_dir / "results_20ns.csv", "fep_20ns"),
            load_simple_score_csv(
                benchmark_dir / "results_glide_dock.csv",
                id_column="Title",
                column_map={
                    "glide gscore": "docking_glide_gscore",
                    "Exp. ΔG": "docking_exp_dg",
                },
            ),
            load_simple_score_csv(
                benchmark_dir / "results_glide_score_inplace.csv",
                id_column="Title",
                column_map={
                    "glide gscore": "docking_inplace_glide_gscore",
                    "Exp. ΔG": "docking_inplace_exp_dg",
                },
            ),
            load_simple_score_csv(
                benchmark_dir / "results_prime.csv",
                id_column="Title",
                column_map={
                    "MMGBSA dG Bind": "mmgbsa_dg_bind",
                    "Exp. ΔG": "mmgbsa_exp_dg",
                },
            ),
        ]

        if explicit_modes:
            benchmark_modes = explicit_modes
        elif args.compare_only:
            benchmark_modes = resolve_default_modes_for_benchmark(benchmark_dir)
        else:
            benchmark_modes = ensure_default_mode_results(benchmark_dir, args)
        mode_names: list[str] = []
        if not benchmark_modes:
            print(f"[Info] No mode result paths found for {benchmark_dir.name}; baseline-only comparison.")
        for mode_name, mode_path in benchmark_modes:
            mode_names.append(mode_name)
            frames.append(load_mode_table(mode_name, mode_path))

        merged = merge_frames(frames).sort_values(by="ligand_id").reset_index(drop=True)
        merged = add_baseline_score_columns(merged)
        merged = add_mw_corrected_affinity_columns(merged, mode_names)
        correlations = build_correlation_table(merged, mode_names)

        merged_csv = output_dir / "benchmark_mode_compare_merged.csv"
        correlations_csv = output_dir / "benchmark_mode_compare_correlations.csv"
        summary_md = output_dir / "benchmark_mode_compare_summary.md"

        merged.to_csv(merged_csv, index=False)
        correlations.to_csv(correlations_csv, index=False)
        write_summary_markdown(
            summary_md,
            benchmark_dir=benchmark_dir,
            merged=merged,
            correlations=correlations,
            mode_names=mode_names,
            activity_property_name=activity_property_name,
        )

        print(f"[{benchmark_dir.name}] Wrote merged table: {merged_csv}")
        print(f"[{benchmark_dir.name}] Wrote correlations: {correlations_csv}")
        print(f"[{benchmark_dir.name}] Wrote summary: {summary_md}")


if __name__ == "__main__":
    main()
