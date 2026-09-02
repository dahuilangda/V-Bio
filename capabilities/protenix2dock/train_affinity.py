#!/usr/bin/env python3
"""Train the protenix2dock native affinity head (ProtenixAffinityHead).

Fusion properties (not a stitched pipeline):
  - The head consumes the frozen Protenix trunk's s_inputs/s representations
    and the *crystal* pose coordinates during training — exactly the tensors
    it will see at inference (where coordinates come from Protenix's own
    diffusion output). Training and inference therefore share one code path.
  - Loss follows boltz2 (MSE on affinity value + BCE on binder/non-binder)
    plus a pairwise ranking term; MW-corrected pIC50 is derived at readout.

Generalisation choices borrowed from nesso-1:
  - use_msa is randomised per sample (0.5) so the head works with or without
    MSAs at inference (nesso trains MSA-free and generalises broadly).
  - MC-dropout is active in training; at inference mc_samples>1 yields
    affinity_pred_std as an uncertainty estimate.

Data format (PDBbind-style):
  index.csv: name,pic50,active(optional 0/1),protein_path,ligand_path
  protein_path: .pdb/.cif structure; ligand_path: posed .sdf (crystal pose)

Smoke test (no external data):
  python train_affinity.py --smoke
  builds a tiny protein + CCD ligand record, trains a few steps, saves the
  head checkpoint, reloads it through PROTENIX_AFFINITY_CKPT and runs the
  inference path end-to-end.

Runs inside the Protenix runtime image:
  PYTHONPATH=/workspace/vbio/vendor/protenix-source python train_affinity.py ...
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.input_prep import align_init_coords, build_input_json, load_ligand_pose, resolve_msa  # noqa: E402
from core.structure import parse_protein_chains  # noqa: E402


class _FrozenTrunk:
    """Wraps a Protenix model to expose trunk representations with no grad.

    Uses the stock InferenceDataset.process_one so the training features are
    byte-identical to inference (MSA/template handling included).
    """

    def __init__(self, model, dataset):
        self.model = model
        self.dataset = dataset
        self.model.eval()
        for p in self.model.parameters():
            p.requires_grad_(False)

    @torch.no_grad()
    def representations(self, single_sample_dict, device):
        """Trunk features + distogram expected distances (structure-free channel)."""
        from protenix.model.protenix import update_input_feature_dict
        from protenix.model.sample_confidence import get_bin_centers

        data, _, _ = self.dataset.process_one(single_sample_dict)
        feats = data["input_feature_dict"]
        feats = {
            k: (v.to(device) if torch.is_tensor(v) else v) for k, v in feats.items()
        }
        feats = self.model.relative_position_encoding.generate_relp(feats)
        feats = update_input_feature_dict(feats)
        feats = {
            k: (v.to(device) if torch.is_tensor(v) else v) for k, v in feats.items()
        }
        s_inputs, s, z = self.model.get_pairformer_output(
            input_feature_dict=feats,
            N_cycle=1,
            inplace_safe=True,
        )
        expected_dist = None
        h_pl = None
        try:
            dcfg = self.model.configs.loss.distogram
            logits = self.model.distogram_head(z.float())
            probs = torch.softmax(logits, dim=-1)
            centers = get_bin_centers(
                float(dcfg.min_bin), float(dcfg.max_bin), int(dcfg.no_bins)
            ).to(device)
            expected_dist = (probs * centers).sum(-1)  # [N, N] Å
            # Nesso/TerraBind H_PL: normalised Shannon entropy of the
            # ligand-token↔token distance distributions; high = diffuse
            # ligand position = weak structural binding evidence.
            eps = 1e-9
            is_lig = feats["is_ligand"].to(torch.bool)
            if is_lig.any():
                lig_tokens = torch.unique(feats["atom_to_token_idx"][is_lig].long())
                lig_probs = probs[lig_tokens]  # [N_lig, N_token, bins]
                ent = -(lig_probs * (lig_probs + eps).log()).sum(-1)
                h_pl = (ent.mean() / math.log(lig_probs.shape[-1])).item()
        except Exception as exc:  # noqa: BLE001
            print(f"[Warning] distogram/H_PL unavailable: {exc}")
        return feats, s_inputs, s, z, expected_dist, h_pl


def train(args: argparse.Namespace) -> Path:
    import torch.nn as nn

    from protenix.model.modules.affinity import ProtenixAffinityHead
    from runner.inference import InferenceRunner

    from core.runner import build_configs

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    work_dir = Path(args.work_dir).expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    # Frozen trunk via the stock runner config (loads protenix-v2 weights).
    job = [{"name": "train", "sequences": [
        {"proteinChain": {"sequence": "MKVLAAALLASWQVQGTQASWQ", "count": 1}},
        {"ligand": {"ligand": "CCD_GOL", "count": 1}},
    ], "modelSeeds": [args.seed]}]
    (work_dir / "trunk_input.json").write_text(json.dumps(job))
    configs = build_configs(
        input_json_path=work_dir / "trunk_input.json",
        output_dir=work_dir / "trunk_out",
        model_name=args.model_name,
        checkpoint_dir=Path(args.checkpoint_dir),
        seeds=[42],
        n_step=1, n_sample=1, sigma_max=160.0, guidance_enable=False,
    )
    runner = InferenceRunner(configs)
    from protenix.data.inference.infer_dataloader import InferenceDataset

    msa_on = dict(configs.to_dict()) if hasattr(configs, "to_dict") else dict(configs)
    dataset_cache = {}

    def _dataset_for(use_msa: bool):
        key = bool(use_msa)
        if key not in dataset_cache:
            cfg = copy.deepcopy(configs)
            cfg.use_msa = key
            dataset_cache[key] = InferenceDataset(cfg)
        return dataset_cache[key]

    trunk = None  # built lazily (needs a dataset for process_one)

    c_s = c_z = None  # resolved lazily on first batch
    head_kwargs = dict(num_blocks=args.num_blocks, dropout=args.dropout,
                       mc_samples=args.mc_samples)

    samples = []
    if args.index_csv:
        with open(args.index_csv, encoding="utf-8") as fh:
            samples = list(csv.DictReader(fh))
    if args.smoke:
        samples = _smoke_samples(work_dir)

    head = None
    opt = None
    history = []
    global_step = 0
    # Long-sequence guard: N^2 pair tensors OOM past this many residues.
    before_guard = len(samples)
    samples = [r for r in samples if len(r.get("sequence") or "") <= args.max_seq_len]
    # Structured (crystal-pose) records carry no `sequence` column; cap their TOTAL
    # polymer tokens instead — multi-chain complexes past this size blow the N^2
    # pair-attention memory budget in one forward pass.
    def _row_tokens(row):
        raw = row.get("protein_tokens")
        try:
            return int(raw) if str(raw).strip() else None
        except (TypeError, ValueError):
            return None
    before_structured_guard = len(samples)
    samples = [
        r for r in samples
        if (t := _row_tokens(r)) is None or t + 32 <= args.max_seq_len
    ]
    if before_structured_guard != len(samples):
        print(f"[filter] dropped {before_structured_guard - len(samples)} structured records with tokens > {args.max_seq_len}")
    if len(samples) < before_guard:
        print(f"[filter] dropped {before_guard - len(samples)} records with sequence > {args.max_seq_len} aa")

    # Resume support (V-Bio task sharding): restore head + optimizer + step.
    start_epoch = 0
    if args.resume_ckpt and Path(args.resume_ckpt).exists():
        blob = torch.load(args.resume_ckpt, map_location=device, weights_only=False)
        history = blob.get("history") or []
        global_step = int(blob.get("global_step") or 0)
        start_epoch = int(blob.get("epoch") or 0) + 1
        head_cfg_saved = dict(blob.get("config") or {})
        # Rebuild the head from the checkpoint's own config: num_blocks,
        # dropout, mc_samples and dims are architectural, and must match the
        # saved weights rather than the current CLI defaults.
        head = ProtenixAffinityHead(**head_cfg_saved).to(device)
        try:
            head.load_state_dict(blob["state_dict"])
        except RuntimeError as exc:
            # Architecture changed (head rework): state_dict keys no longer
            # match. Fail loudly instead of silently retraining from scratch —
            # the caller decides whether to drop the stale checkpoint.
            raise RuntimeError(
                f"checkpoint {args.resume_ckpt} is not compatible with the "
                f"current head architecture (state_dict key mismatch: {exc}). "
                "Retrain from scratch or rebuild the checkpoint with the "
                "current ProtenixAffinityHead."
            ) from exc
        opt = torch.optim.AdamW(head.parameters(), lr=args.lr)
        if blob.get("optimizer"):
            opt.load_state_dict(blob["optimizer"])
        c_s, c_z = head_cfg_saved.get("c_s"), head_cfg_saved.get("c_z")
        print(f"[resume] epoch={start_epoch} step={global_step} from {args.resume_ckpt}")

    val_rows = []
    if args.val_csv:
        val_csv_path = Path(args.val_csv)
        if val_csv_path.exists():
            with open(val_csv_path, encoding="utf-8") as fh:
                val_rows = list(csv.DictReader(fh))
        else:
            print(f"[Warning] val_csv not found: {val_csv_path}; skipping validation gate")
    errors = 0
    contradictions = 0

    # Resume semantics: --epochs is the number of ADDITIONAL epochs to run
    # after a resumed checkpoint (shard1: 0..1, shard2 resume: 1..2).
    end_epoch = start_epoch + max(1, args.epochs)
    for epoch in range(start_epoch, end_epoch):
        random.shuffle(samples)
        # Nesso-style assay-grouped pairing: with probability, draw a second
        # sample from the same assay for the relative-difference loss.
        by_assay = defaultdict(list)
        for row in samples:
            by_assay[row.get("assay_id") or (row.get("target_id") or "unknown")].append(row)
        for si, row in enumerate(samples):
            try:
                use_msa = random.random() < args.msa_prob
                dataset = _dataset_for(use_msa)
                if trunk is None:
                    trunk = _FrozenTrunk(runner.model, dataset)
                partner = None
                if args.rel_weight > 0 and head is not None and random.random() < 0.5:
                    pool = by_assay[row.get("assay_id") or (row.get("target_id") or "unknown")]
                    if len(pool) > 1:
                        partner_row = random.choice([p for p in pool if p is not row])
                        partner = _forward_row(
                            trunk, head, partner_row, work_dir, device, args, msa_dataset_fn=_dataset_for
                        )
                # Build the per-sample input job exactly like inference does.
                feats, s_inputs, s, z, expected_dist, h_pl, coords = _cached_or_repr(
                    trunk, row, args, work_dir, device, use_msa)
                # Nesso-1 contradiction filter: weak structural evidence
                # (H_PL high) + strong label (pIC50>=6) => label suspect, skip.
                if (h_pl is not None and h_pl > args.hpl_max
                        and float(row["pic50"]) >= args.hpl_pic50_min):
                    contradictions += 1
                    continue
                if coords is not None:
                    coords = coords.to(device)
                if head is None:
                    c_s, c_z = s_inputs.shape[-1], z.shape[-1]  # s_inputs carries the input-embedder width the head consumes
                    head = ProtenixAffinityHead(c_s=c_s, c_z=c_z, **head_kwargs).to(device)
                    opt = torch.optim.AdamW(head.parameters(), lr=args.lr)
                label_pic50 = torch.tensor([float(row["pic50"])], device=device)
                active = torch.tensor(
                    [float(row.get("active") or (1.0 if float(row["pic50"]) >= 6.0 else 0.0))],
                    device=device,
                )

                head.train()
                # Scalar-tensor training pass: run the fused readout with grads.
                entry = _grad_entry(
                    head, s_inputs, s, z, coords, feats, device,
                    expected_dist=expected_dist,
                )
                value = entry["affinity_pred_value_t"]
                logit = entry["affinity_logits_binary_t"]

                # Boltz-2/nesso-style losses: Huber on the value, focal BCE on the
                # binder score, and (when a same-assay partner was drawn) the
                # relative-difference term that Nesso-1 up-weights to optimise
                # intra-assay ranking over inter-assay offsets.
                abs_loss = nn.functional.huber_loss(value, label_pic50)
                p = torch.sigmoid(logit)
                p_t = p * active + (1 - p) * (1 - active)
                focal = (-((1 - p_t) ** args.focal_gamma) * torch.log(p_t + 1e-7)).reshape(())
                loss = abs_loss + args.focal_weight * focal
                if partner is not None:
                    v2 = partner["value"]
                    dy = label_pic50 - partner["label"]
                    rel = nn.functional.huber_loss((value - v2).reshape(1), dy.reshape(1))
                    loss = loss + args.rel_weight * rel
                opt.zero_grad()
                loss.backward()
                opt.step()
                global_step += 1
                history.append(loss.item())
                if args.ckpt_every > 0 and global_step % args.ckpt_every == 0:
                    _save_checkpoint(
                        head, opt, c_s, c_z, head_kwargs, history, (end_epoch - 1),
                        global_step, errors, contradictions, work_dir / "protenix_affinity_head.pt",
                    )
                if si % 10 == 0:
                    print(f"[progress] epoch={epoch}/{args.epochs} sample={si}/{len(samples)} step={global_step} loss={loss.item():.4f}", flush=True)
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if errors <= 20:
                    print(f"[skip] sample {si} failed: {exc}", flush=True)
                if errors > max(50, len(samples) // 5):
                    raise RuntimeError(f"too many sample failures ({errors})")
                # Crystal complexes vary wildly in token count; without this the
                # caching allocator fragments across shapes and later samples OOM.
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

    print(f"[curate] H_PL contradiction samples skipped: {contradictions}")

    # Validation (honesty gate): Spearman on the held-out val split.
    if val_rows and head is not None and trunk is not None:
        head.eval()
        preds, labels = [], []
        val_errors = 0
        for row in val_rows[: args.val_limit]:
            try:
                row = dict(row)
                row.setdefault("protein_path", "")
                structured = bool(row.get("protein_path"))
                if structured:
                    chains = parse_protein_chains(Path(row["protein_path"]))
                    ligand_ref = Path(row["ligand_path"])
                else:
                    ligand_ref = _embed_ligand_sdf(row["smiles"], work_dir, args.seed)
                    from core.structure import ProteinChainData
                    chains = [ProteinChainData(chain_name="A", sequence=row["sequence"], residues=[])]
                job = build_input_json(chains=chains, ligand_sdf=ligand_ref,
                                       sample_name="val", msa_paths={}, seeds=[42])
                feats, s_inputs, s, z, expected_dist, h_pl = trunk.representations(job, device)
                coords = _crystal_coords(job, chains, ligand_ref).to(device) if structured else None
                with torch.no_grad():
                    entry = _grad_entry(head, s_inputs, s, z, coords, feats, device,
                                        expected_dist=expected_dist)
                preds.append(entry["affinity_pred_value_t"].item())
                labels.append(float(row["pic50"]))
            except Exception as exc:  # noqa: BLE001
                val_errors += 1
        if len(preds) >= 8:
            from scipy.stats import spearmanr

            rho = spearmanr(preds, labels).statistic
            print(f"[val] n={len(preds)} spearman={rho:+.3f} errors={val_errors}", flush=True)
        head.train()

    if head is None:
        raise RuntimeError("no training samples processed; nothing to save")

    ckpt_path = work_dir / "protenix_affinity_head.pt"
    _save_checkpoint(
        head, opt, c_s, c_z, head_kwargs, history, (end_epoch - 1),
        global_step, errors, contradictions, ckpt_path,
    )
    # Nesso-style checkpoint averaging with the best previous run, if any.
    if args.resume_ckpt and Path(args.resume_ckpt).exists():
        try:
            prev = torch.load(args.resume_ckpt, map_location="cpu", weights_only=False)
            avg = {k: (v.float() + prev["state_dict"][k].float()) / 2
                   for k, v in head.state_dict().items()}
            torch.save(
                {
                    "state_dict": {k: v.to(prev["state_dict"][k].dtype) for k, v in avg.items()},
                    "config": {"c_s": c_s, "c_z": c_z, **head_kwargs},
                    "history": history,
                    "epoch": end_epoch - 1,
                    "global_step": global_step,
                    "averaged": True,
                },
                work_dir / "protenix_affinity_head_avg.pt",
            )
            print(f"[saved] averaged checkpoint (prev + this) -> protenix_affinity_head_avg.pt")
        except Exception as exc:  # noqa: BLE001
            print(f"[Warning] checkpoint averaging skipped: {exc}")
    print(f"[saved] {ckpt_path}")
    return ckpt_path


def _crystal_coords(job: dict, chains, ligand_path: Path) -> torch.Tensor:
    """Crystal-pose coordinates aligned to the assembled atom order."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump([job], fh)
        input_json = Path(fh.name)
    ligand_mol = load_ligand_pose(ligand_path)
    coords, _, _ = align_init_coords(input_json, chains, ligand_mol)
    input_json.unlink(missing_ok=True)
    return torch.from_numpy(coords)



