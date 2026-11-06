#!/usr/bin/env python3
"""RFD4-Proteina generation wrapper for V-Bio.

Runs inside the rfd4-proteina-engine image: condition spec YAML/JSON ->
rfd4-generate -> structure CIFs + summary.json.

GPU contract: one container per GPU, dispatched host-side via
`--runtime nvidia --gpus device=N`. This wrapper asserts device availability
and logs the assigned device; it never schedules across GPUs.
Failures are explicit: non-zero exits, timeouts, and missing artifacts all
raise — no silent retries.
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
import time
from pathlib import Path

AA3_TO_AA1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


def _log(msg: str) -> None:
    print(f"[rfd4] {msg}", file=sys.stderr, flush=True)


def _assert_gpu(allow_cpu: bool) -> None:
    import torch

    if torch.cuda.is_available():
        count = torch.cuda.device_count()
        if count != 1:
            raise RuntimeError(
                f"{count} GPUs visible. Generation requires single-GPU "
                "dispatch (host-side --gpus device=N binds exactly one).")
        name = torch.cuda.get_device_name(0)
        total = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        _log(f"GPU: {name}, {total:.0f} GiB (single)")
        return
    if allow_cpu:
        _log("No CUDA device; continuing in CPU diagnostic mode via --allow-cpu")
        return
    raise RuntimeError(
        "No CUDA device available. GPU generation requires single-GPU dispatch "
        "host-side (docker run --runtime nvidia --gpus device=N); "
        "pass --allow-cpu for diagnostics."
    )


def _resolve_spec(spec: Path, out_dir: Path) -> Path:
    """Resolve relative input paths in the condition spec to absolute
    paths and write a resolved copy.

    Hydra switches cwd at runtime, breaking relative input paths in
    the spec; absolute paths are immune. """
    import yaml

    data = yaml.safe_load(spec.read_text())
    spec_dir = spec.resolve().parent
    for design in data.values():
        if not isinstance(design, dict):
            continue
        raw = design.get("input")
        if not raw:
            continue
        input_path = Path(raw)
        if input_path.is_absolute():
            resolved = input_path
        elif input_path.exists():  # relative to cwd (container /workspace) hits directly
            resolved = input_path.resolve()
        else:  # relative to the spec directory
            resolved = (spec_dir / input_path).resolve()
        if not resolved.exists():
            raise FileNotFoundError(
                f"spec input not found: {raw} (resolved to {resolved})")
        design["input"] = str(resolved)
    resolved = out_dir / (spec.stem + "_resolved" + spec.suffix)
    out_dir.mkdir(parents=True, exist_ok=True)
    resolved.write_text(yaml.safe_dump(data, sort_keys=False))
    return resolved


def _build_overrides(args: argparse.Namespace, out_dir: Path) -> list[str]:
    overrides = [
        f"inference_experiment={args.preset}",
        f"inputs={args.resolved_spec}",
        f"out_dir={out_dir}",
        f"num_replicates={args.num_designs}",
        f"seed={args.seed}",
        "compile_lengths=null",
    ]
    for extra in args.extra or []:
        overrides.append(extra)
    return overrides


def _run_generation(overrides: list[str], timeout_min: float) -> None:
    cmd = ["rfd4-generate", *overrides]
    _log("running: " + " ".join(cmd))
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, timeout=timeout_min * 60)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"rfd4-generate timed out after {timeout_min:.0f} min") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"rfd4-generate exited {proc.returncode}")
    _log(f"generation finished in {time.monotonic() - started:.0f} s")


def _collect_cifs(out_dir: Path) -> list[Path]:
    cifs = sorted(out_dir.rglob("*.cif"))
    if not cifs:
        raise RuntimeError(f"no structure artifacts under {out_dir}")
    return cifs


def _chain_summary(cif_path: Path) -> dict:
    """Per-chain residue counts and one-letter sequence; non-standard
    residues are reported as [CCD_NAME]."""
    import gemmi

    st = gemmi.read_structure(str(cif_path))
    st.setup_entities()
    chains = {}
    for chain in st[0]:
        seq = []
        for res in chain:
            name = res.name.strip().upper()
            seq.append(AA3_TO_AA1.get(name, f"[{name}]"))
        chains[chain.name] = {
            "n_residues": len(seq),
            "sequence": "".join(seq),
        }
    return chains


def _write_summary(args: argparse.Namespace, out_dir: Path,
                   cifs: list[Path], elapsed_s: float) -> Path:
    summary = {
        "engine": "rfd4-proteina",
        "spec": str(Path(args.spec).resolve()),
        "preset": args.preset,
        "num_designs": args.num_designs,
        "seed": args.seed,
        "elapsed_seconds": round(elapsed_s, 1),
        "outputs": [],
    }
    for cif in cifs:
        entry = {"cif": str(cif)}
        try:
            entry["chains"] = _chain_summary(cif)
        except Exception as exc:  # keep the path, blank the chain digest
            entry["chains_error"] = str(exc)
        summary["outputs"].append(entry)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    return summary_path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="RFD4-Proteina generation wrapper: condition-spec YAML in, validated structures out")
    ap.add_argument("--spec", required=True,
                    help="condition spec YAML/JSON, relative to container cwd /workspace")
    ap.add_argument("--out-dir", required=True, help="output directory")
    ap.add_argument("--num-designs", type=int, default=2, help="number of designs")
    ap.add_argument("--seed", type=int, default=0, help="random seed; fixes reproducibility")
    ap.add_argument("--preset", default="quickstart",
                    help="inference preset (quickstart skips scoring and compile warm-up)")
    ap.add_argument("--timeout-min", type=float, default=120.0,
                    help="generation timeout in minutes; hard error on exceed")
    ap.add_argument("--allow-cpu", action="store_true",
                    help="allow running without a GPU (diagnostics only)")
    ap.add_argument("--extra", action="append", default=[],
                    help="extra rfd4-generate Hydra override, repeatable")
    args = ap.parse_args()

    _assert_gpu(args.allow_cpu)

    spec = Path(args.spec)
    if not spec.is_file():
        raise SystemExit(f"spec file not found: {spec}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    args.resolved_spec = _resolve_spec(spec, out_dir)
    overrides = _build_overrides(args, out_dir)
    started = time.monotonic()
    _run_generation(overrides, args.timeout_min)
    cifs = _collect_cifs(out_dir)
    summary_path = _write_summary(args, out_dir, cifs,
                                  time.monotonic() - started)
    summary = json.loads(summary_path.read_text())
    n_chain_errors = sum(1 for o in summary["outputs"] if "chains_error" in o)
    print(json.dumps({"ok": n_chain_errors == 0,
                      "n_structures": len(cifs),
                      "n_chain_errors": n_chain_errors,
                      "summary": str(summary_path)}, ensure_ascii=False))
    if n_chain_errors:
        raise SystemExit(
            f"{n_chain_errors} artifacts failed chain parsing; see summary.json")


if __name__ == "__main__":
    main()
