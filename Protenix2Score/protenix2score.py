#!/usr/bin/env python3
"""Protenix2Score prototype runner (Docker-based).

This tool converts an input PDB/mmCIF to Protenix JSON, then runs Protenix inference
to collect confidence summaries. Unlike Boltz2Score, this is not a true confidence-head-
only scorer yet, because public Protenix runtime does not expose a score-only entrypoint.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_IMAGE = os.environ.get(
    "PROTENIX_DOCKER_IMAGE",
    "ai4s-share-public-cn-beijing.cr.volces.com/release/protenix:1.0.0.4",
)
DEFAULT_SOURCE_DIR = os.environ.get("PROTENIX_SOURCE_DIR", "/data/protenix")
DEFAULT_MODEL_DIR = os.environ.get("PROTENIX_MODEL_DIR", "/data/protenix/model")
DEFAULT_MODEL_NAME = os.environ.get("PROTENIX_MODEL_NAME", "protenix_base_20250630_v1.0.0")
DEFAULT_PYTHON_BIN = os.environ.get("PROTENIX_PYTHON_BIN", "python3")
DEFAULT_DOCKER_EXTRA_ARGS = os.environ.get("PROTENIX_DOCKER_EXTRA_ARGS", "")
DEFAULT_INFER_EXTRA_ARGS = os.environ.get("PROTENIX_INFER_EXTRA_ARGS", "")


def _is_true(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _detect_input_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdb":
        return "pdb"
    if suffix in {".cif", ".mmcif"}:
        return "cif"
    raise ValueError(f"Unsupported input format: {path.name} (only .pdb/.cif/.mmcif)")


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _run_command(command: list[str], log_path: Path, cwd: Path | None = None) -> None:
    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(cwd) if cwd else None,
        )
        tail: list[str] = []
        if proc.stdout is not None:
            for line in proc.stdout:
                log_file.write(line)
                tail.append(line)
                if len(tail) > 200:
                    tail.pop(0)
        return_code = proc.wait()
    if return_code != 0:
        tail_text = "".join(tail)
        raise RuntimeError(
            f"Command failed with exit code {return_code}.\n"
            f"Command: {' '.join(shlex.quote(part) for part in command)}\n"
            f"Log: {log_path}\n"
            f"Last output:\n{tail_text}"
        )


def _build_gpu_arg(raw_visible_devices: str | None) -> str:
    text = str(raw_visible_devices or "").strip()
    if not text:
        return "all"
    tokens = [tok.strip() for tok in text.split(",") if tok.strip()]
    if not tokens:
        return "all"
    for token in tokens:
        if not token.isdigit():
            return "all"
    return f"device={','.join(tokens)}"


def _build_docker_base(
    *,
    image: str,
    input_dir: Path,
    json_dir: Path,
    output_dir: Path,
    source_dir: Path,
    model_dir: Path,
    common_dir: Path,
    gpu_arg: str,
    docker_extra_args: str,
) -> list[str]:
    cmd: list[str] = [
        "docker",
        "run",
        "--rm",
        "--runtime",
        "nvidia",
        "--gpus",
        gpu_arg,
        "--env",
        "PYTHONPATH=/app",
        "--volume",
        f"{input_dir}:/workspace/input:ro",
        "--volume",
        f"{json_dir}:/workspace/json",
        "--volume",
        f"{output_dir}:/workspace/output",
        "--volume",
        f"{source_dir}:/app:ro",
        "--volume",
        f"{model_dir}:/workspace/model:ro",
        "--volume",
        f"{common_dir}:/root/common",
    ]
    if Path("/dev/shm").exists():
        cmd.extend(["--volume", "/dev/shm:/dev/shm"])
    if docker_extra_args.strip():
        cmd.extend(shlex.split(docker_extra_args))
    cmd.append(image)
    return cmd


def _collect_summary_metrics(pred_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(pred_dir.rglob("*_summary_confidence_sample_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            item = {
                "file": str(path.relative_to(pred_dir)),
                "plddt": payload.get("plddt"),
                "ptm": payload.get("ptm"),
                "iptm": payload.get("iptm"),
                "gpde": payload.get("gpde"),
                "ranking_score": payload.get("ranking_score"),
                "chain_ptm": payload.get("chain_ptm"),
                "chain_plddt": payload.get("chain_plddt"),
                "chain_iptm": payload.get("chain_iptm"),
                "chain_pair_iptm": payload.get("chain_pair_iptm"),
                "chain_pair_iptm_global": payload.get("chain_pair_iptm_global"),
            }
            items.append(item)
        except Exception:
            continue
    items.sort(key=lambda x: float(x.get("ranking_score") or -1e9), reverse=True)
    return items


def _normalize_cif_entity_ids(cif_text: str) -> str:
    """Normalize entity IDs by removing gemmi-added '!' suffixes."""
    normalized = re.sub(r"^([A-Z][A-Z0-9]*)!\s+", r"\1 ", cif_text, flags=re.MULTILINE)
    normalized = re.sub(r"([A-Za-z0-9]+)\s+([A-Z][A-Z0-9]*)(!)\s*$", r"\1 \2", normalized, flags=re.MULTILINE)
    normalized = re.sub(r"([A-Z][A-Z0-9]*)(!)\s+\.", r"\1 .", normalized)
    normalized = re.sub(r"([A-Z][A-Z0-9]*)(!)\s+\?", r"\1 ?", normalized)
    normalized = re.sub(r"([A-Z][A-Z0-9]*)(!)(\s+)", r"\1\3", normalized)
    return normalized


def _ensure_polymer_entity_sequences(structure: Any) -> None:
    for entity in structure.entities:
        if entity.entity_type.name != "Polymer":
            continue
        if not entity.subchains:
            continue
        seq: list[str] = []
        for chain in structure[0]:
            for residue in chain:
                if residue.subchain in entity.subchains:
                    seq.append(residue.name)
        if seq:
            entity.full_sequence = seq


def _convert_input_to_cif(input_path: Path, cif_path: Path) -> None:
    suffix = input_path.suffix.lower()
    if suffix not in {".pdb", ".cif", ".mmcif"}:
        raise ValueError(f"Unsupported input format: {input_path.name} (only .pdb/.cif/.mmcif)")
    try:
        import gemmi  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("gemmi is required to convert PDB to CIF for Protenix2Score.") from exc

    if suffix == ".pdb":
        structure = gemmi.read_pdb(str(input_path))
    else:
        structure = gemmi.read_structure(str(input_path))

    # Protenix json_maker relies on entity/polymer annotations in mmCIF.
    # Also enforce label_seq_id for all formats. Missing label_seq_id in atom_site can
    # trigger parser chain over-segmentation (e.g. one polymer chain split into A/B/C).
    structure.setup_entities()
    _ensure_polymer_entity_sequences(structure)
    structure.assign_label_seq_id()
    document = structure.make_mmcif_document()
    cif_text = _normalize_cif_entity_ids(document.as_string())
    cif_path.write_text(cif_text, encoding="utf-8")


def _resolve_components_dir(source_dir: Path) -> Path:
    candidates = [
        source_dir / "release_data",
        source_dir / "common",
        source_dir / "data" / "common",
    ]
    for candidate in candidates:
        if (candidate / "components.cif").exists():
            return candidate
    raise FileNotFoundError(
        "Cannot find components.cif for Protenix conversion. "
        f"Tried: {', '.join(str(item) for item in candidates)}"
    )


def _prepare_common_dir(work_dir: Path, components_dir: Path) -> Path:
    common_dir = work_dir / "common"
    common_dir.mkdir(parents=True, exist_ok=True)
    primary = components_dir / "components.cif"
    if not primary.exists():
        raise FileNotFoundError(f"components.cif not found in: {components_dir}")
    shutil.copyfile(primary, common_dir / "components.cif")
    optional_cache = components_dir / "components.cif.rdkit_mol.pkl"
    if optional_cache.exists():
        shutil.copyfile(optional_cache, common_dir / "components.cif.rdkit_mol.pkl")
    return common_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Protenix2Score prototype: convert structure to Protenix JSON and run Docker inference "
            "to collect confidence summaries."
        )
    )
    parser.add_argument("--input", required=True, help="Input structure file (.pdb/.cif/.mmcif).")
    parser.add_argument("--output_dir", required=True, help="Directory to write outputs.")
    parser.add_argument("--work_dir", default="", help="Optional working directory. Default: auto temporary dir.")
    parser.add_argument("--docker_image", default=DEFAULT_IMAGE, help="Protenix Docker image.")
    parser.add_argument("--protenix_source", default=DEFAULT_SOURCE_DIR, help="Host path to Protenix source repo.")
    parser.add_argument("--model_dir", default=DEFAULT_MODEL_DIR, help="Host path to Protenix model directory.")
    parser.add_argument("--model_name", default=DEFAULT_MODEL_NAME, help="Model name without .pt suffix.")
    parser.add_argument("--python_bin", default=DEFAULT_PYTHON_BIN, help="Python executable inside container.")
    parser.add_argument("--dtype", default="bf16", choices=["bf16", "fp32"], help="Inference dtype.")
    parser.add_argument("--seed", type=int, default=None, help="Optional inference seed.")
    parser.add_argument("--use_msa", default="false", help="Enable MSA feature (true/false).")
    parser.add_argument("--use_template", default="false", help="Enable template feature (true/false).")
    parser.add_argument("--docker_extra_args", default=DEFAULT_DOCKER_EXTRA_ARGS, help="Extra args for `docker run`.")
    parser.add_argument("--infer_extra_args", default=DEFAULT_INFER_EXTRA_ARGS, help="Extra args for inference.py.")
    parser.add_argument("--keep_work", action="store_true", help="Keep working directory.")
    parser.add_argument(
        "--prepare_only",
        action="store_true",
        help="Only prepare Protenix JSON from input structure (skip inference).",
    )
    args = parser.parse_args()

    if shutil.which("docker") is None:
        raise RuntimeError("Docker not found in PATH.")

    input_path = Path(args.input).resolve()
    if not input_path.exists() or not input_path.is_file():
        raise FileNotFoundError(f"Input not found: {input_path}")
    _detect_input_format(input_path)

    source_dir = Path(args.protenix_source).resolve()
    if not source_dir.exists():
        raise FileNotFoundError(f"Protenix source directory not found: {source_dir}")
    if not (source_dir / "runner" / "inference.py").exists():
        raise FileNotFoundError(f"Missing runner/inference.py under: {source_dir}")

    model_dir = Path(args.model_dir).resolve()
    if not model_dir.exists():
        raise FileNotFoundError(f"Model directory not found: {model_dir}")
    checkpoint_name = f"{args.model_name}.pt"
    if not (model_dir / checkpoint_name).exists():
        raise FileNotFoundError(f"Checkpoint not found: {model_dir / checkpoint_name}")
    components_dir = _resolve_components_dir(source_dir)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    auto_tmp = not args.work_dir.strip()
    work_dir = (
        Path(tempfile.mkdtemp(prefix="protenix2score_"))
        if auto_tmp
        else Path(args.work_dir).resolve()
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    input_dir = work_dir / "input"
    json_dir = work_dir / "json"
    pred_dir = work_dir / "pred"
    input_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)
    pred_dir.mkdir(parents=True, exist_ok=True)

    staged_cif = input_dir / "input.cif"
    _convert_input_to_cif(input_path, staged_cif)
    common_dir = _prepare_common_dir(work_dir, components_dir)

    gpu_arg = _build_gpu_arg(os.environ.get("CUDA_VISIBLE_DEVICES"))
    docker_base = _build_docker_base(
        image=args.docker_image,
        input_dir=input_dir,
        json_dir=json_dir,
        output_dir=pred_dir,
        source_dir=source_dir,
        model_dir=model_dir,
        common_dir=common_dir,
        gpu_arg=gpu_arg,
        docker_extra_args=args.docker_extra_args,
    )

    tojson_log = output_dir / "protenix2score_tojson.log"
    tojson_cmd = docker_base + [
        args.python_bin,
        "/app/protenix/data/inference/json_maker.py",
        "-c",
        "/workspace/input/input.cif",
        "-j",
        "/workspace/json/input.json",
    ]
    _run_command(tojson_cmd, tojson_log)

    json_candidates = sorted(json_dir.glob("*.json"))
    if not json_candidates:
        raise RuntimeError("No JSON generated by Protenix tojson step.")
    input_json_path = json_candidates[0]

    out_json_path = output_dir / input_json_path.name
    shutil.copyfile(input_json_path, out_json_path)

    if args.prepare_only:
        prepare_payload = {
            "mode": "prepare-only",
            "input_structure": str(input_path),
            "input_json": input_json_path.name,
            "note": "Prepared Protenix JSON only; inference skipped by --prepare_only."
        }
        metrics_path = output_dir / "protenix2score_metrics.json"
        metrics_path.write_text(json.dumps(prepare_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[Protenix2Score] Prepared JSON: {out_json_path}")
        print(f"[Protenix2Score] Metrics: {metrics_path}")
        return 0

    infer_log = output_dir / "protenix2score_infer.log"
    infer_cmd = docker_base + [
        args.python_bin,
        "/app/runner/inference.py",
        "--model_name",
        args.model_name,
        "--load_checkpoint_dir",
        "/workspace/model",
        "--load_checkpoint_path",
        f"/workspace/model/{checkpoint_name}",
        "--input_json_path",
        f"/workspace/json/{input_json_path.name}",
        "--dump_dir",
        "/workspace/output",
        "--need_atom_confidence",
        "True",
        "--use_msa",
        "true" if _is_true(args.use_msa) else "false",
        "--use_template",
        "true" if _is_true(args.use_template) else "false",
        "--dtype",
        args.dtype,
    ]
    if args.seed is not None:
        infer_cmd.extend(["--seeds", str(int(args.seed))])
    if args.infer_extra_args.strip():
        infer_cmd.extend(shlex.split(args.infer_extra_args))
    _run_command(infer_cmd, infer_log)

    summaries = _collect_summary_metrics(pred_dir)
    if not summaries:
        raise RuntimeError(f"No summary confidence files found under: {pred_dir}")

    best = summaries[0]
    metrics_payload: dict[str, Any] = {
        "mode": "proxy-inference",
        "note": (
            "Current Protenix runtime does not expose a public score-only interface like Boltz2Score. "
            "This result comes from Protenix inference on converted input JSON."
        ),
        "input_structure": str(input_path),
        "input_json": input_json_path.name,
        "model_name": args.model_name,
        "best_summary": best,
        "all_summaries": summaries,
    }

    metrics_path = output_dir / "protenix2score_metrics.json"
    metrics_path.write_text(json.dumps(metrics_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    out_pred_dir = output_dir / "protenix_output"
    _copy_tree(pred_dir, out_pred_dir)

    should_cleanup = auto_tmp and not args.keep_work
    if should_cleanup:
        shutil.rmtree(work_dir, ignore_errors=True)
    else:
        (output_dir / "work_dir.txt").write_text(f"{work_dir}\n", encoding="utf-8")

    print(f"[Protenix2Score] Metrics: {metrics_path}")
    print(f"[Protenix2Score] Best ranking_score: {best.get('ranking_score')}")
    print(f"[Protenix2Score] Output directory: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