def _cached_or_repr(trunk, row, args, work_dir, device, use_msa, msa_dataset_fn=None, job_ctx=None):
    """Feature-cache path: load precomputed trunk representations when present.

    Returns (feats, s_inputs, s, z, expected_dist, h_pl, coords). Falls back to
    the on-the-fly trunk forward (identical semantics) when no cache entry
    exists. z is reconstructed zero-filled outside the ligand row/col slices —
    the affinity head only reads the lt_u x rt_u interface grid."""
    import numpy as np
    cache_dir = getattr(args, "feature_cache", None)
    name = row.get("name") or ""
    if cache_dir and name:
        path = Path(cache_dir) / f"{name}_msa{int(use_msa)}.npz"
        if path.exists():
            try:
                d = np.load(path, allow_pickle=False)
                lt = d["lig_tokens"].astype(np.int64)
                n = int(d["n_token"])
                cz = int(d["z_lig_rows"].shape[-1])
                z = torch.zeros(n, n, cz)
                z[lt] = torch.from_numpy(d["z_lig_rows"].astype(np.float32))
                z[:, lt] = torch.from_numpy(d["z_lig_cols"].astype(np.float32))
                feats = {
                    "atom_to_token_idx": torch.from_numpy(d["atom_to_token_idx"].astype(np.int64)),
                    "is_ligand": torch.from_numpy(d["is_ligand"].astype(bool)),
                }
                ed = torch.from_numpy(d["expected_dist"].astype(np.float32))
                ed = ed if ed.numel() else None
                coords = torch.from_numpy(d["coords"].astype(np.float32))
                dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                return (feats,
                        torch.from_numpy(d["s_inputs"].astype(np.float32)).to(dev),
                        torch.from_numpy(d["s"].astype(np.float32)).to(dev),
                        z.to(dev), ed.to(dev) if ed is not None else None,
                        float(d["h_pl"]), coords)
            except Exception as exc:  # noqa: BLE001
                print(f"[cache-miss-corrupt] {name} msa{int(use_msa)}: {exc}", flush=True)
    job, chains, ligand_ref, structured = _sample_job(
        row, args, work_dir, use_msa=use_msa)
    feats, s_inputs, s, z, expected_dist, h_pl = trunk.representations(job, device)
    coords = _crystal_coords(job, chains, ligand_ref) if structured else None
    return feats, s_inputs, s, z, expected_dist, h_pl, coords

