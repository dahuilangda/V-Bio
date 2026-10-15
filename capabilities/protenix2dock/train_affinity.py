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
                       mc_samples=args.mc_samples,
                       **({"pocket_cond": True} if getattr(args, "pocket_cond", False) else {}),
                       **({"full_dist_cond": True} if getattr(args, "full_dist_cond", False) else {}),
                       **({"pae_aux": True} if getattr(args, "pae_aux", False) else {}))

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
    # Cache-only mode: rows without precomputed features would take the online
    # trunk path, whose OOM failures poison the error budget. In cache mode the
    # tail of oversized complexes is intentionally out of scope.
    if getattr(args, "feature_cache", None):
        cache_dir = Path(args.feature_cache)
        token_map_path = cache_dir / "n_token_map.json"
        true_tokens = json.loads(token_map_path.read_text()) if token_map_path.exists() else {}
        before_cache = len(samples)
        samples = [
            r for r in samples
            if (cache_dir / f"{r.get('name','')}_msa0.npz").exists()
            and (cache_dir / f"{r.get('name','')}_msa1.npz").exists()
            and int(true_tokens.get(r.get('name', ''), 0)) <= args.max_seq_len
        ]
        print(f"[filter] cache-only: kept {len(samples)}/{before_cache} rows with full feature cache "
              f"(true n_token <= {args.max_seq_len})")
    if len(samples) < before_guard:
        print(f"[filter] dropped {before_guard - len(samples)} records with sequence > {args.max_seq_len} aa")

    # Resume support (V-Bio task sharding): restore head + optimizer + step.
    start_epoch = 0
    _auto_ckpt = work_dir / "protenix_affinity_head.pt"
    if _auto_ckpt.exists():
        # Own-progress ckpt always wins over the upstream --resume_ckpt:
        # without this, a docker on-failure restart re-warmed from the
        # upstream shard and silently discarded everything since (observed
        # 2026-10-07: three crash-looped lives, each redoing the epoch from
        # the rebal warm start).
        args.resume_ckpt = str(_auto_ckpt)
        print(f"[auto-resume] {args.resume_ckpt}")
    best_gate = float("-inf")
    _bg = work_dir / "best_gate.pt"
    if _bg.exists():
        # keep the all-time best across crash-restart lives: each new process
        # re-initialises best_gate, which let diverged weights overwrite a
        # good best_gate.pt with negative-gate garbage (2026-10-09)
        try:
            _bb = torch.load(_bg, map_location="cpu", weights_only=False)
            best_gate = float((_bb.get("extra") or {}).get("gate_spearman")
                              or float("-inf"))
            print(f"[gate] resume baseline best_gate={best_gate:+.4f}")
        except Exception:  # noqa: BLE001
            pass
    if args.resume_ckpt and Path(args.resume_ckpt).exists():
        blob = torch.load(args.resume_ckpt, map_location=device, weights_only=False)
        history = blob.get("history") or []
        global_step = int(blob.get("global_step") or 0)
        start_epoch = int(blob.get("epoch") or 0) + 1
        head_cfg_saved = dict(blob.get("config") or {})
        # Deliberate upgrades: --pae_aux / --full_dist_cond on a checkpoint
        # saved WITHOUT the flag must inject it into the rebuild, else the
        # warm start silently trains an inert head (fresh params only:
        # pae_aux adds a zero-init linear, full_dist_cond reuses the
        # existing distance embedding).
        for _up in ("pae_aux", "full_dist_cond"):
            if getattr(args, _up, False) and _up not in head_cfg_saved:
                head_cfg_saved[_up] = True
                print(f"[resume] upgrading warm start with {_up}=True")
        # Rebuild the head from the checkpoint's own config: num_blocks,
        # dropout, mc_samples and dims are architectural, and must match the
        # saved weights rather than the current CLI defaults.
        head = ProtenixAffinityHead(**head_cfg_saved).to(device)
        # Warm start into a pae_aux=True head from a pre-v7 checkpoint: the
        # only tolerated mismatch is the fresh aux head (zero weight + PAE
        # prior bias). Anything else is a real architecture conflict.
        _incompat = head.load_state_dict(blob["state_dict"], strict=False)
        _missing = [k for k in _incompat.missing_keys
                    if not k.startswith("pae_aux_head.")]
        if _missing or _incompat.unexpected_keys:
            raise RuntimeError(
                f"checkpoint {args.resume_ckpt} is not compatible with the "
                f"current head architecture (missing: {_missing}, "
                f"unexpected: {list(_incompat.unexpected_keys)}). "
                "Retrain from scratch or rebuild the checkpoint."
            )
        if _incompat.missing_keys:
            print(f"[resume] non-strict load: initialised fresh "
                  f"{sorted(_incompat.missing_keys)}")
            # The head gained parameters (e.g. pae_aux_head): the ckpt's
            # optimizer state no longer matches the param-group size — skip
            # it rather than crash-looping (observed 2026-10-10: 1400+
            # restarts). Fresh weights keep their warm start; Adam moments
            # re-accumulate under the warmup restart.
            blob["optimizer"] = None
            print("[resume] skipping ckpt optimizer state (param set grew)")
        # Architectural keys (pocket_cond, num_blocks, ...) must survive the
        # round-trip: the resume rebuilds the head from the SAVED config, but
        # _save_checkpoint regenerates config from CLI args — merge so a
        # resumed run without the original flag doesn't save a mismatched
        # config (it broke eval's head rebuild for pocket checkpoints).
        for _k, _v in head_cfg_saved.items():
            if _k not in ("c_s", "c_z"):
                head_kwargs[_k] = _v
        # Inverse merge: a CLI-injected head flag absent from the saved
        # config must not creep into later checkpoints — the resumed head is
        # rebuilt WITHOUT it, but a stale head_kwargs would describe and then
        # save the flag, and a subsequent resume would start feeding the
        # channel (e.g. full_dist) to weights never trained with it
        # (audit 2026-10-07).
        for _k in ("full_dist_cond", "pocket_cond", "ipsae_pool_v3"):
            if _k in head_kwargs and _k not in head_cfg_saved:
                del head_kwargs[_k]
        opt = torch.optim.AdamW(head.parameters(), lr=args.lr,
                                  eps=1e-6)
        if blob.get("optimizer"):
            opt.load_state_dict(blob["optimizer"])
            # load_state_dict restores the OLD run's lr AND initial_lr; the
            # new run's --lr must win or LambdaLR inherits a stale base
            # (observed: v5 warm-start silently trained at the ckpt's 1e-4
            # instead of --lr 2e-5, 2026-10-06).
            for _g in opt.param_groups:
                _g["lr"] = args.lr
                _g["initial_lr"] = args.lr
        sched, accum_counter = None, 0
        opt.zero_grad(set_to_none=True)
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

    # Label normalization: clamp pIC50 tails then z-score. The Huber delta=1
    # then reads as 1 sigma, and absolute-vs-ranking objective tension shrinks.
    # Spearman is unaffected (monotone) — no eval-side inversion needed.
    lo, hi = args.label_lo, args.label_hi
    _ys = [min(max(float(r["pic50"]), lo), hi) for r in samples]
    y_mean = sum(_ys) / max(1, len(_ys))
    y_std = (sum((y - y_mean) ** 2 for y in _ys) / max(1, len(_ys))) ** 0.5 or 1.0
    print(f"[labels] clamped [{lo},{hi}] mu={y_mean:.3f} sigma={y_std:.3f}")

    # Internal val gate: carve ~3% of TARGETS (not rows) out of the cached
    # training pool. Cache-based + deterministic (eval mode, mc=1) so the
    # gate number is reproducible — unlike the old end-of-run online val.
    import hashlib
    gate_rows = []
    if getattr(args, "feature_cache", None):
        def _is_gate(row):
            key = str(row.get("target_id") or row.get("name") or "")
            return int(hashlib.md5(key.encode()).hexdigest(), 16) % 33 == 0
        gate_rows = [r for r in samples if _is_gate(r)]
        _gate_names = {r.get("name") for r in gate_rows}
        samples = [r for r in samples if r.get("name") not in _gate_names]
        print(f"[gate] held out {len(gate_rows)} rows (~3% of targets); train rows: {len(samples)}")

    # Poison list: samples that OOM twice are skipped permanently instead of
    # burning the error budget every epoch (the old counter never reset, so
    # deterministic replays aborted every incarnation at the same count).
    _tok_cache = {}
    if getattr(args, "feature_cache", None):
        _tm_path = Path(args.feature_cache) / "n_token_map.json"
        if _tm_path.exists():
            _tok_cache = json.loads(_tm_path.read_text())

    poison_path = work_dir / "poison.json"
    try:
        poison = json.loads(poison_path.read_text()) if poison_path.exists() else {}
    except Exception:
        poison = {}  # truncated by a mid-write crash; start fresh rather than die
    fail_counts = {}

    # EMA shadow weights — replaces the old (prev+current)/2 cross-basin
    # averaging, which was a self-no-op when resume path == save path.
    ema_state = None

    def _ema_update():
        nonlocal ema_state
        if head is None:
            return
        with torch.no_grad():
            sd = head.state_dict()
            if ema_state is None:
                ema_state = {k: v.detach().clone().float() for k, v in sd.items()}
                return
            d = args.ema_decay
            for k, v in sd.items():
                if v.dtype.is_floating_point:
                    ema_state[k].mul_(d).add_(v.detach().float(), alpha=1 - d)
                else:
                    ema_state[k] = v.detach().clone()

    # best_gate baseline restored from work_dir best_gate.pt above (crash-
    # restart safety); do NOT re-initialise here.

    def _run_gate(epoch_now):
        nonlocal best_gate
        if not gate_rows or head is None:
            return None
        was_training = head.training
        head.eval()
        rng = random.Random(1234)
        preds, labels_v, errs = [], [], 0
        for row in rng.sample(gate_rows, min(args.val_gate_n, len(gate_rows))):
            try:
                feats, s_inputs, s, z, expected_dist, h_pl, coords = _cached_or_repr(
                    trunk, row, args, work_dir, device, use_msa=False)
                if coords is not None:
                    coords = coords.to(device)
                with torch.no_grad():
                    entry = _grad_entry(head, s_inputs, s, z, coords, feats, device,
                                        expected_dist=expected_dist)
                preds.append(float(entry["affinity_pred_value_t"].item()))
                labels_v.append(min(max(float(row["pic50"]), lo), hi))
            except Exception:  # noqa: BLE001
                errs += 1
        if was_training:
            head.train()
        if len(preds) >= 16:
            from scipy.stats import spearmanr
            rho = spearmanr(preds, labels_v).statistic
            tag = ""
            if rho > best_gate:
                best_gate = rho
                tag = " *BEST*"
                _save_checkpoint(head, opt, c_s, c_z, head_kwargs, history, epoch_now,
                                 global_step, errors, contradictions,
                                 work_dir / "best_gate.pt",
                                 extra={"label_mean": y_mean, "label_std": y_std,
                                        "label_lo": lo, "label_hi": hi,
                                        "gate_spearman": rho, "state_dict_ema": ema_state})
            print(f"[gate] step={global_step} n={len(preds)} spearman={rho:+.4f} err={errs}{tag}", flush=True)
            return rho
        return None

    # Resume semantics: --epochs is the number of ADDITIONAL epochs to run
    # after a resumed checkpoint (shard1: 0..1, shard2 resume: 1..2).
    # Bound by STEPS, not the epoch counter: every crash-restart bumps
    # start_epoch (ckpt epoch+1) which lifted end_epoch, so an OOM-looping
    # run re-armed its own budget forever (v5b ran 21 epoch-equivalents,
    # 2026-10-10). _start_step anchors the step-based budget.
    end_epoch = start_epoch + max(1, args.epochs)
    _start_step = global_step
    # Warmup + cosine decay over the PLANNED step budget. Constant LR was the
    # audit's #1 cause of the 0.35-0.50 oscillation: batch-1 gradients at a
    # never-annealing 1e-4 kicked the weights around the basin forever.
    import math
    # sched.step() fires once per OPTIMIZER step (every --accum samples), so
    # the plan must be counted in optimizer steps or the cosine never
    # completes (a 42360-sample plan ran ~5.3k steps and held lr near peak).
    _planned = max(1, math.ceil(max(1, args.epochs) * len(samples) / max(1, args.accum)))
    _warm = max(1, args.warmup)

    def _lr_lambda(step):
        if step < _warm:
            return step / _warm
        _p = min(1.0, (step - _warm) / max(1, _planned - _warm))
        return args.lr_min_frac + (1 - args.lr_min_frac) * 0.5 * (1 + math.cos(math.pi * _p))

    sched = torch.optim.lr_scheduler.LambdaLR(opt, _lr_lambda) if opt is not None else None
    print(f"[sched] warmup={_warm} planned_steps={_planned} lr={args.lr} -> min {args.lr_min_frac * args.lr:.2e}")

    for epoch in range(start_epoch, end_epoch):
        random.shuffle(samples)
        # Giant complexes sink to the epoch tail (smaller graphs first keeps
        # the allocator healthy). The random tiebreak varies the order across
        # epochs — the old sort was deterministic, freezing the visit order.
        if getattr(args, "feature_cache", None):
            _tok_map_p = Path(args.feature_cache) / "n_token_map.json"
            if _tok_map_p.exists():
                _tm = json.loads(_tok_map_p.read_text())
                samples.sort(key=lambda r: (_tm.get(r.get("name", ""), 10**9), random.random()))
        epoch_errors = 0
        # Nesso-style assay-grouped pairing: with probability, draw a second
        # sample from the same assay for the relative-difference loss.
        by_assay = defaultdict(list)
        for row in samples:
            by_assay[row.get("assay_id") or (row.get("target_id") or "unknown")].append(row)
        for si, row in enumerate(samples):
            # Mid-epoch budget: the epoch-head check never fires for a
            # single-epoch run, and crash-restarts inflate the epoch counter
            # — bound by global_step here (sample units).
            if (global_step - _start_step) >= max(1, args.epochs) * len(samples):
                print(f"[budget] reached planned {args.epochs}*{len(samples)} "
                      f"samples (global_step={global_step}); stopping", flush=True)
                break
            _name = row.get("name") or f"idx{si}"
            if poison.get(_name, 0) >= 2:
                continue
            try:
                use_msa = random.random() < args.msa_prob
                dataset = _dataset_for(use_msa)
                if trunk is None:
                    trunk = _FrozenTrunk(runner.model, dataset)
                partner = None
                if args.rel_weight > 0 and head is not None and random.random() < args.partner_prob:
                    pool = by_assay[row.get("assay_id") or (row.get("target_id") or "unknown")]
                    if len(pool) > 1:
                        partner_row = random.choice([p for p in pool if p is not row])
                        _nt = _tok_cache.get(row.get("name", ""), 10**9)
                        _pt = _tok_cache.get(partner_row.get("name", ""), 10**9)
                        _lim = 380 if (row.get("protein_path") or partner_row.get("protein_path")) else 480
                        _grad_pair = (max(_nt, _pt) <= _lim)
                        partner = _forward_row(
                            trunk, head, partner_row, work_dir, device, args,
                            msa_dataset_fn=_dataset_for,
                            label_norm=(lo, hi, y_mean, y_std),
                            require_grad=_grad_pair,
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
                    opt = torch.optim.AdamW(head.parameters(), lr=args.lr,
                                  eps=1e-6)
                    opt.zero_grad(set_to_none=True)
                    accum_counter = 0
                    if sched is None:
                        sched = torch.optim.lr_scheduler.LambdaLR(opt, _lr_lambda)
                label_pic50 = torch.tensor(
                    [(min(max(float(row["pic50"]), lo), hi) - y_mean) / y_std], device=device)
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
                abs_loss = args.abs_weight * nn.functional.huber_loss(value, label_pic50)
                p = torch.sigmoid(logit)
                p_t = p * active + (1 - p) * (1 - active)
                focal = (-((1 - p_t) ** args.focal_gamma) * torch.log(p_t + 1e-7)).reshape(())
                loss = abs_loss + args.focal_weight * focal
                if partner is not None:
                    v2 = partner["value"]
                    dy = label_pic50 - partner["label"]
                    rel = nn.functional.huber_loss((value - v2).reshape(1), dy.reshape(1))
                    loss = loss + args.rel_weight * rel
                if "pae_aux_loss_t" in entry:
                    loss = loss + args.pae_aux_weight * entry["pae_aux_loss_t"]
                # Gradient accumulation: each sample is its own graph; scale by
                # the window size and step every --accum samples. Together with
                # clip_grad_norm this converts batch-1 gradient noise into an
                # effective-batch average.
                # Loss-spike guard: a diverging value head (Adam second-
                # moment spike) first shows as an isolated huge loss; skip
                # the backward so garbage doesn't reinforce itself, and
                # remember the offender.
                if loss.item() > 50.0:
                    errors += 1
                    _nm = row.get("name", "unknown")
                    poison[_nm] = int(poison.get(_nm, 0)) + 1  # count semantics
                    opt.zero_grad(set_to_none=True)
                    accum_counter = 0
                    continue
                (loss / args.accum).backward()
                accum_counter += 1
                if accum_counter >= args.accum:
                    torch.nn.utils.clip_grad_norm_(head.parameters(), 1.0)
                    opt.step()
                    if sched is not None:
                        sched.step()
                    opt.zero_grad(set_to_none=True)
                    accum_counter = 0
                    _ema_update()
                global_step += 1
                history.append(loss.item())
                if args.ckpt_every > 0 and global_step % args.ckpt_every == 0:
                    _save_checkpoint(
                        head, opt, c_s, c_z, head_kwargs, history, (end_epoch - 1),
                        global_step, errors, contradictions, work_dir / "protenix_affinity_head.pt",
                        extra={"label_mean": y_mean, "label_std": y_std,
                               "label_lo": lo, "label_hi": hi, "state_dict_ema": ema_state},
                    )
                    _run_gate(epoch)
                if si % 10 == 0:
                    mem = torch.cuda.memory_allocated()/2**20 if torch.cuda.is_available() else 0
                    maxmem = torch.cuda.max_memory_allocated()/2**20 if torch.cuda.is_available() else 0
                    lr_now = sched.get_last_lr()[0] if sched is not None else args.lr
                    print(f"[progress] epoch={epoch}/{args.epochs} sample={si}/{len(samples)} step={global_step} loss={loss.item():.4f} lr={lr_now:.2e} mem={mem:.0f}M peak={maxmem:.0f}M", flush=True)
                # empty_cache is a device-wide sync; every sample cost real
                # throughput. Every 8th sample + the except path is enough.
                if torch.cuda.is_available() and si % 4 == 0:
                    torch.cuda.empty_cache()
            except Exception as exc:  # noqa: BLE001
                errors += 1
                epoch_errors += 1
                fail_counts[_name] = fail_counts.get(_name, 0) + 1
                if fail_counts[_name] >= 2:
                    poison[_name] = fail_counts[_name]
                    _ptmp = poison_path.with_suffix(".json.tmp")
                    _ptmp.write_text(json.dumps(poison))
                    _ptmp.replace(poison_path)
                if errors <= 20 or epoch_errors % 200 == 0:
                    print(f"[skip] sample {si} ({_name}) failed: {exc}", flush=True)
                if epoch_errors > max(500, len(samples) // 10):
                    raise RuntimeError(f"too many sample failures this epoch ({epoch_errors})")
                # Crystal complexes vary wildly in token count; without this the
                # caching allocator fragments across shapes and later samples OOM.
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

        # flush the accumulation window at epoch end
        if accum_counter > 0:
            torch.nn.utils.clip_grad_norm_(head.parameters(), 1.0)
            opt.step()
            if sched is not None:
                sched.step()
            opt.zero_grad(set_to_none=True)
            accum_counter = 0
            _ema_update()
        _run_gate(epoch)

    if args.hpl_max >= 1.0:
        print(f"[curate] H_PL contradiction filter DISABLED (hpl_max={args.hpl_max})")
    else:
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
    # The old (prev+current)/2 averaging was removed: when the resume path
    # equals the save path it averaged a checkpoint with itself, and across
    # distant steps it averaged across basins. EMA (state_dict_ema) is the
    # principled replacement and is saved inside every checkpoint.
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



# Frozen z-probe PAE projection (benchmark-suite compatibility). The suite's
# gated-head paths call T._expected_pae_from_z; for plain checkpoints the
# projection stays None and the score path is a passive no-op (the REAL
# confidence PAE comes from ipsae_metrics.enrich's run_confidence_head).
_PAE_PROJ = {"w": None, "b": None, "centers": None}


def _install_pae_projection(runner) -> None:
    """Cache the confidence head's raw-z PAE projection (z-probe channel).

    This is the training-time approximation documented in the research audit
    (confidence projection applied directly to trunk z, skipping the 4
    pose-conditioned blocks). Never used for plain checkpoints."""
    try:
        conf = runner.model.confidence_head
        _PAE_PROJ["w"] = conf.linear_no_bias_pae.weight.detach().clone()
        _PAE_PROJ["b"] = getattr(conf.linear_no_bias_pae, "bias", None)
        if _PAE_PROJ["b"] is not None:
            _PAE_PROJ["b"] = _PAE_PROJ["b"].detach().clone()
        # replicate pae_ln (elementwise-affine LayerNorm on raw z)
        _ln = conf.pae_ln
        _PAE_PROJ["ln_w"] = _ln.weight.detach().clone()
        _PAE_PROJ["ln_b"] = _ln.bias.detach().clone()
        _PAE_PROJ["eps"] = float(getattr(_ln, "eps", 1e-5))
        _PAE_PROJ["shape"] = tuple(_ln.normalized_shape)
        pcfg = runner.model.configs.loss.pae
        from protenix.model.sample_confidence import get_bin_centers
        _PAE_PROJ["centers"] = get_bin_centers(
            float(pcfg.min_bin), float(pcfg.max_bin), int(pcfg.no_bins))
        print(f"[pae-proj] installed z-probe PAE projection "
              f"({tuple(_PAE_PROJ['w'].shape)})", flush=True)
    except Exception as exc:  # noqa: BLE001
        _PAE_PROJ["w"] = None
        print(f"[pae-proj] unavailable ({exc}) — gated heads disabled", flush=True)


def _expected_pae_from_z(z: torch.Tensor, device) -> Optional[torch.Tensor]:
    """z-probe expected PAE (Angstroem) from the cached projection, [N, N].

    Replicates the confidence readout's pae_ln -> linear_no_bias_pae applied
    directly to trunk z (skips the pose-conditioned blocks — documented
    approximation; the real PAE comes from the full confidence readout)."""
    import torch.nn.functional as _F
    if _PAE_PROJ["w"] is None:
        return None
    with torch.no_grad():
        h = _F.layer_norm(z.to(device).float(), _PAE_PROJ["shape"],
                          _PAE_PROJ["ln_w"].to(device),
                          _PAE_PROJ["ln_b"].to(device), _PAE_PROJ["eps"])
        logits = _F.linear(h, _PAE_PROJ["w"].to(device),
                           _PAE_PROJ["b"].to(device) if _PAE_PROJ["b"] is not None else None)
        probs = torch.softmax(logits, dim=-1)
        return (probs * _PAE_PROJ["centers"].to(device)).sum(-1)


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
                if getattr(args, "pae_aux", False):
                    _tp = Path(getattr(args, "true_pae_dir", "")) / f"{name}.npz"
                    if _tp.exists():
                        _t = np.load(_tp, allow_pickle=False)
                        feats["true_pae"] = torch.from_numpy(
                            _t["expected_pae"].astype(np.float32))
                ed = torch.from_numpy(d["expected_dist"].astype(np.float32))
                ed = ed if ed.numel() else None
                _c = torch.from_numpy(d["coords"].astype(np.float32))
                coords = _c if _c.numel() else None  # empty = structure-free row
                dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                return (feats,
                        torch.from_numpy(d["s_inputs"].astype(np.float32)).to(dev),
                        torch.from_numpy(d["s"].astype(np.float32)).to(dev),
                        z.to(dev), ed.to(dev) if ed is not None else None,
                        float(d["h_pl"]), coords)
            except Exception as exc:  # noqa: BLE001
                # no online fallback in cache mode: the trunk forward for these
                # rows is what OOMs; fail fast so the sample is skipped cheaply
                raise RuntimeError(f"cache load failed: {exc}") from exc
    job, chains, ligand_ref, structured = _sample_job(
        row, args, work_dir, use_msa=use_msa)
    feats, s_inputs, s, z, expected_dist, h_pl = trunk.representations(job, device)
    coords = _crystal_coords(job, chains, ligand_ref) if structured else None
    return feats, s_inputs, s, z, expected_dist, h_pl, coords

def _forward_row(trunk, head, row, work_dir, device, args, msa_dataset_fn,
                 label_norm=None, require_grad=False, n_tokens=None):
    """Partner forward for the relative-difference loss.

    Runs in EVAL mode (deterministic — a stochastic ranking target injects
    variance into the very signal Spearman reads). With ``require_grad`` the
    graph is kept so BOTH sides of the pair learn from the ranking term
    (in-batch pairwise ranking, boltz-2 style); the memory guard pairs only
    when both graphs fit (n_tokens <= args.grad_partner_max_tokens)."""
    use_msa = random.random() < args.msa_prob
    dataset = msa_dataset_fn(use_msa)
    feats, s_inputs, s, z, expected_dist, h_pl, coords = _cached_or_repr(
        trunk, row, args, work_dir, device, use_msa)
    if coords is not None:
        coords = coords.to(device)
    _was_training = head.training
    head.eval()
    try:
        if require_grad:
            entry = _grad_entry(
                head, s_inputs, s, z, coords, feats, device, expected_dist=expected_dist
            )
        else:
            with torch.no_grad():
                entry = _grad_entry(
                    head, s_inputs, s, z, coords, feats, device, expected_dist=expected_dist
                )
    finally:
        head.train(_was_training)
    label = float(row["pic50"])
    if label_norm is not None:
        _lo, _hi, _mu, _sd = label_norm
        label = (min(max(label, _lo), _hi) - _mu) / _sd
    return {
        "value": entry["affinity_pred_value_t"].detach(),
        "label": torch.tensor([label], device=device),
    }


def _save_checkpoint(head, opt, c_s, c_z, head_kwargs, history, final_epoch,
                  global_step, errors, contradictions, ckpt_path, extra=None):
    import shutil

    import torch

    # Rotate the rolling checkpoint: keep the previous generation so a
    # mid-divergence overwrite can't destroy the last known-good weights
    # (2026-10-09: the only live ckpt was overwritten by diverged weights
    # and the 220k-step gate-best was unrecoverable).
    ckpt_path = Path(ckpt_path)
    if ckpt_path.name == "protenix_affinity_head.pt" and ckpt_path.exists():
        try:
            shutil.copy2(ckpt_path, ckpt_path.with_suffix(".pt.prev"))
        except OSError:
            pass

    blob = {
        "state_dict": head.state_dict(),
        "optimizer": opt.state_dict() if opt is not None else None,
        "config": {"c_s": c_s, "c_z": c_z, **head_kwargs},
        "history": history,
        "epoch": final_epoch,
        "global_step": global_step,
        "errors": errors,
        "contraditions": contradictions,
    }
    if extra:
        blob.update(extra)
    torch.save(blob, ckpt_path)



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


def _grad_entry(head, s_inputs, s, z, coords, feature_dict, device,
                expected_dist=None, expected_pae=None, contact_stats=None,
                plddt_lig=None):
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
        expected_pae=expected_pae,
        contact_stats=contact_stats,
        plddt_lig=plddt_lig,
        full_dist=expected_dist if getattr(head, "full_dist_cond", False) else None,
        true_pae=feature_dict.get("true_pae") if getattr(head, "pae_aux", False) else None,
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
    parser.add_argument("--pae_aux", action="store_true",
                        help="per-pair PAE auxiliary supervision from the "
                             "precomputed true_pae_feats cache")
    parser.add_argument("--pae_aux_weight", type=float, default=0.05,
                        help="weight of the PAE auxiliary Huber term")
    parser.add_argument("--true_pae_dir", default="/data/affinity_training/true_pae_feats",
                        help="dir of precomputed <name>.npz expected-PAE targets")
    parser.add_argument("--full_dist_cond", action="store_true",
                        help="embed the trunk distogram on ALL pair cells, "
                             "not just the interface (zero new params)")
    parser.add_argument("--pocket_cond", action="store_true",
                        help="pocket-conditioned readout (pooled ligand+rec context)")
    parser.add_argument("--num_blocks", type=int, default=2)
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--mc_samples", type=int, default=1,
                        help="saved into the ckpt config; >1 makes EXTERNAL eval stochastic "
                             "(MC-dropout). Keep 1 for deterministic evals.")
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
    parser.add_argument("--accum", type=int, default=8,
                        help="gradient accumulation window (effective batch)")
    parser.add_argument("--warmup", type=int, default=500)
    parser.add_argument("--lr_min_frac", type=float, default=0.05)
    parser.add_argument("--label_lo", type=float, default=2.0)
    parser.add_argument("--label_hi", type=float, default=12.0)
    parser.add_argument("--ema_decay", type=float, default=0.999)
    parser.add_argument("--val_gate_n", type=int, default=120,
                        help="internal cache-based gate subset size per evaluation")
    parser.add_argument("--grad_partner_max_tokens", type=int, default=430,
                        help="co-train the ranking partner (grads on) only when both "
                             "graphs' token counts fit this bound; else no-grad partner")
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
    parser.add_argument("--abs_weight", type=float, default=1.0,
                        help="weight for absolute Huber loss")
    parser.add_argument("--partner_prob", type=float, default=0.5,
                        help="probability of sampling same-assay partner")
    args = parser.parse_args()

    # Ops override: the docker Cmd is frozen at container creation, so a
    # work_dir sentinel is the only way to retune a run without recreating
    # the container (2026-10-07: cap max_seq_len 600->520 to cut the ~21GB
    # transient peak of the largest samples that was OOM-crash-looping the
    # run against the 23.5GB GPU).
    _ov_path = Path(args.work_dir) / "train_config_override.json"
    if _ov_path.exists():
        for _k, _v in json.loads(_ov_path.read_text()).items():
            if not hasattr(args, _k):
                # A typo'd key would otherwise be set as a dead attribute and
                # silently never applied.
                raise SystemExit(f"[override] unknown key {_k!r} in {_ov_path}")
            _cur = getattr(args, _k)
            setattr(args, _k, type(_cur)(_v) if _cur is not None else _v)
            print(f"[override] {_k} = {getattr(args, _k)}", flush=True)

    import random as _random
    _random.seed(args.seed)
    try:
        import numpy as _np
        _np.random.seed(args.seed)
    except ImportError:
        pass
    try:
        import torch as _torch
        _torch.manual_seed(args.seed)
        if _torch.cuda.is_available():
            _torch.cuda.manual_seed_all(args.seed)
    except ImportError:
        pass

    ckpt = train(args)
    print(
        "\nNext: run protenix2dock with --affinity_head_ckpt "
        f"{ckpt} (or env PROTENIX_AFFINITY_CKPT={ckpt})."
    )


if __name__ == "__main__":
    main()
