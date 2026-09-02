"""HALO command-line interface.

    python -m halo.cli pretrain --run_dir runs/cdk8
    python -m halo.cli run --target cdk8 --run_dir runs/cdk8 [--mock-oracle]
    python -m halo.cli validate-oracle --target cdk2 --gpus 1
    python -m halo.cli analyze --run_dir runs/cdk8
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import torch

from rdkit import Chem

from halo import __version__
from halo.config import HaloConfig
from halo.data.ligands import load_ligand_table, load_smiles_corpus
from halo.data.targets import available_targets, get_target
from halo.generate.prior import SmilesTransformer, pretrain_prior
from halo.generate.vocab import SmilesVocab


def _device(prefer_gpu: int | None = None):
    if not torch.cuda.is_available():
        return "cpu"
    if prefer_gpu is not None and prefer_gpu < torch.cuda.device_count():
        return f"cuda:{prefer_gpu}"
    return "cuda"


def build_models(run_dir: Path, cfg: HaloConfig, corpus: list[str] | None = None,
                 device="cuda", log=print):
    """Load or create vocab + prior + agent under run_dir."""
    vocab_path = run_dir / "vocab.json"
    if vocab_path.exists():
        vocab = SmilesVocab.load(vocab_path)
    else:
        corpus = corpus or []
        vocab = SmilesVocab(corpus)
    import json as _json

    prior_path = run_dir / "prior.pt"
    agent_path = run_dir / "agent.pt"
    meta_path = run_dir / "model_meta.json"
    meta = _json.loads(meta_path.read_text()) if meta_path.exists() else {"model": "transformer"}
    if meta.get("model") == "gpt2":
        from halo.generate.gpt2_prior import GPT2Prior

        if meta.get("tokenizer") == "digitbpe" or (run_dir / "digit_bpe_tokens.json").exists():
            from halo.generate.safe_prior import DigitBPEVocab as _DV

            vocab = _DV.load(run_dir / "digit_bpe_tokens.json")
        elif meta.get("representation") == "safe":
            from halo.generate.safe_prior import SafeVocab as _SV

            vocab = _SV.load(run_dir / "safe_bpe.json")
        if any(str(t).startswith("unified") for t in meta.get("tasks", ())):
            cfg.loop.unified_prior = True
        mk = lambda: GPT2Prior(vocab, d_model=meta.get("d_model", 512), n_layers=meta.get("n_layers", 8),
                               n_heads=meta.get("n_heads", 8), max_len=meta.get("max_len", 192))
    else:
        mk = lambda: SmilesTransformer(vocab, cfg.generator.d_model, cfg.generator.n_layers,
                                       cfg.generator.n_heads, cfg.generator.dropout)
    prior = mk()
    if prior_path.exists():
        prior.load_state_dict(torch.load(prior_path, map_location=device, weights_only=True))
        trained = True
    else:
        trained = False
    agent = mk()
    if agent_path.exists():
        agent.load_state_dict(torch.load(agent_path, map_location=device, weights_only=True))
    elif trained:
        agent.load_state_dict(prior.state_dict())
    return vocab, prior, agent, trained


def cmd_pretrain(args) -> None:
    from halo.generate.vocab import build_fragment_regex, mine_fragments, tokenize

    cfg = HaloConfig.load(args.run_dir / "config.json") if (args.run_dir / "config.json").exists() else HaloConfig()
    cfg.target_name = args.target
    args.run_dir.mkdir(parents=True, exist_ok=True)
    cfg.save(args.run_dir / "config.json")

    target = get_target(args.target)
    chembl_path = Path(args.chembl)
    corpus = load_corpus_smiles(chembl_path, args.chembl_limit)
    lig = load_ligand_table(target.ligands_sdf)
    corpus = corpus + [s for s in lig["smiles"].dropna() if isinstance(s, str)]
    log = print
    log(f"[pretrain] corpus={len(corpus)} (chembl={len(corpus) - len(lig)}, target={len(lig)})")

    if args.tokenizer == "fragment":
        fragments = mine_fragments(corpus, max_molecules=args.mine_molecules, min_count=args.min_count)
        log(f"[pretrain] mined {len(fragments)} fragments (min_count={args.min_count})")
        pattern = build_fragment_regex(fragments)
        vocab = SmilesVocab(corpus, fragment_pattern=pattern, fragments=fragments)
        import random as _random

        sample = _random.Random(0).sample(corpus, min(2000, len(corpus)))
        bad = sum(1 for s in sample if "".join(tokenize(s, pattern)) != s)
        log(f"[pretrain] roundtrip mismatches: {bad}/{len(sample)}")
        assert bad < len(sample) * 0.01, "fragment tokenizer loses characters - refusing to train"
        # report compression
        n_atom = sum(len(tokenize(s)) for s in sample)
        log(f"[pretrain] tokens/molecule on sample: {n_atom / len(sample):.1f}")
    else:
        vocab = SmilesVocab(corpus)
    log(f"[pretrain] vocab size {len(vocab)}")
    vocab.save(args.run_dir / "vocab.json")
    device = _device(args.gpu if hasattr(args, "gpu") else None)

    if args.model == "gpt2":
        from halo.generate.gpt2_prior import GPT2Prior, pretrain_gpt2

        model = GPT2Prior(vocab, d_model=args.d_model, n_layers=args.n_layers, n_heads=8, max_len=192)
        n_params = sum(p.numel() for p in model.parameters())
        log(f"[pretrain] GPT-2 prior: {n_params/1e6:.1f}M params on {device}")
        import random as _random

        rng = _random.Random(0)
        rng.shuffle(corpus)
        n_val = max(2000, min(10000, len(corpus) // 200))
        val, train = corpus[:n_val], corpus[n_val:]
        hist = pretrain_gpt2(model, train, val, vocab, epochs=args.epochs or 2,
                             batch_size=args.batch_size, lr=args.lr, device=device,
                             log=log, save_best=args.run_dir / "prior.pt")
        torch.save(model.state_dict(), args.run_dir / "prior.pt")
        import json as _json

        (args.run_dir / "model_meta.json").write_text(_json.dumps(
            {"model": "gpt2", "d_model": args.d_model, "n_layers": args.n_layers,
             "n_heads": 8, "max_len": 192}))
        model.to("cpu")
        torch.save(model.state_dict(), args.run_dir / "agent.pt")
        model.to(device)
    else:
        model = SmilesTransformer(vocab, cfg.generator.d_model, cfg.generator.n_layers,
                                  cfg.generator.n_heads, cfg.generator.dropout)
        hist = pretrain_prior(model, corpus, vocab, epochs=args.epochs or cfg.generator.max_epochs,
                              batch_size=cfg.generator.batch_size, lr=cfg.generator.lr,
                              device=device, log=log)
        torch.save(model.state_dict(), args.run_dir / "prior.pt")
        model.to("cpu")
        torch.save(model.state_dict(), args.run_dir / "agent.pt")
        model.to(device)
    # MOSES-style evaluation
    from rdkit import Chem

    for temp in (1.0, 0.8):
        samples = model.sample(64, device, temperature=temp)
        valid = sum(1 for s in samples if Chem.MolFromSmiles(s) is not None)
        uniq = len({s for s in samples if Chem.MolFromSmiles(s) is not None})
        log(f"[pretrain] best val loss {hist['best_val_loss']:.4f}; "
            f"validity {valid}/64 unique {uniq} (T={temp})")

    # MOSES-style evaluation on a larger sample (train sample for novelty)
    try:
        from halo.bench.moses_eval import evaluate_samples, sample_reference

        import random as _random

        samples = model.sample(2000, device, temperature=1.0)
        train_ref = _random.Random(1).sample(corpus, min(10000, len(corpus)))
        metrics = evaluate_samples(samples, train_ref, fcd_reference=sample_reference(args.chembl, 5000), device=device)
        log(f"[pretrain] MOSES metrics: {metrics}")
    except Exception as e:
        log(f"[pretrain] MOSES eval skipped: {type(e).__name__}: {e}")
    samples = model.sample(6, device, temperature=0.9)
    for s in samples:
        log("   ", s)


def load_corpus_smiles(path: Path, limit: int | None) -> list[str]:
    """Load a corpus .smi/.txt file (tab or space separated, header tolerant).

    Lines that are already canonical single-fragment SMILES (e.g. exported
    """
    path = Path(path)
    if path.name.endswith(".tsv") or path.name.endswith(".txt"):
        from halo.data.ligands import load_chembl_tsv

        return load_chembl_tsv(path, limit=limit)
    out, seen = [], set()
    for line in path.read_text().splitlines():
        smi = line.split("\t")[0].split()[0] if line.strip() else ""
        if not smi or "." in smi or "*" in smi:
            continue
        if smi in seen:
            continue
        seen.add(smi)
        out.append(smi)
        if limit and len(out) >= limit:
            break
    return out


def cmd_focus(args) -> None:
    """Focused prior: continue-pretrain a base prior on neighbours of a reference.

    The REINVENT lead-opt recipe: given a reference compound (arbitrary user
    lead, optionally plus a target's ligand series), retrieve its nearest
    ChEMBL36 neighbours and transfer-learn the prior onto that focused corpus
    so generation starts inside the right chemical space.
    """
    import json as _json

    import torch

    from halo.data.ligands import load_ligand_table
    from halo.data.neighbors import find_neighbors
    from halo.generate.gpt2_prior import GPT2Prior, pretrain_gpt2
    from halo.generate.vocab import SmilesVocab

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    device = _device(args.gpu)
    refs = [args.reference] if args.reference else []
    if args.target:
        refs += load_ligand_table(get_target(args.target).ligands_sdf)["smiles"].dropna().tolist()
    refs = [s for s in refs if isinstance(s, str) and Chem.MolFromSmiles(s)]
    if not refs:
        raise SystemExit("no valid reference SMILES (use --reference and/or --target)")
    log = print
    log(f"[focus] {len(refs)} reference compounds")

    corpus_path = Path(args.chembl)
    cache = corpus_path.with_suffix(".fp2048.npz")
    neighbors, sims = find_neighbors(refs, corpus_path, cache, topk=args.topk)
    log(f"[focus] {len(neighbors)} ChEMBL neighbours (sim {sims.min():.2f}..{sims.max():.2f}, median {np_median(sims):.2f})")
    # focused corpus: neighbours + upweighted reference series
    corpus = neighbors + refs * args.ref_repeat

    base = Path(args.base_prior)
    vocab = SmilesVocab.load(base / "vocab.json")
    meta = _json.loads((base / "model_meta.json").read_text())
    model = GPT2Prior(vocab, d_model=meta["d_model"], n_layers=meta["n_layers"],
                      n_heads=meta["n_heads"], max_len=meta.get("max_len", 192))
    sd = torch.load(base / "prior.pt", map_location="cpu", weights_only=True)
    model.load_state_dict(sd)
    import random as _random

    rng = _random.Random(0)
    rng.shuffle(corpus)
    n_val = max(300, len(corpus) // 100)
    val, train = corpus[:n_val], corpus[n_val:]
    hist = pretrain_gpt2(model, train, val, vocab, epochs=args.epochs, batch_size=args.batch_size,
                         lr=args.lr, device=device, log=log, save_best=out_dir / "prior.pt")
    torch.save(model.state_dict(), out_dir / "prior.pt")
    model.to("cpu")
    torch.save(model.state_dict(), out_dir / "agent.pt")
    model.to(device)
    vocab.save(out_dir / "vocab.json")
    (out_dir / "model_meta.json").write_text(_json.dumps(meta))
    # quick focused-ness check: mean Tanimoto of samples to the references
    from rdkit.Chem import rdFingerprintGenerator

    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    from rdkit import DataStructs

    ref_fps = [gen.GetFingerprint(Chem.MolFromSmiles(r)) for r in refs if Chem.MolFromSmiles(r)]
    samples = model.sample(256, device, temperature=1.0)
    sims_s = []
    for s in samples:
        m = Chem.MolFromSmiles(s)
        if m is None:
            continue
        f = gen.GetFingerprint(m)
        sims_s.append(max(DataStructs.TanimotoSimilarity(f, r) for r in ref_fps))
    log(f"[focus] valid {len(sims_s)}/256, mean max-Tanimoto(samples->refs) {sum(sims_s)/max(len(sims_s),1):.3f}")
    (out_dir / "focus_meta.json").write_text(_json.dumps(
        {"refs": refs[:50], "n_neighbors": len(neighbors), "best_val_loss": hist["best_val_loss"],
         "sample_mean_sim": sum(sims_s) / max(len(sims_s), 1)}, indent=1))


def _FP_Generator():
    from rdkit.Chem import rdFingerprintGenerator

    return rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)


def np_median(a):
    import numpy as _np

    return float(_np.median(a))


def cmd_optimize(args) -> None:
    """Unified lead-optimization entry point.

    Scenarios:
      (a) --protein + --reference (SDF/SMILES)     optimize around a lead
      (b) --protein (+ --pocket x,y,z)             de novo from the pocket
      (c) --protein + --keep_fragment SMILES/SDF   conserve a required fragment
      (d) --protein + --reference + --edit_atoms   replace the fragment at a
           user-specified position (or let Boltz2Score per-atom pLDDT pick it)
    """
    import shutil

    import torch
    from rdkit import Chem

    from halo.config import HaloConfig
    from halo.data.targets import Target
    from halo.loop.engine import HaloLoop, MockOracle
    from halo.loop.human import CLIHuman, NoopHuman, SimulatedChemist

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    protein = Path(args.protein).resolve()
    if not protein.exists():
        raise SystemExit(f"protein not found: {protein}")

    # ---- build the target: reference ligand bookkeeping ----
    refs: list[str] = []
    ligand_sdf = run_dir / "reference.sdf"
    if args.reference:
        rp = Path(args.reference)
        if rp.suffix in (".sdf", ".sd") and rp.exists():
            mols = [m for m in Chem.SDMolSupplier(str(rp), removeHs=False) if m]
        else:
            from halo.oracle.pose import embed_conformers

            m = embed_conformers(args.reference, 1) if Chem.MolFromSmiles(args.reference) else None
            mols = [m] if m is not None else []
            if not mols and args.pocket:  # place at pocket centroid
                xyz = [float(v) for v in args.pocket.split(",")]
                from halo.oracle.pose import _centroid_place

                mols = [_centroid_place(Chem.AddHs(Chem.MolFromSmiles(args.reference)), _dummy_ref(xyz))]
        if not mols:
            raise SystemExit("could not read reference molecules")
        w = Chem.SDWriter(str(ligand_sdf))
        for m in mols:
            w.write(m)
            smi = Chem.MolToSmiles(Chem.RemoveHs(m)) if m.GetNumAtoms() else None
            if smi:
                refs.append(smi)
        w.close()
    elif args.keep_fragment:
        # scenario c: no full reference; use the fragment (placed at pocket) as
        # alignment seed and constraint
        kf = Chem.MolFromSmiles(args.keep_fragment)
        if kf is None:
            raise SystemExit("keep_fragment is not valid SMILES")
        if args.pocket:
            xyz = [float(v) for v in args.pocket.split(",")]
            from halo.oracle.pose import _centroid_place

            kf3 = _centroid_place(Chem.AddHs(kf), _dummy_ref(xyz))
        else:
            from halo.oracle.pose import embed_conformers

            kf3 = embed_conformers(args.keep_fragment, 1)
        w = Chem.SDWriter(str(ligand_sdf))
        w.write(kf3)
        w.close()
        refs = [args.keep_fragment]
    else:
        # scenario b: pocket-only de novo; seed the loop from generated samples
        if not args.pocket:
            raise SystemExit("scenario (b) needs --pocket x,y,z (or provide a reference/fragment)")
        # seed set: none yet - engine falls back to agent-only proposals
        ligand_sdf = None

    target = Target(name=protein.stem[:20], protein_pdb=protein,
                    ligands_sdf=ligand_sdf if ligand_sdf and ligand_sdf.exists() else _empty_sdf(run_dir),
                    target_chain=args.chain, ligand_chain="L")

    cfg = HaloConfig.load(run_dir / "config.json") if (run_dir / "config.json").exists() else HaloConfig()
    cfg.target_name = target.name
    cfg.loop.n_rounds = args.rounds
    cfg.oracle.gpus = tuple(args.gpus) if args.gpus else cfg.oracle.gpus
    cfg.loop.reference_smiles = refs[0] if refs else ""
    cfg.loop.keep_fragment_smiles = args.keep_fragment or ""
    if args.edit_atoms:
        cfg.loop.edit_atom_indices = tuple(int(x) for x in str(args.edit_atoms).split(",") if x.strip())
    cfg.loop.use_human = args.human != "none"
    if args.scaffold_hop:
        cfg.loop.scaffold_hop_ratio = args.scaffold_hop
    cfg.save(run_dir / "config.json")

    device = _device(cfg.oracle.gpus[0] if cfg.oracle.gpus else None)
    model_dir = Path(args.prior_dir) if args.prior_dir else run_dir
    if model_dir != run_dir and (model_dir / "prior.pt").exists():
        for fname in ("prior.pt", "vocab.json", "model_meta.json"):
            src, dst = model_dir / fname, run_dir / fname
            if src.exists() and not dst.exists():
                dst.write_bytes(src.read_bytes())
    corpus = load_smiles_corpus(Path(args.chembl), limit=4000) if Path(args.chembl).exists() else []
    vocab, prior, agent, trained = build_models(run_dir, cfg, corpus, device)
    prior.to(device)
    agent.to(device)
    if not trained and not args.mock_oracle:
        raise SystemExit("prior not trained (use --prior_dir runs/prior_unified)")

    if args.mock_oracle:
        oracle = MockOracle(target, seed=cfg.seed)
    else:
        from halo.oracle.boltz_client import BoltzOracle

        oracle = BoltzOracle(target, run_dir / "oracle_work", gpus=cfg.oracle.gpus,
                             score_batch_size=cfg.oracle.score_batch_size,
                             recycling_steps=cfg.oracle.recycling_steps,
                             affinity_recycling_steps=cfg.oracle.affinity_recycling_steps,
                             precision=cfg.oracle.precision, timeout_s=cfg.oracle.score_timeout_s)

    if args.human == "sim":
        from halo.data.ligands import load_ligand_table

        table = load_ligand_table(target.ligands_sdf).dropna(subset=["activity_pic50"])
        gt = dict(zip(table["smiles"], table["activity_pic50"])) if len(table) else {}
        human = SimulatedChemist(gt or {refs[0]: 8.0} if refs else {}, seed=cfg.seed)
    elif args.human == "cli":
        human = CLIHuman(run_dir / "depictions")
    else:
        human = NoopHuman()

    loop = HaloLoop(cfg, target, prior, agent, vocab, oracle, human, run_dir, device=device)
    loop.run()
    from halo.bench.analyze import summarize

    summarize(run_dir)


def _dummy_ref(xyz):
    """A 1-atom reference molecule at the given pocket centre (for placement)."""
    from rdkit import Chem
    from rdkit.Geometry import Point3D

    m = Chem.MolFromSmiles("C")
    m = Chem.AddHs(m)
    conf = m.GetConformer()
    for i in range(m.GetNumAtoms()):
        conf.SetAtomPosition(i, Point3D(*xyz))
    return m


def _empty_sdf(run_dir: Path) -> Path:
    from rdkit import Chem

    p = run_dir / "reference.sdf"
    if not p.exists():
        m = Chem.MolFromSmiles("c1ccccc1")
        from halo.oracle.pose import embed_conformers

        m3 = embed_conformers("c1ccccc1", 1)
        w = Chem.SDWriter(str(p))
        w.write(m3 or m)
        w.close()
    return p


def cmd_run(args) -> None:
    from halo.loop.engine import HaloLoop, MockOracle
    from halo.loop.human import CLIHuman, FileHuman, NoopHuman, SimulatedChemist

    cfg = HaloConfig.load(args.run_dir / "config.json") if (args.run_dir / "config.json").exists() else HaloConfig()
    cfg.target_name = args.target
    if args.gpus:
        cfg.oracle.gpus = tuple(args.gpus)
    if args.rounds:
        cfg.loop.n_rounds = args.rounds
    if args.no_surrogate:
        cfg.loop.use_surrogate = False
    if args.no_agent:
        cfg.loop.use_agent = False
    if args.no_human:
        cfg.loop.use_human = False
    cfg.save(args.run_dir / "config.json")

    target = get_target(args.target)
    device = _device(cfg.oracle.gpus[0] if cfg.oracle.gpus else None)

    chembl_path = Path(args.chembl)
    corpus = load_smiles_corpus(chembl_path, limit=4000) if chembl_path.exists() else []
    lig = load_ligand_table(target.ligands_sdf)
    model_dir = Path(args.prior_dir) if args.prior_dir else args.run_dir
    if model_dir != args.run_dir and (model_dir / "prior.pt").exists():
        for fname in ("prior.pt", "vocab.json", "model_meta.json"):
            src, dst = model_dir / fname, args.run_dir / fname
            if src.exists() and not dst.exists():
                dst.write_bytes(src.read_bytes())
    vocab, prior, agent, trained = build_models(args.run_dir, cfg, corpus + lig["smiles"].dropna().tolist(), device)
    prior.to(device)
    agent.to(device)
    if not trained and not args.mock_oracle:
        raise SystemExit("prior not trained - run `pretrain` first (or use --mock-oracle for smoke tests)")

    if args.mock_oracle:
        oracle = MockOracle(target, seed=cfg.seed)
    else:
        from halo.oracle.boltz_client import BoltzOracle

        oracle = BoltzOracle(target, args.run_dir / "oracle_work", gpus=cfg.oracle.gpus,
                             score_batch_size=cfg.oracle.score_batch_size,
                             recycling_steps=cfg.oracle.recycling_steps,
                             affinity_recycling_steps=cfg.oracle.affinity_recycling_steps,
                             precision=cfg.oracle.precision, timeout_s=cfg.oracle.score_timeout_s)

    if args.human == "sim":
        table = lig.dropna(subset=["activity_pic50"])
        human = SimulatedChemist(dict(zip(table["smiles"], table["activity_pic50"])),
                                 gt_weight=args.sim_gt_weight, seed=cfg.seed)
    elif args.human == "cli":
        human = CLIHuman(args.run_dir / "depictions")
    elif args.human == "file":
        human = FileHuman(args.run_dir / "feedback.json")
    else:
        human = NoopHuman()

    loop = HaloLoop(cfg, target, prior, agent, vocab, oracle, human, args.run_dir, device=device)
    loop.run()


def cmd_validate(args) -> None:
    from halo.bench.oracle_validation import run_validation

    run_validation(target_names=args.targets, gpus=tuple(args.gpus), out_dir=Path(args.out_dir),
                   rounds=args.rounds, limit=args.limit)


def cmd_analyze(args) -> None:
    from halo.bench.analyze import analyze_run

    analyze_run(Path(args.run_dir))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="halo", description="HALO human-in-the-loop lead optimization")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("pretrain")
    pp.add_argument("--target", default="cdk8", choices=available_targets())
    pp.add_argument("--run_dir", type=Path, required=True)
    pp.add_argument("--chembl", default="/data/V-Bio/data/chembl_raw_data.txt")
    pp.add_argument("--chembl_limit", type=int, default=60000)
    pp.add_argument("--epochs", type=int, default=None)
    pp.add_argument("--tokenizer", default="fragment", choices=["fragment", "atom"])
    pp.add_argument("--mine_molecules", type=int, default=60000)
    pp.add_argument("--min_count", type=int, default=300)
    pp.add_argument("--model", default="gpt2", choices=["gpt2", "transformer"])
    pp.add_argument("--d_model", type=int, default=512)
    pp.add_argument("--n_layers", type=int, default=8)
    pp.add_argument("--batch_size", type=int, default=384)
    pp.add_argument("--lr", type=float, default=6e-4)
    pp.add_argument("--gpu", type=int, default=None)
    pp.add_argument("--gpus", type=int, nargs="*", default=None)
    pp.set_defaults(func=cmd_pretrain)

    pf = sub.add_parser("focus")
    pf.add_argument("--reference", default=None, help="reference compound SMILES (the user's lead)")
    pf.add_argument("--target", default=None, choices=available_targets(), help="also focus on this target's ligand series")
    pf.add_argument("--base_prior", type=Path, required=True)
    pf.add_argument("--out_dir", type=Path, required=True)
    pf.add_argument("--topk", type=int, default=30000)
    pf.add_argument("--ref_repeat", type=int, default=20)
    pf.add_argument("--epochs", type=int, default=2)
    pf.add_argument("--batch_size", type=int, default=384)
    pf.add_argument("--lr", type=float, default=3e-4)
    pf.add_argument("--gpu", type=int, default=None)
    pf.add_argument("--chembl", default="runs/chembl36_corpus.smi")
    pf.set_defaults(func=cmd_focus)

    po = sub.add_parser("optimize", help="four-scenario lead optimization entry point")
    po.add_argument("--protein", required=True)
    po.add_argument("--reference", default=None, help="reference SDF or SMILES (scenario a/d)")
    po.add_argument("--keep_fragment", default=None, help="SMILES of a must-keep fragment (scenario c)")
    po.add_argument("--edit_atoms", default=None, help="comma atom indices to edit (scenario d)")
    po.add_argument("--scaffold_hop", type=float, default=0.0, metavar="RATIO",
                    help="fraction of edits that are scaffold hops (e.g. 0.3)")
    po.add_argument("--pocket", default=None, help="x,y,z pocket centre for scenario b/c")
    po.add_argument("--chain", default="A")
    po.add_argument("--target", default=None, choices=available_targets(), help="use a benchmark target's series as extra context")
    po.add_argument("--run_dir", type=Path, required=True)
    po.add_argument("--rounds", type=int, default=6)
    po.add_argument("--gpus", type=int, nargs="*", default=[1, 2])
    po.add_argument("--human", default="none", choices=["none", "sim", "cli"])
    po.add_argument("--prior_dir", default=None)
    po.add_argument("--chembl", default="/data/V-Bio/data/chembl_compounds.smi")
    po.add_argument("--mock-oracle", action="store_true")
    po.set_defaults(func=cmd_optimize)

    pr = sub.add_parser("run")
    pr.add_argument("--target", default="cdk8", choices=available_targets())
    pr.add_argument("--run_dir", type=Path, required=True)
    pr.add_argument("--rounds", type=int, default=None)
    pr.add_argument("--gpus", type=int, nargs="*", default=None)
    pr.add_argument("--human", default="none", choices=["none", "sim", "cli", "file"])
    pr.add_argument("--sim_gt_weight", type=float, default=0.5)
    pr.add_argument("--chembl", default="/data/V-Bio/data/chembl_compounds.smi")
    pr.add_argument("--prior_dir", default=None, help="reuse a trained prior/vocab from another run dir")
    pr.add_argument("--mock-oracle", action="store_true")
    pr.add_argument("--no-surrogate", action="store_true")
    pr.add_argument("--no-agent", action="store_true")
    pr.add_argument("--no-human", action="store_true")
    pr.set_defaults(func=cmd_run)

    pv = sub.add_parser("validate-oracle")
    pv.add_argument("--targets", nargs="+", default=["cdk2"])
    pv.add_argument("--gpus", type=int, nargs="*", default=[1, 2, 3])
    pv.add_argument("--out_dir", default="runs/oracle_validation")
    pv.add_argument("--rounds", type=int, default=1)
    pv.add_argument("--limit", type=int, default=None)
    pv.set_defaults(func=cmd_validate)

    pa = sub.add_parser("analyze")
    pa.add_argument("--run_dir", type=Path, required=True)
    pa.set_defaults(func=cmd_analyze)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
