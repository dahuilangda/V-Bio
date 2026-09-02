#!/usr/bin/env python3
"""Zero-shot OpenBind EV-A71 evaluation for the protenix affinity head.

Mirrors train_affinity.py's val loop exactly (frozen trunk representations +
crystal coords + head readout) so the number is directly comparable to the
trainer's [val] gate. Reference bars on this set:
  - MW baseline:        Spearman +0.469
  - Nesso-1 zero-shot:  Spearman -0.453
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, "/workspace/vbio/capabilities/protenix2dock")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index_csv", required=True)
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--work_dir", required=True)
    ap.add_argument("--model_name", default="protenix-v2")
    ap.add_argument("--checkpoint_dir", default="/workspace/model")
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--msa_server_url", default="http://172.17.3.200:8080")
    args = ap.parse_args()

    import torch
    from protenix.model.modules.affinity import ProtenixAffinityHead
    from runner.inference import InferenceRunner
    from core.runner import build_configs

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    work_dir = Path(args.work_dir).expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    # ---- trunk (identical boot to train_affinity.train) ----
    boot = [{"name": "eval", "sequences": [
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
    cfg = copy.deepcopy(configs)
    cfg.use_msa = False
    dataset = InferenceDataset(cfg)

    import train_affinity as T

    trunk = T._FrozenTrunk(runner.model, dataset)

    # ---- head from checkpoint ----
    blob = torch.load(args.ckpt, map_location=device, weights_only=False)
    head_cfg = blob["config"]
    head = ProtenixAffinityHead(**head_cfg).to(device)
    head.load_state_dict(blob["state_dict"])
    head.eval()
    print(f"[eval] ckpt epoch={blob.get('epoch')} step={blob.get('global_step')}", flush=True)

    rows = list(csv.DictReader(open(args.index_csv)))
    if args.limit:
        rows = rows[: args.limit]
    preds, labels, names = [], [], []
    errors = 0
    for i, row in enumerate(rows):
        try:
            row = dict(row)
            row.setdefault("protein_path", "")
            row.setdefault("sequence", "")
            chains = T.parse_protein_chains(Path(row["protein_path"]))
            ligand_ref = Path(row["ligand_path"])
            job = T.build_input_json(chains=chains, ligand_sdf=ligand_ref,
                                     sample_name="eval", msa_paths={}, seeds=[42])
            feats, s_inputs, s, z, expected_dist, _hpl = trunk.representations(job, device)
            coords = T._crystal_coords(job, chains, ligand_ref).to(device)
            with torch.no_grad():
                entry = T._grad_entry(head, s_inputs, s, z, coords, feats, device,
                                      expected_dist=expected_dist)
            preds.append(entry["affinity_pred_value_t"].item())
            labels.append(float(row["pic50"]))
            names.append(row["name"])
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if errors <= 10:
                print(f"[skip] {row.get('name')}: {exc}", flush=True)
        if (i + 1) % 50 == 0:
            print(f"[eval] {i+1}/{len(rows)} preds={len(preds)} errors={errors}", flush=True)

    print(f"[eval] done: n={len(preds)} errors={errors}", flush=True)
    if len(preds) >= 8:
        from scipy.stats import spearmanr, pearsonr
        rho, pv = spearmanr(preds, labels)
        r, _ = pearsonr(preds, labels)
        print(f"[RESULT] OpenBind EV-A71 zero-shot: n={len(preds)} "
              f"Spearman={rho:+.3f} (p={pv:.1e}) Pearson={r:+.3f}", flush=True)
        print("[RESULT] bars: MW baseline +0.469 | nesso-1 zero-shot -0.453", flush=True)
        with open(work_dir / "openbind_eval.json", "w") as f:
            json.dump({"n": len(preds), "spearman": rho, "pearson": r,
                       "errors": errors, "ckpt": args.ckpt,
                       "names": names, "preds": preds, "labels": labels}, f, indent=2)


if __name__ == "__main__":
    main()
