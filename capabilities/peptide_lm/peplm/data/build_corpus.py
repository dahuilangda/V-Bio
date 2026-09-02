"""Tier-1 pretraining corpus.

Training-data design (each piece motivated by the SOTA survey):
  * FIM (PSM) transformation on 50% of lines — span infilling trained from
    the start (Bavarian 2207.14255 / ProtFIM / IgLM / IDiom): this is the LM
    analogue of ProteinMPNN's fixed-context redesign and powers the Tier-2
    edit operator.
  * Per-property conditioning tags <sol_x><syn_x><liab_x> (x = h/m/l),
    quantile-calibrated independently (ProGen control-tag style; the single
    a single monolithic tag carried almost no signal).
  * Explicit length-bucket token <Lnn> (IgLM-style chain tags) so length is
    a controllable conditioning variable.
  * Staged data (field consensus): UniRef90 chopped windows (bulk, 12M) +
    PDB binder peptides mined at scale (x6) + PeptideGPT property sets
    (experimental labels override the corresponding tag).
"""

from __future__ import annotations

import argparse
import math
import random
from collections import Counter
from pathlib import Path

from peplm.props.descriptors import compute_props, solubility_score, \
    synthesizability_score
from peplm.props.liability import liability_score
from peplm.vocab import parse_tokens

MIN_LEN, MAX_LEN = 8, 45
CLASS_KEEP = {"hi": 1.0, "md": 0.6, "lo": 0.25}
AA = set("ACDEFGHIKLMNPQRSTVWY")


# ---------------------------------------------------------------- helpers
def props_of(seq: str) -> tuple[float, float, float]:
    toks = list(seq)
    return (solubility_score(toks), synthesizability_score(toks),
            liability_score(toks))


def bucket_tag(length: int) -> str:
    return f"<L{min(max((length // 5) * 5, 5), 45)}>"


def tag_prefix(sol: float, syn: float, liab: float,
               cuts: dict) -> list[str]:
    def cls(v, lo, hi):
        return "h" if v >= hi else ("m" if v >= lo else "l")
    return [f"<sol_{cls(sol, cuts['sol'][0], cuts['sol'][1])}>",
            f"<syn_{cls(syn, cuts['syn'][0], cuts['syn'][1])}>",
            f"<liab_{cls(liab, cuts['liab'][0], cuts['liab'][1])}>"]


def fim_line(tokens: list[str], rng: random.Random) -> str:
    """PSM transform: <pre> P <suf> S <mid> M. Span width 3-12."""
    n = len(tokens)
    if n < 8:
        return "".join(tokens)
    w = rng.randint(3, min(12, n - 4))
    a = rng.randint(2, n - w - 2)
    b = a + w
    return ("".join(["<pre>"] + tokens[:a] + ["<suf>"] + tokens[b:]
                    + ["<mid>"] + tokens[a:b]))


def plain_line(tokens: list[str]) -> str:
    return "".join(tokens)


# ------------------------------------------------------------- worker pass
def _process_chunk(args):
    path, start, end, rate, seed, mode, cuts, fim_rate = args
    rng = random.Random(seed)

    def handle(seq: str, out: list, scores: list):
        if not (set(seq) <= AA):
            return
        if rng.random() > rate:
            return
        # windows
        n = len(seq)
        if n < MIN_LEN:
            return
        if n <= MAX_LEN:
            windows = [seq]
        else:
            windows = []
            for _ in range(2):
                L = rng.randint(MIN_LEN, MAX_LEN)
                s = rng.randint(0, n - L)
                windows.append(seq[s:s + L])
        for w in windows:
            if mode == "sample":
                sol, syn, liab = props_of(w)
                scores.append((sol, syn, liab))
                return
            sol, syn, liab = props_of(w)
            # keep-downsample on the solubility class
            cls = "hi" if sol >= cuts["sol"][1] else (
                "m" if sol >= cuts["sol"][0] else "l")
            if rng.random() > CLASS_KEEP[{"hi": "hi", "m": "md", "l": "lo"}[cls]]:
                return
            tags = tag_prefix(sol, syn, liab, cuts)
            toks = parse_tokens(w) if "[" not in w else list(w)
            body = (fim_line(toks, rng) if rng.random() < fim_rate
                    else plain_line(toks))
            out.append(" ".join(tags + [bucket_tag(len(w)), "<lin>"]) + " " + body)

    out: list[str] = []
    scores: list[tuple] = []
    buf: list[bytes] = []
    with open(path, "rb") as f:
        f.seek(start)
        if start > 0:
            f.readline()
        while f.tell() <= end:
            line = f.readline()
            if not line:
                break
            if line.startswith(b">"):
                if buf:
                    handle(b"".join(buf).decode("ascii", "ignore").upper(),
                           out, scores)
                buf = []
            else:
                buf.append(line.strip())
    if buf:
        handle(b"".join(buf).decode("ascii", "ignore").upper(), out, scores)
    return out if mode == "build" else scores


