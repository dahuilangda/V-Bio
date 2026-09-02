#!/usr/bin/env python3
"""Precompute frozen-trunk affinity features for cache-based head training.

For each index_csv row x {msa_on, msa_off}, run the frozen trunk ONCE and store
the slices the ProtenixAffinityHead actually reads: s_inputs/s (full, fp16),
z ligand rows+cols (fp16 — the head's triangle attention only touches the
lt_u x rt_u interface grid), crystal coords, full expected_dist (fp16), token
maps, h_pl. Storage ~6 MB/variant -> ~130 GB for 10.9k x 2.

Resumable (skips existing npz). Sharded: SHARD/N_SHARDS by row index.
Usage (inside protenix runtime):
  python precompute_feats.py --index_csv ... --out /data/affinity_training/pxm_feats \
    --shard 0 --n_shards 1
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/workspace/vbio/capabilities/protenix2dock")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index_csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--n_shards", type=int, default=1)
    ap.add_argument("--model_name", default="protenix-v2")
    ap.add_argument("--checkpoint_dir", default="/workspace/model")
    ap.add_argument("--msa_server_url", default="http://172.17.3.200:8080")
    ap.add_argument("--msa_cache_dir", default="/data/boltz_msa_cache")
    ap.add_argument("--max_seq_len", type=int, default=700)
    args = ap.parse_args()

    import numpy as np
    import torch
    from runner.inference import InferenceRunner
    from core.runner import build_configs
    import train_affinity as T

    device = torch.device("cuda")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / f"_work_shard{args.shard}"
    work_dir.mkdir(parents=True, exist_ok=True)

    boot = [{"name": "pre", "sequences": [
        {"proteinChain": {"sequence": "MKVLAAALLASWQVQGTQASWQ", "count": 1}},
        {"ligand": {"ligand": "CCD_GOL", "count": 1}},
    ], "modelSeeds": [42]}]
    (work_dir / "trunk_input.json").write_text(json.dumps(boot))
    configs = build_configs(
        input_json_path=work_dir / "trunk_input.json",
        output_dir=work_dir / "trunk_out",
        model_name=args.model_name,
        checkpoint_dir=Path(args.checkpoint_dir),
        seeds=[42], n_step=1, n_sample=1, sigma_max=160.0, guidance_enable=False,
    )
    runner = InferenceRunner(configs)
    from protenix.data.inference.infer_dataloader import InferenceDataset
    datasets = {}

    def dataset_for(use_msa: bool):
        if use_msa not in datasets:
            cfg = copy.deepcopy(configs)
            cfg.use_msa = use_msa
            datasets[use_msa] = InferenceDataset(cfg)
        return datasets[use_msa]

    trunk = T._FrozenTrunk(runner.model, dataset_for(False))
    targs = argparse.Namespace(**vars(args))
    targs.seed = 42
    targs.msa_prob = 1.0  # _sample_job gates MSA resolution on this

    rows = list(csv.DictReader(open(args.index_csv)))
    rows = rows[args.shard::args.n_shards]
    done = fail = 0
    t0 = time.time()
    for row in rows:
        try:
            tokens = int(row.get("protein_tokens") or 0)
            if tokens and tokens + 32 > args.max_seq_len:
                continue
            for use_msa in (False, True):
                key = f"{row['name']}_msa{int(use_msa)}.npz"
                path = out_dir / key
                if path.exists():
                    continue
                job, chains, ligand_ref, structured = T._sample_job(
                    row, targs, work_dir, use_msa=use_msa)
                feats, s_inputs, s, z, expected_dist, h_pl = trunk.representations(job, device)
                atom2tok = feats["atom_to_token_idx"].to("cpu").numpy()
                is_lig = feats["is_ligand"].to("cpu").numpy().astype(bool)
                lt = np.unique(atom2tok[is_lig].astype(np.int64))
                zc = z.to(torch.float32).cpu().numpy()
                np.savez_compressed(
                    path,
                    s_inputs=s_inputs.to(torch.float32).cpu().numpy().astype(np.float16),
                    s=s.to(torch.float32).cpu().numpy().astype(np.float16),
                    z_lig_rows=zc[lt].astype(np.float16),
                    z_lig_cols=zc[:, lt].astype(np.float16),
                    lig_tokens=lt.astype(np.int32),
                    n_token=np.int32(zc.shape[0]),
                    coords=T._crystal_coords(job, chains, ligand_ref).numpy().astype(np.float32),
                    expected_dist=(expected_dist.to(torch.float32).cpu().numpy()
                                   if expected_dist is not None
                                   else np.zeros(0, np.float16)).astype(np.float16),
                    atom_to_token_idx=atom2tok.astype(np.int32),
                    is_ligand=is_lig,
                    h_pl=np.float32(h_pl if h_pl is not None else 0.0),
                )
                done += 1
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
        except Exception as exc:  # noqa: BLE001
            fail += 1
            if fail <= 20:
                print(f"[fail] {row.get('name')}: {exc}", flush=True)
        if (done + fail) % 20 == 0 and (done + fail) > 0:
            rate = (time.time() - t0) / max(1, done + fail)
            print(f"[{time.strftime('%H:%M:%S')}] saved={done} fail={fail} "
                  f"({rate:.1f}s/row)", flush=True)
    print(f"[done] shard {args.shard}: saved={done} fail={fail}", flush=True)


if __name__ == "__main__":
    main()
