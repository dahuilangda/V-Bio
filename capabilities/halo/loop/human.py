"""Human-in-the-loop feedback interfaces.

Three interchangeable backends:
  * CLIHuman      - interactive terminal review (draws 2D depictions to PNG)
  * FileHuman     - reads structured feedback from a JSON file (asynchronous,
                    expert-friendly: pairwise prefs + rule edits)
  * SimulatedChemist - rule-based synthetic chemist for closed-loop
                    benchmarking; optional access to ground-truth activity
                    (measures how much human expertise accelerates the loop).

Feedback schema (list of records):
  {"type": "prefer", "better": <smiles>, "worse": <smiles>}
  {"type": "accept"|"reject", "smiles": <smiles>, "reason": str}
  {"type": "rule", "key": "mw_max", "value": 480}
  {"type": "weight", "key": "affinity", "value": 4.0}
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from halo.score.properties import compute_descriptors, descriptor_vector


class FeedbackBatch:
    def __init__(self):
        self.pairs: list[tuple[str, str]] = []  # (better, worse)
        self.accepted: list[str] = []
        self.rejected: list[str] = []
        self.rules: dict[str, float] = {}
        self.weights: dict[str, float] = {}

    def to_records(self) -> list[dict]:
        recs = [{"type": "prefer", "better": a, "worse": b} for a, b in self.pairs]
        recs += [{"type": "accept", "smiles": s} for s in self.accepted]
        recs += [{"type": "reject", "smiles": s} for s in self.rejected]
        recs += [{"type": "rule", "key": k, "value": v} for k, v in self.rules.items()]
        recs += [{"type": "weight", "key": k, "value": v} for k, v in self.weights.items()]
        return recs

    @classmethod
    def from_records(cls, records: list[dict]) -> "FeedbackBatch":
        fb = cls()
        for r in records:
            t = r.get("type")
            if t == "prefer":
                fb.pairs.append((r["better"], r["worse"]))
            elif t == "accept":
                fb.accepted.append(r["smiles"])
            elif t == "reject":
                fb.rejected.append(r["smiles"])
            elif t == "rule":
                fb.rules[r["key"]] = r["value"]
            elif t == "weight":
                fb.weights[r["key"]] = r["value"]
        return fb

    def __bool__(self):
        return bool(self.pairs or self.accepted or self.rejected or self.rules or self.weights)


class HumanInterface:
    name = "base"

    def review(self, candidates: list[dict], context: dict) -> FeedbackBatch:
        raise NotImplementedError


class NoopHuman(HumanInterface):
    name = "none"

    def review(self, candidates, context):
        return FeedbackBatch()


class CLIHuman(HumanInterface):
    """Interactive terminal review; non-TTY falls back to no-op with a warning."""

    name = "cli"

    def __init__(self, depict_dir: Path | None = None):
        self.depict_dir = Path(depict_dir) if depict_dir else None

    def review(self, candidates, context):
        fb = FeedbackBatch()
        if not candidates:
            return fb
        print("\n=== HALO human review (round %s) ===" % context.get("round", "?"))
        print("Commands: p <i> > <j> (prefer i over j) | a <i> accept | r <i> reject | w <key> <val> | q finish")
        for i, c in enumerate(candidates):
            d = c.get("desc", {})
            print(
                f"[{i:2d}] {c['smiles'][:80]:<80} aff {c.get('affinity_pic50', float('nan')):.2f} "
                f"ipSAE {c.get('ipsae', float('nan')):.2f} R {c.get('reward', float('nan')):.3f} "
                f"MW {d.get('mw', 0):.0f} SAS {d.get('sas', 0):.1f}"
            )
        if self.depict_dir:
            try:
                from rdkit import Chem
                from rdkit.Chem import Draw

                self.depict_dir.mkdir(parents=True, exist_ok=True)
                mols = [Chem.MolFromSmiles(c["smiles"]) for c in candidates]
                mols = [m for m in mols if m]
                if mols:
                    Draw.MolsToGridImage(mols, molsPerRow=4, subImgSize=(400, 300),
                                         legends=[f"{i}" for i in range(len(mols))]
                                         ).save(str(self.depict_dir / "review.png"))
                    print(f"grid depiction: {self.depict_dir / 'review.png'}")
            except Exception as e:  # pragma: no cover
                print("depiction failed:", e)
        try:
            while True:
                line = input("> ").strip()
                if line in ("q", ""):
                    break
                try:
                    if line.startswith("p "):
                        a, b = line[2:].split(">")
                        i, j = int(a.strip()), int(b.strip())
                        fb.pairs.append((candidates[i]["smiles"], candidates[j]["smiles"]))
                    elif line.startswith("a "):
                        fb.accepted.append(candidates[int(line[2:])]["smiles"])
                    elif line.startswith("r "):
                        fb.rejected.append(candidates[int(line[2:])]["smiles"])
                    elif line.startswith("w "):
                        k, v = line[2:].split()
                        fb.weights[k] = float(v)
                except Exception:
                    print("unrecognized - use: p 3 > 5 | a 3 | r 3 | w affinity 4.0 | q")
        except EOFError:
            pass
        return fb


class FileHuman(HumanInterface):
    """Asynchronous expert feedback: the run pauses and waits for feedback.json."""

    name = "file"

    def __init__(self, feedback_path: Path, poll_interval_s: float = 5.0, prompt_file: bool = True):
        self.path = Path(feedback_path)
        self.poll_interval_s = poll_interval_s
        self.prompt_file = prompt_file

    def review(self, candidates, context):
        fb = FeedbackBatch()
        if not candidates:
            return fb
        prompt = self.path.with_suffix(".prompt.json")
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(json.dumps(
            {"round": context.get("round"), "candidates": candidates}, indent=1, default=str))
        self.path.unlink(missing_ok=True)
        print(f"[human] wrote {prompt}; waiting for {self.path} ...")
        while not self.path.exists():
            import time

            time.sleep(self.poll_interval_s)
        records = json.loads(self.path.read_text())
        if isinstance(records, dict):
            records = records.get("feedback", [])
        return FeedbackBatch.from_records(records)


class SimulatedChemist(HumanInterface):
    """Synthetic expert for benchmarking the HITL loop.

    Two knowledge components (weight configurable):
      * structural priors ("med-chem taste"): penalises high MW / cLogP /
        flatness, rewards moderate polar surface and sp3 fraction;
      * ground-truth oracle: uses measured activity of nearest known ligand
        (noisy) - models a chemist with deep SAR knowledge of the series.
    Emits pairwise preferences among reviewed candidates + occasional rules.
    """

    name = "sim"

    def __init__(self, ground_truth_smiles_to_pic50: dict[str, float] | None = None,
                 noise_sigma: float = 0.3, gt_weight: float = 0.5, seed: int = 0,
                 review_topk: int = 12):
        self.gt = ground_truth_smiles_to_pic50 or {}
        self.noise_sigma = noise_sigma
        self.gt_weight = gt_weight
        self.rng = random.Random(seed)
        self.review_topk = review_topk

    def _taste_score(self, smiles: str) -> float:
        from rdkit import Chem, DataStructs
        from rdkit.Chem import rdFingerprintGenerator

        m = Chem.MolFromSmiles(smiles)
        if m is None:
            return -1.0
        d = compute_descriptors(m)
        s = 0.0
        s += max(0.0, (d["mw"] - 420) / 200) * -1.0      # dislikes heavy
        s += max(0.0, (d["clogp"] - 4.0) / 3.0) * -1.0   # dislikes greasy
        s += max(0.0, (2.5 - d["tpsa"]) / 60) * -0.5     # dislikes very low TPSA
        s += (d["frac_csp3"] - 0.25) * 0.6               # likes some 3D shape
        s += max(0.0, (d["sas"] - 5.0) / 4.0) * -1.0     # dislikes hard synthesis
        return s

    def _gt_score(self, smiles: str) -> float | None:
        if not self.gt:
            return None
        from rdkit import Chem, DataStructs
        from rdkit.Chem import rdFingerprintGenerator

        m = Chem.MolFromSmiles(smiles)
        if m is None:
            return None
        gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
        fp = gen.GetFingerprint(m)
        best_sim, best = 0.0, None
        for s, pic50 in self.gt.items():
            mm = Chem.MolFromSmiles(s)
            if mm is None:
                continue
            sim = DataStructs.TanimotoSimilarity(fp, gen.GetFingerprint(mm))
            if sim > best_sim:
                best_sim, best = sim, pic50
        if best is None or best_sim < 0.35:
            return None
        val = best + self.rng.gauss(0, self.noise_sigma)
        return val * min(1.0, best_sim / 0.6)

    def review(self, candidates, context):
        fb = FeedbackBatch()
        cands = [c for c in candidates if c.get("smiles")]
        if not cands:
            return fb
        scored = []
        for c in cands:
            taste = self._taste_score(c["smiles"])
            gt = self._gt_score(c["smiles"])
            if gt is None:
                total = taste
            else:
                total = (1 - self.gt_weight) * taste + self.gt_weight * (gt / 10.0)
            scored.append((total + self.rng.gauss(0, 0.05), c["smiles"]))
        scored.sort(reverse=True)
        # pairwise feedback: top third vs bottom third of reviewed batch
        k = max(2, len(scored) // 3)
        top, bottom = scored[:k], scored[-k:]
        for (_, smi_a), (_, smi_b) in zip(top, reversed(bottom)):
            fb.pairs.append((smi_a, smi_b))
        for _, s in scored[: max(1, k // 3)]:
            fb.accepted.append(s)
        for _, s in scored[-max(1, k // 4):]:
            fb.rejected.append(s)
        # occasional hard rule from the simulated chemist
        if self.rng.random() < 0.4:
            fb.rules["mw_max"] = float(self.rng.choice([440, 480, 500]))
        return fb
