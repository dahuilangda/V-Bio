#!/usr/bin/env python3
"""Dispatch RFD4-generated designs to V-Bio verification backends.

Reads a generation output directory (binder CIFs), extracts each binder
sequence, and submits co-folding verification tasks through the platform
gateway to mol1 / protenix / alphafold3. The gateway records every task in
the project; scores (ipTM/ipSAE/RMSD/DockQ) are compared once the tasks
finish, using the same evaluator as the FadA campaign.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

AA3_TO_AA1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}
BACKENDS = ("mol1", "protenix")


def binder_from_cif(path: Path) -> str:
    import gemmi

    st = gemmi.read_structure(str(path))
    st.setup_entities()
    st.remove_alternative_conformations()
    polymers = [c for c in st[0] if sum(1 for r in c if r.het_flag != "H") >= 3]
    if not polymers:
        raise ValueError(f"no polymer chain in {path}")
    chain = min(polymers, key=lambda c: sum(1 for r in c if r.het_flag != "H"))
    return "".join(AA3_TO_AA1.get(r.name.strip().upper(), "A")
                   for r in chain if r.het_flag != "H")


def post_multipart(url: str, token: str, fields: dict, yaml_path: Path) -> dict:
    import subprocess

    cmd = ["curl", "-sS", "-X", "POST", url, "-H", f"X-API-Token: {token}"]
    for key, value in fields.items():
        cmd += ["-F", f"{key}={value}"]
    cmd += ["-F", f"yaml_file=@{yaml_path};type=application/x-yaml"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return json.loads(out)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Dispatch RFD4 designs to V-Bio verification backends")
    ap.add_argument("--runs-dir", required=True,
                    help="RFD4 generation output dir (contains inference_samples)")
    ap.add_argument("--target-sequence", required=True)
    ap.add_argument("--gateway", default="http://172.17.3.200:5173/vbio-api")
    ap.add_argument("--token", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--backends", default="mol1,protenix",
                    help="comma-separated verification backends (default: mol1,protenix)")
    ap.add_argument("--top", type=int, default=5, help="designs per backend")
    ap.add_argument("--seed", type=int, default=101)
    args = ap.parse_args()

    cifs = sorted(Path(args.runs_dir).rglob("*_generated.cif"))
    if not cifs:
        raise SystemExit(f"no generated cif under {args.runs_dir}")
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    manifest = {"verifications": []}

    for cif in cifs:
        seq = binder_from_cif(cif)
        label = cif.parent.name
        yaml_path = Path("/tmp") / f"verify_{label}.yaml"
        yaml_path.write_text(
            "version: 1\nsequences:\n"
            "- protein:\n"
            "    id: A\n"
            f"    sequence: {args.target_sequence}\n"
            "- protein:\n"
            "    id: B\n"
            f"    sequence: {seq}\n")
        for backend in backends:
            response = post_multipart(
                f"{args.gateway}/predict", args.token,
                {
                    "project_id": args.project_id,
                    "backend": backend,
                    "workflow": "prediction",
                    "use_msa_server": "true",
                    "msa_mode": "uniref",
                    "seed": str(args.seed),
                    "task_name": f"RFD4 verify {backend} {label} {seq[:16]}",
                }, yaml_path)
            task_id = response.get("task_id")
            if task_id:
                manifest["verifications"].append(
                    {"design": label, "sequence": seq, "backend": backend,
                     "task_id": task_id})
                print(f"{label} {backend} -> {task_id}")
            else:
                print(f"{label} {backend} REJECTED: {json.dumps(response)[:120]}")

    manifest_path = Path(args.runs_dir) / "verification_manifest.json"
    try:
        manifest_path.write_text(json.dumps(manifest, indent=1))
    except OSError:  # runs dir owned by the engine container; fall back to cwd
        manifest_path = Path.cwd() / "verification_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=1))
    print(f"\n{len(manifest['verifications'])} verification tasks; "
          f"manifest: {manifest_path}")


if __name__ == "__main__":
    main()