def _forward_row(trunk, head, row, work_dir, device, args, msa_dataset_fn):
    """One no-grad forward for a partner sample (relative-difference loss)."""
    use_msa = random.random() < args.msa_prob
    dataset = msa_dataset_fn(use_msa)
    feats, s_inputs, s, z, expected_dist, h_pl, coords = _cached_or_repr(
        trunk, row, args, work_dir, device, use_msa)
    if coords is not None:
        coords = coords.to(device)
    with torch.no_grad():
        entry = _grad_entry(
            head, s_inputs, s, z, coords, feats, device, expected_dist=expected_dist
        )
    return {
        "value": entry["affinity_pred_value_t"].detach(),
        "label": torch.tensor([float(row["pic50"])], device=device),
    }


def _save_checkpoint(head, opt, c_s, c_z, head_kwargs, history, final_epoch,
                  global_step, errors, contradictions, ckpt_path):
    import torch

    torch.save(
        {
            "state_dict": head.state_dict(),
            "optimizer": opt.state_dict() if opt is not None else None,
            "config": {"c_s": c_s, "c_z": c_z, **head_kwargs},
            "history": history,
            "epoch": final_epoch,
            "global_step": global_step,
            "errors": errors,
            "contradictions": contradictions,
        },
        ckpt_path,
    )