def _percentile_cuts(scores, lo_pct=25, hi_pct=55) -> dict:
    import numpy as np

    cuts = {}
    for i, k in enumerate(("sol", "syn", "liab")):
        arr = np.asarray([s[i] for s in scores])
        cuts[k] = (float(np.percentile(arr, lo_pct)),
                   float(np.percentile(arr, hi_pct)))
    return cuts


# ---------------------------------------------------------------- binder
def load_binder_lines(pdb_dir: Path, weight: int, cuts: dict,
                      rng: random.Random, fim_rate: float) -> list[str]:
    f = pdb_dir / "binder_peptides.txt"
    if not f.exists():
        return []
    lines = []
    for seq in f.read_text().splitlines():
        seq = seq.strip().upper()
        if not (5 <= len(seq) <= 35) or not (set(seq) <= AA):
            continue
        sol, syn, liab = props_of(seq)
        tags = tag_prefix(sol, syn, liab, cuts)
        toks = list(seq)
        for _ in range(weight):
            body = (fim_line(toks, rng) if rng.random() < fim_rate
                    else plain_line(toks))
            lines.append(" ".join(tags + [bucket_tag(len(seq)), "<lin>"])
                         + " " + body)
    return lines


def load_pepgpt_lines(pepgpt_dir: Path, cuts: dict, rng: random.Random,
                      fim_rate: float, max_each: int = 8000) -> list[str]:
    """Experimental labels override the corresponding property tag (PepGPT
    sets: soluble -> sol_h, non-fouling -> sol_h, hemolytic -> liab_l)."""
    lines = []

    def emit(seq: str, override: str):
        seq = "".join(c for c in seq.upper() if c in AA)
        if not (5 <= len(seq) <= 60):
            return
        sol, syn, liab = props_of(seq)
        tags = tag_prefix(sol, syn, liab, cuts)
        tagmap = dict(zip(("sol", "syn", "liab"), tags))
        if override == "sol_h":
            tagmap["sol"] = "<sol_h>"
        elif override == "liab_l":
            tagmap["liab"] = "<liab_l>"
        toks = list(seq[:45])
        body = (fim_line(toks, rng) if rng.random() < fim_rate
                else plain_line(toks))
        lines.append(" ".join([tagmap["sol"], tagmap["syn"], tagmap["liab"],
                               bucket_tag(len(toks)), "<lin>"]) + " " + body)

    def strip_tokens(s: str) -> str:
        return s.replace("<|endoftext|>", "").strip().strip(",")

    f = pepgpt_dir / "hemo_train.csv"
    if f.exists():
        rows = [strip_tokens(l) for l in f.read_text().splitlines()[1:] if l.strip()]
        for s in rows[:max_each]:
            emit(s, "liab_l")
    f = pepgpt_dir / "nf_train.csv"
    if f.exists():
        rows = [strip_tokens(l) for l in f.read_text().splitlines()[1:] if l.strip()]
        for s in rows[:max_each]:
            emit(s, "sol_h")
    f = pepgpt_dir / "sol_train.txt"
    if f.exists():
        rows = [strip_tokens(l) for l in f.read_text().splitlines() if l.strip()]
        for s in rows[:max_each]:
            emit(s, "sol_h")
    return lines


