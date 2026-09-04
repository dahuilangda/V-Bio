"""Server-side HALO runner: adapts the CLI optimize flow for the V-Bio worker.

Called by the Celery lead-optimization task with a plain payload dict; builds
the Target/config/models, runs the closed loop with the native prediction
oracle (protenix2dock default; boltz2dock / alphafold3 optional), and reports
progress through a callback.

Modes (mirrors halo CLI scenarios):
  denovo       — pocket-only generation (no reference ligand)
  fragment     — fragment replacement around a reference lead (keep_fragment
                 and/or edit atoms optional)
  scaffold_hop — reference lead with scaffold_hop_ratio > 0

Everything resolves inside V-Bio: priors and the novelty corpus live under
capabilities/halo/runs, the oracle submits through the local V-Bio API.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from halo import RUNS_DIR
from halo.config import HaloConfig

_DEFAULT_PRIOR = RUNS_DIR / "prior_mv_rag2"

Mode = str  # "denovo" | "fragment" | "scaffold_hop"


def _resolve_prior_dir(prior_dir: Optional[str]) -> Path:
    candidate = Path(prior_dir) if prior_dir else _DEFAULT_PRIOR
    if not candidate.is_absolute():
        candidate = RUNS_DIR / candidate
    if not (candidate / "prior.pt").exists():
        raise FileNotFoundError(
            f"HALO prior not found at {candidate} (expected prior.pt). "
            f"Ship a prior under capabilities/halo/runs or set prior_dir."
        )
    return candidate


def _embed_reference(smiles: str, out_sdf: Path, pocket_xyz=None) -> None:
    from rdkit import Chem

    from halo.oracle.pose import embed_conformers

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"reference_smiles is not valid SMILES: {smiles}")
    if pocket_xyz:
        from halo.oracle.pose import _centroid_place

        placed = _centroid_place(Chem.AddHs(mol), _dummy_ref(pocket_xyz))
    else:
        placed = embed_conformers(smiles, 1)
    if placed is None:
        raise ValueError(f"could not embed reference molecule: {smiles}")
    writer = Chem.SDWriter(str(out_sdf))
    writer.write(placed)
    writer.close()


def _dummy_ref(xyz):
    from rdkit import Chem
    from rdkit.Geometry import Point3D

    mol = Chem.AddHs(Chem.MolFromSmiles("C"))
    conf = mol.GetConformer()
    for i in range(mol.GetNumAtoms()):
        conf.SetAtomPosition(i, Point3D(*xyz))
    return mol


class _RoundProgressWatcher(threading.Thread):
    """Tails the loop's round artifacts and emits peptide-style progress.

    The engine rewrites rounds.jsonl and appends candidates.csv at every round
    boundary (after the GRPO update), so polling those files needs no engine
    changes and survives any internal round failure.
    """

    _STATS_FIELDS = (
        "round", "pool", "oracle_calls", "oracle_s", "surrogate_n",
        "top_final_reward", "mean_final_reward", "best_affinity_oracle",
        "alpha", "elapsed_s",
    )

    def __init__(self, run_dir: Path, total_rounds: int, report: Callable[[dict], None], interval_s: float = 5.0):
        super().__init__(daemon=True, name="halo-round-watcher")
        self.run_dir = Path(run_dir)
        self.total_rounds = int(total_rounds)
        self.report = report
        self.interval_s = float(interval_s)
        self._shutdown = threading.Event()
        self.last_round = 0

    def stop(self) -> None:
        self._shutdown.set()

    def run(self) -> None:
        while not self._shutdown.wait(self.interval_s):
            try:
                self._poll_once()
            except Exception:
                # Progress reporting must never kill the optimization.
                continue

    def _poll_once(self) -> None:
        stats = self._read_rounds()
        if not stats:
            return
        latest_round = int(stats[-1].get("round") or 0)
        if latest_round <= self.last_round:
            return
        self.last_round = latest_round
        latest = stats[-1]
        compact_stats = {k: latest.get(k) for k in self._STATS_FIELDS if latest.get(k) is not None}
        best_affinity = latest.get("best_affinity_oracle")
        message = (
            f"round {latest_round}/{self.total_rounds}: "
            + (f"best affinity pIC50 {float(best_affinity):.2f}" if best_affinity is not None else "round complete")
        )
        self.report({
            "stage": "round",
            "round": latest_round,
            "total_rounds": self.total_rounds,
            "message": message,
            "stats": compact_stats,
            "top_candidates": self._top_candidates(),
        })

    def _read_rounds(self) -> list[dict]:
        path = self.run_dir / "rounds.jsonl"
        if not path.exists():
            return []
        rows: list[dict] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def _top_candidates(self, limit: int = 12) -> list[dict]:
        import pandas as pd

        path = self.run_dir / "candidates.csv"
        if not path.exists():
            return []
        try:
            frame = pd.read_csv(path)
        except Exception:
            return []
        needed = {"smiles", "final_reward"}
        if not needed.issubset(frame.columns):
            return []
        ranked = frame.sort_values("final_reward", ascending=False).head(limit)
        rows = []
        for _, row in ranked.iterrows():
            entry = {
                "smiles": str(row.get("smiles") or ""),
                "final_reward": _finite_or_none(row.get("final_reward")),
                "round": _finite_or_none(row.get("round")),
            }
            for key in ("affinity_pic50", "ipsae", "ligand_plddt_mean", "source"):
                value = row.get(key)
                if value is not None and str(value) != "nan":
                    entry[key] = value
            rows.append(entry)
        return rows


def _finite_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN guard


class _ProgressOracle:
    """Wraps the prediction oracle to emit per-round scoring events.

    The engine calls score_smiles exactly once per round (tag "rNNN"), so the
    call boundary is the round boundary; the wrapper reports before scoring
    starts — the phase where all the wall-clock time goes.
    """

    def __init__(self, inner, report: Callable[[dict], None], total_rounds: int):
        self.inner = inner
        self.report = report
        self.total_rounds = int(total_rounds)

    def score_smiles(self, smiles_list, tag="batch"):
        round_number = self._round_from_tag(tag)
        event = {
            "stage": "scoring",
            "message": f"scoring {len(smiles_list)} candidates",
            "candidates": len(smiles_list),
        }
        if round_number is not None:
            event["round"] = round_number
            event["total_rounds"] = self.total_rounds
        self.report(event)
        return self.inner.score_smiles(smiles_list, tag)

    @staticmethod
    def _round_from_tag(tag: str) -> int | None:
        digits = "".join(ch for ch in str(tag) if ch.isdigit())
        return int(digits) if digits else None

    def __getattr__(self, name):
        return getattr(self.inner, name)


def run_halo_optimization(
    payload: dict,
    run_dir: Path,
    progress_cb: Optional[Callable[[dict], None]] = None,
    log=print,
) -> dict:
    """Run one HALO closed-loop optimization. Returns the final-state summary."""
    import torch

    from halo.cli import build_models
    from halo.data.targets import Target
    from halo.loop.engine import HaloLoop
    from halo.loop.human import NoopHuman
    from halo.oracle.predict_oracle import PredictOracle

    def report(message, stage: str = "loop", **fields):
        if progress_cb is None:
            return
        if isinstance(message, dict) and stage == "loop" and not fields:
            # Watcher / oracle wrapper events arrive fully formed.
            progress_cb(message)
            return
        progress_cb({"stage": stage, "message": message, **fields})

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    mode = str(payload.get("mode") or "fragment").strip().lower()
    if mode not in {"denovo", "fragment", "scaffold_hop"}:
        raise ValueError(f"Unknown HALO mode '{mode}' (denovo | fragment | scaffold_hop).")

    protein_path = Path(payload.get("protein_path") or "")
    if not str(payload.get("protein_path") or "").strip():
        raise ValueError("protein structure (protein_path) is required.")

    reference_smiles = str(payload.get("reference_smiles") or "").strip()
    keep_fragment = str(payload.get("keep_fragment_smiles") or "").strip()
    pocket = payload.get("pocket")  # "x,y,z" for de novo
    pocket_xyz = None
    if pocket:
        try:
            pocket_xyz = [float(v) for v in str(pocket).split(",")]
            if len(pocket_xyz) != 3:
                raise ValueError
        except ValueError:
            raise ValueError("pocket must be 'x,y,z'") from None

    # An uploaded 3D reference (real binding pose) wins over re-embedding from
    # SMILES; its first molecule's canonical SMILES seeds the similarity band.
    reference_sdf_path = str(payload.get("reference_sdf_path") or "").strip()
    ligand_sdf: Optional[Path] = None
    if reference_sdf_path and Path(reference_sdf_path).exists():
        ligand_sdf = Path(reference_sdf_path)
        if not reference_smiles:
            from rdkit import Chem

            supplier = Chem.SDMolSupplier(str(ligand_sdf), removeHs=True)
            first = next((mol for mol in supplier if mol), None)
            if first is not None:
                reference_smiles = Chem.MolToSmiles(first)
    elif reference_smiles:
        ligand_sdf = run_dir / "reference.sdf"
        _embed_reference(reference_smiles, ligand_sdf, pocket_xyz)
    elif keep_fragment:
        ligand_sdf = run_dir / "reference.sdf"
        _embed_reference(keep_fragment, ligand_sdf, pocket_xyz)
    elif mode != "denovo":
        raise ValueError(f"mode '{mode}' needs reference_smiles (and/or keep_fragment_smiles)")
    if mode == "denovo" and not pocket_xyz and not reference_smiles:
        raise ValueError("de novo mode needs pocket 'x,y,z' or a reference ligand for placement")

    if not protein_path.exists():
        raise FileNotFoundError(f"protein structure not found: {protein_path}")

    if ligand_sdf is None:
        # Engine bookkeeping expects an SDF path; write a placeholder at the
        # pocket centroid when the mode has no reference at all.
        ligand_sdf = run_dir / "reference.sdf"
        from rdkit import Chem

        xyz = pocket_xyz or (0.0, 0.0, 0.0)
        placeholder = _dummy_ref(xyz)
        writer = Chem.SDWriter(str(ligand_sdf))
        writer.write(placeholder)
        writer.close()

    target = Target(
        name=str(payload.get("target_name") or protein_path.stem[:20]),
        protein_pdb=protein_path,
        ligands_sdf=ligand_sdf,
        target_chain=str(payload.get("target_chain") or "A"),
        ligand_chain="L",
    )

    cfg = HaloConfig()
    cfg.target_name = target.name
    cfg.run_dir = str(run_dir)
    cfg.seed = int(payload.get("seed") or 0)
    cfg.loop.n_rounds = int(payload.get("rounds") or 6)
    cfg.loop.oracle_budget_per_round = int(payload.get("budget_per_round") or 48)
    if payload.get("n_agent_samples"):
        cfg.loop.n_agent_samples = int(payload["n_agent_samples"])
    cfg.loop.reference_smiles = reference_smiles
    cfg.loop.keep_fragment_smiles = keep_fragment
    edit_atoms = payload.get("edit_atom_indices")
    if isinstance(edit_atoms, (list, tuple)):
        cfg.loop.edit_atom_indices = tuple(int(x) for x in edit_atoms)
    elif edit_atoms:
        cfg.loop.edit_atom_indices = tuple(int(x) for x in str(edit_atoms).split(",") if str(x).strip())
    if mode == "scaffold_hop":
        cfg.loop.scaffold_hop_ratio = float(payload.get("scaffold_hop_ratio") or 0.4)
    cfg.loop.use_human = False
    cfg.save(run_dir / "config.json")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    prior_dir = _resolve_prior_dir(payload.get("prior_dir"))
    for fname in ("prior.pt", "agent.pt", "digit_bpe_tokens.json", "safe_bpe.json", "vocab.json", "model_meta.json"):
        src = prior_dir / fname
        dst = run_dir / fname
        if src.exists() and not dst.exists():
            dst.write_bytes(src.read_bytes())
    report(f"loading prior from {prior_dir.name}", stage="init")
    vocab, prior, agent, trained = build_models(run_dir, cfg, [], device)
    if not trained:
        raise RuntimeError(f"prior weights missing after staging from {prior_dir}")
    prior.to(device)
    agent.to(device)

    if payload.get("mock_oracle"):
        # CPU stand-in for pipeline tests: the loop, RL updates, and progress
        # reporting all run unchanged; only the scoring head differs.
        from halo.loop.engine import MockOracle

        oracle = MockOracle(target, seed=cfg.seed)
    else:
        oracle = PredictOracle(
            target,
            run_dir / "oracle_work",
            backend=str(payload.get("backend") or PredictOracle.DEFAULT_BACKEND),
            timeout_s=int(payload.get("oracle_timeout_s") or 7200),
            priority=str(payload.get("priority") or "default"),
            seed=cfg.seed or None,
            log=log,
        )

    progress_oracle = _ProgressOracle(oracle, report, cfg.loop.n_rounds)
    watcher = _RoundProgressWatcher(run_dir, cfg.loop.n_rounds, report)
    loop = HaloLoop(cfg, target, prior, agent, vocab, progress_oracle, NoopHuman(), run_dir, device=device)
    report(f"starting {mode} optimization ({cfg.loop.n_rounds} rounds)", stage="loop")
    watcher.start()
    try:
        loop.run()
    finally:
        # The final round often lands between the last poll tick and loop
        # exit — capture it (and emit its event) before shutting down.
        watcher._poll_once()
        watcher.stop()
        watcher.join(timeout=10)

    final_state_path = run_dir / "final_state.json"
    summary: dict = {"mode": mode, "run_dir": str(run_dir),
                     "backend": getattr(oracle, "backend", "mock"),
                     "rounds_completed": watcher.last_round, "total_rounds": cfg.loop.n_rounds}
    rounds_stats = watcher._read_rounds()
    if rounds_stats:
        summary["rounds_log"] = [
            {k: s.get(k) for k in _RoundProgressWatcher._STATS_FIELDS if s.get(k) is not None}
            for s in rounds_stats
        ]
    if final_state_path.exists():
        summary["final_state"] = json.loads(final_state_path.read_text())
    candidates_path = run_dir / "candidates.csv"
    if candidates_path.exists():
        summary["candidates_csv"] = str(candidates_path)
        try:
            import pandas as pd

            frame = pd.read_csv(candidates_path)
            summary["n_candidates"] = int(len(frame))
        except Exception:
            summary["n_candidates"] = -1
    report(
        f"optimization complete: {watcher.last_round}/{cfg.loop.n_rounds} rounds",
        stage="done",
        rounds_completed=watcher.last_round,
        top_candidates=watcher._top_candidates(),
    )
    return summary