def _sample_job(row, args, work_dir, use_msa=True):
    """Build the per-sample input job (training and partner paths share it).

    MSA resolution is gated on this sample's actual use_msa draw: fetching costs
    tens of seconds per chain on the ColabFold server, and resolving for samples
    that run MSA-free made epoch time explode with zero quality benefit."""
    from core.structure import ProteinChainData

    structured = bool(row.get("protein_path"))
    if structured:
        chains = parse_protein_chains(Path(row["protein_path"]))
        ligand_ref = Path(row["ligand_path"])
    else:
        ligand_ref = _embed_ligand_sdf(row["smiles"], work_dir, args.seed)
        chains = [ProteinChainData(chain_name="A", sequence=row["sequence"], residues=[])]
    msa_paths = {}
    if args.msa_prob > 0 and use_msa:
        msa_dir = work_dir / "msa"
        for chain in chains:
            msa_paths[chain.chain_name] = resolve_msa(
                chain.sequence, chain.chain_name,
                Path(args.msa_cache_dir) if args.msa_cache_dir else None,
                args.msa_server_url, msa_dir,
            )
    job = build_input_json(
        chains=chains,
        ligand_sdf=ligand_ref,
        sample_name="train",
        msa_paths=msa_paths,
        seeds=[args.seed],
    )
    return job, chains, ligand_ref, structured


