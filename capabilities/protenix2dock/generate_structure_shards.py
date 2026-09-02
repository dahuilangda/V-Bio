#!/usr/bin/env python3
"""Generate structure-based affinity training shards with protenix2dock.

For each (smiles, pic50) record of a target, dock the ligand into the target
structure via the protenix2dock runtime (docker), extract the best-ranked
docked pose as an SDF, and emit a train_affinity.py index row
(name,pic50,active,protein_path,ligand_path).

Usage:
    python generate_structure_shards.py \
      --records records.json \
      --protein protein.pdb \
      --center_x .. --center_y .. --center_z .. --size_x .. --size_y .. --size_z .. \
      --out /data/affinity_training/cdk2_struct --gpus 0,1,2,3

records.json: [{"name","smiles","pic50"}, ...]
Outputs:
    <out>/shard_<gpu>.csv     per-worker index rows (incremental)
    <out>/poses/<name>.sdf    docked poses
    <out>/failures.csv        skipped records + reason
    <out>/index.csv           merged final index (after all shards)
    <out>/_manifest.json      run metadata
Resumable: already-present pose files are skipped.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gemmi
from rdkit import Chem

REPO_ROOT = Path(__file__).resolve().parents[2]  # /data/V-Bio
P2D_SCRIPT = "/workspace/vbio/capabilities/protenix2dock/protenix2dock.py"
P2D_IMAGE = os.environ.get("P2D_IMAGE", "vbio-protenix-v2-runtime:2.0.0")
P2D_MODEL_DIR = os.environ.get("P2D_MODEL_DIR", "/data/protenix/model")
P2D_COMMON_CACHE = os.environ.get("P2D_COMMON_CACHE", "/data/protenix/common_cache")
P2D_MODULE_CACHE = os.environ.get("P2D_MODULE_CACHE", "/data/protenix/module_cache")
P2D_TRITON_CACHE = os.environ.get("P2D_TRITON_CACHE", "/data/protenix/triton_cache")
MSA_SERVER = os.environ.get("MSA_SERVER_URL", "http://172.17.3.200:8080")
ACTIVE_CUTOFF_PIC50 = 6.0  # IC50 <= 1 uM counts as active


def canonicalize_smiles(smiles: str) -> str:
    """Canonical SMILES, ignoring trailing CXSMILES annotations (|...|)."""
    text = str(smiles or "").strip().split("|", 1)[0].strip()
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        raise ValueError(f"unparseable SMILES: {smiles!r}")
    return Chem.MolToSmiles(mol)


def run_dock(gpu: int, smiles: str, protein_pdb: Path, pocket: dict, out_dir: Path) -> Path:
    """Dock one ligand; return the best-ranked (sample_0) pose cif path.

    Raises RuntimeError with the container log tail when the dock fails or
    produces no structure, and subprocess.TimeoutExpired past 900 s.
    """
    work = out_dir / "_work"
    run_dir = work / f"run_{os.getpid()}_{int(time.time() * 1000)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(protein_pdb, run_dir / "protein.pdb")
    cmd = [
        "docker", "run", "--rm", "--entrypoint=", "--gpus", f"device={gpu}", "--shm-size", "16g",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--volume", f"{REPO_ROOT}:/workspace/vbio:ro",
        "--volume", f"{run_dir}:/tmp/p2d_gen",
        "--volume", f"{P2D_MODEL_DIR}:/workspace/model:ro",
        "--volume", f"{P2D_COMMON_CACHE}:/cache/common:ro",
        "--volume", f"{P2D_MODULE_CACHE}:/cache/module_cache",
        "--volume", f"{P2D_TRITON_CACHE}:/tmp/triton",
        "--volume", "/dev/shm:/dev/shm",
        "--env", "PYTHONPATH=/workspace/vbio/vendor/protenix-source",
        "--env", "PROTENIX_ROOT_DIR=/cache",
        "--env", "PROTENIX_MODULE_CACHE_DIR=/cache/module_cache",
        "--env", "TRITON_CACHE_DIR=/tmp/triton",
        P2D_IMAGE,
        "/usr/local/micromamba/envs/protenix/bin/python",
        P2D_SCRIPT,
        "--mode", "dock",
        "--protein_file", "/tmp/p2d_gen/protein.pdb",
        "--ligand_smiles", smiles,
        "--center_x", str(pocket["x"]), "--center_y", str(pocket["y"]), "--center_z", str(pocket["z"]),
        "--size_x", str(pocket["sx"]), "--size_y", str(pocket["sy"]), "--size_z", str(pocket["sz"]),
        "--output_dir", "/tmp/p2d_gen/out", "--work_dir", "/tmp/p2d_gen/work",
        "--msa_server_url", MSA_SERVER,
        "--seed", "42", "--checkpoint_dir", "/workspace/model",
    ]
    r = subprocess.run(
        cmd, cwd=str(run_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=900,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"dock container rc={r.returncode}; log tail: {r.stdout[-400:]}")
    cifs = sorted((run_dir / "out").glob("**/*.cif"))
    if not cifs:
        raise RuntimeError(f"dock produced no structure cif under {run_dir / 'out'}")
    return cifs[0]


def extract_complex_pose(cif_path: Path, out_pose_sdf: Path, out_protein_pdb: Path,
                         expected_heavy: int, canonical_smiles: str) -> bool:
    """Split the docked output complex into per-record protein + ligand pose.

    The docked complex cif contains the protein and ligand chains in one
    shared frame (Protenix applies random per-step augmentation, so the
    absolute frame is arbitrary but the protein-ligand relative geometry is
    the docked pose).  Both chains are extracted from one cif, so the
    training record (protein_path + ligand_path) is frame-consistent by
    construction — no post-hoc alignment needed.

    The ligand SDF is rebuilt from the canonical SMILES (correct bond
    orders/connectivity — required by Protenix's featurizer at training
    time) with the docked conformer coordinates transplanted in atom order;
    the element sequence is verified against the cif chain before writing.
    """
    st = gemmi.read_structure(str(cif_path))
    protein_chains = [c for c in st[0] if c.name.upper().startswith("A")]
    ligand_chains = [c for c in st[0] if not c.name.upper().startswith("A")]
    if not protein_chains or not ligand_chains:
        return False
    # protein as PDB
    protein = gemmi.Structure()
    protein.add_model(gemmi.Model("1"))
    for chain in protein_chains:
        protein[0].add_chain(chain.clone())
    protein.write_pdb(str(out_protein_pdb))

    # ligand: template from SMILES (bonds/connectivity) + docked coordinates
    template = Chem.MolFromSmiles(canonical_smiles)
    if template is None or template.GetNumAtoms() != expected_heavy:
        return False
    ligand = ligand_chains[0]
    cif_elements = []
    cif_coords = []
    for res in ligand:
        for atom in res:
            if atom.element.name == "H":
                continue
            cif_elements.append(atom.element.name)
            cif_coords.append((atom.pos.x, atom.pos.y, atom.pos.z))
    template_elements = [a.GetSymbol() for a in template.GetAtoms()]
    if cif_elements != template_elements:
        return False
    mol = Chem.Mol(template)
    conf = Chem.Conformer(mol.GetNumAtoms())
    for idx, xyz in enumerate(cif_coords):
        conf.SetAtomPosition(idx, xyz)
    mol.RemoveAllConformers()
    mol.AddConformer(conf, assignId=True)
    w = Chem.SDWriter(str(out_pose_sdf))
    w.write(mol)
    w.close()
    return True


def worker(gpu: int, tasks: list[dict], protein_pdb: Path, pocket: dict, out_dir: Path,
           shard_csv: Path, failures_csv: Path, log) -> None:
    poses_dir = out_dir / "poses"
    proteins_dir = out_dir / "proteins"
    poses_dir.mkdir(parents=True, exist_ok=True)
    proteins_dir.mkdir(parents=True, exist_ok=True)
    shard_fh = open(shard_csv, "a", newline="", encoding="utf-8")
    fail_fh = open(failures_csv, "a", newline="", encoding="utf-8")
    writer = csv.writer(shard_fh)
    fail_writer = csv.writer(fail_fh)
    for i, task in enumerate(tasks):
        name, smiles, pic50 = task["name"], task["smiles"], task["pic50"]
        pose_path = poses_dir / f"{name}.sdf"
        prot_path = proteins_dir / f"{name}.pdb"
        if pose_path.exists() and prot_path.exists():
            writer.writerow([name, pic50, 1 if pic50 >= ACTIVE_CUTOFF_PIC50 else 0,
                             str(prot_path), str(pose_path)])
            shard_fh.flush()
            continue
        try:
            canon = canonicalize_smiles(smiles)
        except ValueError:
            fail_writer.writerow([name, smiles, pic50, "bad_smiles"])
            fail_fh.flush()
            continue
        expected = Chem.MolFromSmiles(canon).GetNumAtoms()
        t0 = time.time()
        try:
            cif = run_dock(gpu, canon, protein_pdb, pocket, out_dir)
        except subprocess.TimeoutExpired:
            fail_writer.writerow([name, smiles, pic50, "dock_timeout"])
            fail_fh.flush()
            continue
        except RuntimeError as exc:
            fail_writer.writerow([name, smiles, pic50, f"dock_failed: {exc}"[:500]])
            fail_fh.flush()
            continue
        if not extract_complex_pose(cif, pose_path, prot_path, expected, canon):
            fail_writer.writerow([name, smiles, pic50, "pose_mismatch"])
            fail_fh.flush()
            continue
        writer.writerow([name, pic50, 1 if pic50 >= ACTIVE_CUTOFF_PIC50 else 0,
                         str(prot_path), str(pose_path)])
        shard_fh.flush()
        print(f"[gpu{gpu}] {i+1}/{len(tasks)} {name} {time.time()-t0:.0f}s", file=log, flush=True)
    shard_fh.close()
    fail_fh.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", required=True)
    ap.add_argument("--protein", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--gpus", default="0")
    for axis in ("x", "y", "z"):
        ap.add_argument(f"--center_{axis}", type=float, required=True)
        ap.add_argument(f"--size_{axis}", type=float, default=20.0)
    ap.add_argument("--limit", type=int, default=0, help="cap on records per worker (debug)")
    args = ap.parse_args()

    records = json.loads(Path(args.records).read_text())
    if args.limit:
        records = records[: args.limit]
    gpus = [int(g) for g in args.gpus.split(",") if g.strip()]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    protein_pdb = Path(args.protein).resolve()
    pocket = {"x": args.center_x, "y": args.center_y, "z": args.center_z,
              "sx": args.size_x, "sy": args.size_y, "sz": args.size_z}

    # write header lines once
    for gpu in gpus:
        shard = out_dir / f"shard_{gpu}.csv"
        if not shard.exists():
            shard.write_text("name,pic50,active,protein_path,ligand_path\n")
    failures = out_dir / "failures.csv"
    if not failures.exists():
        failures.write_text("name,smiles,pic50,reason\n")

    def _chunks():
        return [records[i:: len(gpus)] for i in range(len(gpus))]

    log_path = out_dir / "generate.log"
    with open(log_path, "a", encoding="utf-8") as log:
        chunks = [c for c in _chunks() if c]
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            futures = [
                pool.submit(worker, gpu, chunk, protein_pdb, pocket, out_dir,
                            out_dir / f"shard_{gpu}.csv", failures, log)
                for gpu, chunk in zip(gpus, chunks)
            ]
            for f in futures:
                f.result()

    # merge shards into the final index
    index_path = out_dir / "index.csv"
    seen = set()
    with open(index_path, "w", newline="", encoding="utf-8") as out_fh:
        w = csv.writer(out_fh)
        w.writerow(["name", "pic50", "active", "protein_path", "ligand_path"])
        for gpu in gpus:
            shard = out_dir / f"shard_{gpu}.csv"
            if not shard.exists():
                continue
            with open(shard, encoding="utf-8") as shard_fh:
                for row in csv.DictReader(shard_fh):
                    if row["name"] in seen:
                        continue
                    seen.add(row["name"])
                    w.writerow([row["name"], row["pic50"], row["active"],
                                row["protein_path"], row["ligand_path"]])
    with open(failures, encoding="utf-8") as fail_fh:
        n_fail = sum(1 for _ in fail_fh) - 1
    manifest = {
        "records": len(records),
        "index_rows": len(seen),
        "failures": max(n_fail, 0),
        "protein": str(protein_pdb),
        "pocket": pocket,
        "gpus": gpus,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (out_dir / "_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"done: {len(seen)} rows -> {index_path}; failures: {n_fail}")


if __name__ == "__main__":
    main()