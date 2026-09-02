"""Learned property heads (Vilya-style, trained on experimental labels).

Small GradientBoosting classifiers over sequence-composition features trained
on the PeptideGPT experimental sets (soluble / non-fouling / non-hemolytic).
Used as reward terms: P(predictive soluble), P(non-fouling), P(non-hemolytic).
sklearn GradientBoosting (no GPU needed, ~1 min on 10k rows) keeps with the
field pattern of calibrated, data-driven developability signals on top of
structure confidence (RL-PLM: reward fidelity determines RL gains).
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np

from peplm.residues import NATURAL_AA, NCAA_TOKENS
from peplm.vocab import parse_tokens

_MODEL_PATH = Path(__file__).resolve().parents[2] / "runs" / "pepgpt_props.pkl"
_ORDER = list(NATURAL_AA) + NCAA_TOKENS + ["L"]


def _features(tokens: list[str]) -> np.ndarray:
    comp = np.zeros(len(_ORDER))
    for t in tokens:
        k = t if t in _ORDER else ("[{}]".format(t) if t.startswith("[") else t)
        if k in _ORDER:
            comp[_ORDER.index(k)] += 1
    n = max(len(tokens), 1)
    comp /= n
    length = min(len(tokens), 40) / 40.0
    ncaa = sum(1 for t in tokens if t.startswith("[")) / max(n, 1)
    return np.concatenate([comp, [length, ncaa]])


def _features_from_props(p: dict) -> np.ndarray:
    toks = [_ORDER[0]] * max(int(p.get("length", 12)), 1)
    comp = np.zeros(len(_ORDER))
    # approximate composition from the descriptor dict is not available; use
    # the stored token composition when present, else neutral features
    if "token_comp" in p:
        comp = np.asarray(p["token_comp"], dtype=float)
    n = max(int(p.get("length", 12)), 1)
    if comp.sum() > 0:
        comp = comp / comp.sum()
    else:
        comp[0] = 1.0
    return np.concatenate([comp, [n / 40.0, p.get("ncaa_count", 0) / max(n, 1)]])


def train_heads(out_path: str | None = None):
    import joblib
    from sklearn.ensemble import GradientBoostingClassifier

    root = Path(__file__).resolve().parents[2]
    sets: dict[str, list[tuple[np.ndarray, int]]] = {}

    def add(name, rows, pos):
        for s in rows:
            s = s.replace("<|endoftext|>", "").strip().strip(",")
            s = "".join(c for c in s.upper() if c in "ACDEFGHIKLMNPQRSTVWY")
            if 5 <= len(s) <= 60:
                try:
                    toks = parse_tokens(s)
                except ValueError:
                    continue
                sets.setdefault(name, []).append((_features(toks), pos))

    hemo = (root / "runs/data_pepgpt/hemo_train.csv").read_text().splitlines()[1:]
    nf = (root / "runs/data_pepgpt/nf_train.csv").read_text().splitlines()[1:]
    sol = (root / "runs/data_pepgpt/sol_train.txt").read_text().splitlines()
    hets = [l.split(",")[0] for l in hemo if l.strip()]
    nfs = [l.split(",")[0] for l in nf if l.strip()]
    sols = [l.split(",")[0] for l in sol if l.strip()]
    # PeptideGPT sets are single-class; build binary heads with the other
    # sets as the (antagonistic) negative class (Peptide-GPT practice)
    add("nonhemo", sols + nfs, 1)
    add("nonhemo", hets, 0)
    add("nonfouling", nfs, 1)
    add("nonfouling", hets, 0)
    add("soluble", sols, 1)
    add("soluble", hets, 0)

    rng = random.Random(0)
    models = {}
    for name, data in sets.items():
        if len(data) < 40:
            continue
        rng.shuffle(data)
        X = np.stack([x for x, _ in data])
        y = np.asarray([v for _, v in data])
        m = GradientBoostingClassifier(n_estimators=150, max_depth=3,
                                       learning_rate=0.08, random_state=0)
        m.fit(X, y)
        models[name] = m
        acc = m.score(X, y)
        print(f"[props-head] {name}: n={len(data)} pos={int(y.sum())} "
              f"train-acc={acc:.3f}", file=sys.stderr)
    out = out_path or str(_MODEL_PATH)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(models, out)
    return models


class LearnedPropertyHeads:
    """Loads once; predict(tokens) -> {soluble, nonfouling, nonhemo} in [0,1]
    (probabilities), None if unavailable."""

    def __init__(self, path: str | None = None):
        import joblib

        p = Path(path) if path else _MODEL_PATH
        self.models = {}
        if p.exists():
            try:
                self.models = joblib.load(p)
            except Exception:
                self.models = {}
        self.order = _ORDER

    def __call__(self, tokens: list[str]) -> dict | None:
        if not self.models:
            return None
        x = _features(tokens).reshape(1, -1)
        out = {}
        for name, m in self.models.items():
            prob = float(m.predict_proba(x)[0][1]) if hasattr(m, "predict_proba") \
                else float(m.predict(x)[0])
            out[name] = prob
        return out


def _load_learned_heads():
    try:
        return LearnedPropertyHeads()
    except Exception:
        return None