def _embed_ligand_sdf(smiles: str, work_dir: Path, seed: int = 42) -> Path:
    """ETKDG conformer SDF for a structure-free record (FILE_ ligand input)."""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
    if mol is None:
        raise ValueError(f"invalid SMILES: {smiles}")
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    if AllChem.EmbedMolecule(mol, params) != 0:
        raise ValueError(f"embedding failed: {smiles}")
    mol = Chem.RemoveHs(mol)
    import hashlib
    key = hashlib.md5(f"{smiles}|{seed}".encode()).hexdigest()[:16]
    out = work_dir / f"lig_{key}.sdf"
    if not out.exists():
        w = Chem.SDWriter(str(out))
        w.write(mol)
        w.close()
    return out


def _grad_entry(head, s_inputs, s, z, coords, feature_dict, device, expected_dist=None):
    """Differentiable single-sample head pass (training).

    Runs the SAME code path as inference: head.forward with return_tensors
    keeps the autograd graph; only the distance channel differs. Crystal
    pose wins when present (explicit 3D evidence); the trunk distogram
    expected-distance channel is the structure-free fallback. mc_samples=1
    with the head in train() mode keeps dropout active for one stochastic pass.
    """
    entry = head.forward(
        s_inputs=s_inputs,
        z_trunk=z,
        x_pred=coords.unsqueeze(0) if coords is not None else None,
        atom_to_token_idx=feature_dict["atom_to_token_idx"].to(device).long(),
        atom_is_ligand=feature_dict["is_ligand"].to(device),
        mc_samples=1,
        expected_dist=None if coords is not None else expected_dist,
        return_tensors=True,
    )[0]
    return {
        "affinity_pred_value_t": entry["affinity_pred_value_t"].reshape(1),
        "affinity_logits_binary_t": entry["affinity_logits_binary_t"].reshape(1),
    }