# ------------------------------------------------------------------- main
def main():
    import multiprocessing as mp

    ap = argparse.ArgumentParser()
    ap.add_argument("--uniref", default="/data/alphafold3/databases/uniref90_2022_05.fa")
    ap.add_argument("--n_segments", type=int, default=12_000_000)
    ap.add_argument("--out_dir", default="runs/data")
    ap.add_argument("--fim_rate", type=float, default=0.5)
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--calib_cache", default="runs/data/cuts.json")
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # pass 1: calibration sample
    cuts = None
    cache = Path(args.calib_cache)
    if cache.exists():
        import json

        cuts = {k: tuple(v) for k, v in json.loads(cache.read_text()).items()}
        print(f"[corpus] cuts from cache: {cuts}")
    else:
        size = Path(args.uniref).stat().st_size
        chunk = size // args.workers + 1
        ranges = [(i * chunk, min((i + 1) * chunk, size) - 1)
                  for i in range(args.workers) if i * chunk < size]
        tasks = [(args.uniref, s, e, 0.012, 4000 + i, "sample", None, 0.0)
                 for i, (s, e) in enumerate(ranges)]
        scores: list[tuple] = []
        with mp.Pool(args.workers) as pool:
            for part in pool.imap_unordered(_process_chunk, tasks):
                scores.extend(part)
        cuts = _percentile_cuts(scores)
        print(f"[corpus] calibrated per-property cuts (n={len(scores)}): {cuts}")
        import json

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({k: list(v) for k, v in cuts.items()}))

    # pass 2: build
    size = Path(args.uniref).stat().st_size
    chunk = size // args.workers + 1
    ranges = [(i * chunk, min((i + 1) * chunk, size) - 1)
              for i in range(args.workers) if i * chunk < size]
    est_entries = max(1e6, 14_000_000)
    rate = min(1.0, args.n_segments / (1.3 * est_entries))
    print(f"[corpus] build pass: rate {rate:.3f}, fim {args.fim_rate}")
    tasks = [(args.uniref, s, e, rate, 5000 + i, "build", cuts, args.fim_rate)
             for i, (s, e) in enumerate(ranges)]
    lines: list[str] = []
    with mp.Pool(args.workers) as pool:
        for part in pool.imap_unordered(_process_chunk, tasks):
            lines.extend(part)
    # 10% fully untagged CFG examples
    rng = random.Random(1)
    fixed = []
    for l in lines:
        if rng.random() < 0.10:
            parts = l.split()
            fixed.append(" ".join(["<lin>"] + parts[5:]))
        else:
            fixed.append(l)
    lines = fixed
    # binder + property corpora
    binder = load_binder_lines(Path(__file__).resolve().parents[2] / "runs" / "data_pdb", 6, cuts, rng, args.fim_rate)
    pepgpt = load_pepgpt_lines(Path(__file__).resolve().parents[2] / "runs" / "data_pepgpt", cuts, rng, args.fim_rate)
    print(f"[corpus] uniref {len(lines)}, binder {len(binder)}, pepgpt {len(pepgpt)}")
    lines = lines + binder + pepgpt
    rng.shuffle(lines)
    if len(lines) > args.n_segments + 200_000:
        lines = lines[: args.n_segments + 200_000]
    n_val = min(50_000, max(1000, len(lines) // 100))
    val, train = lines[:n_val], lines[n_val:]
    (out / "uniref_train.txt").write_text("\n".join(train))
    (out / "uniref_val.txt").write_text("\n".join(val))
    tags = Counter(l.split()[0] for l in lines)
    print(f"[corpus] -> {len(train)} train / {len(val)} val; first-tag mix "
          f"{dict(list(tags.items())[:4])}")


if __name__ == "__main__":
    main()