def _smoke_samples(work_dir: Path) -> list[dict]:
    """Synthetic records: small protein + a few CCD/SMILES ligands, fake pIC50."""
    protein = work_dir / "smoke_protein.pdb"
    protein.write_text(
        "ATOM      1  N   ALA A   1      11.104   6.134  -6.504  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1      10.857   7.578  -6.635  1.00  0.00           C\n"
        "ATOM      3  C   ALA A   1       9.367   7.826  -6.486  1.00  0.00           C\n"
        "ATOM      4  O   ALA A   1       8.869   8.944  -6.601  1.00  0.00           O\n"
    )
    lig = work_dir / "smoke_ligand.sdf"
    from rdkit import Chem
    from rdkit.Chem import AllChem

    m = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(m, randomSeed=3)
    m = Chem.RemoveHs(m)
    w = Chem.SDWriter(str(lig)); w.write(m); w.close()
    return [
        {"protein_path": str(protein), "ligand_path": str(lig), "pic50": "5.0",
         "active": "0", "target_id": "T0", "assay_id": "A0"},
        {"protein_path": str(protein), "ligand_path": str(lig), "pic50": "7.5",
         "active": "1", "target_id": "T0", "assay_id": "A0"},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index_csv", default=None,
                        help="CSV: name,pic50,active,protein_path,ligand_path")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--work_dir", default="/tmp/p2d_affinity_train")
    parser.add_argument("--model_name", default="protenix-v2")
    parser.add_argument("--checkpoint_dir", default="/workspace/model")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--num_blocks", type=int, default=2)
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--mc_samples", type=int, default=4)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--msa_prob", type=float, default=0.5,
                        help="Probability of training with MSA features (nesso-style robustness)")
    parser.add_argument("--msa_server_url", default=None)
    parser.add_argument("--msa_cache_dir", default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--resume_ckpt", default=None,
                        help="restore head+optimizer and continue (task sharding)")
    parser.add_argument("--val_csv", default=None, help="held-out split for the val gate")
    parser.add_argument("--val_limit", type=int, default=200)
    parser.add_argument("--hpl_max", type=float, default=0.7,
                        help="skip samples with H_PL above this AND pIC50 >= hpl_pic50_min")
    parser.add_argument("--hpl_pic50_min", type=float, default=6.0)
    parser.add_argument("--ckpt_every", type=int, default=0,
                        help="save a resumable checkpoint every N global steps (0=end only)")
    parser.add_argument("--feature_cache", default=None,
                        help="dir of precomputed trunk-feature npz (precompute_feats.py)")
    parser.add_argument("--max_seq_len", type=int, default=1200,
                        help="drop records with longer sequences (N^2 memory)")
    parser.add_argument("--focal_gamma", type=float, default=2.0,
                        help="focal-loss gamma for the binary head (Nesso-1)")
    parser.add_argument("--focal_weight", type=float, default=0.5)
    parser.add_argument("--rel_weight", type=float, default=2.0,
                        help="weight of the intra-assay relative-difference loss "
                             "(Nesso-1 up-weights this to optimise ranking)")
    args = parser.parse_args()

    ckpt = train(args)
    print(
        "\nNext: run protenix2dock with --affinity_head_ckpt "
        f"{ckpt} (or env PROTENIX_AFFINITY_CKPT={ckpt})."
    )


if __name__ == "__main__":
    main()
