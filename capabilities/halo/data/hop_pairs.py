"""Mine scaffold-hop pairs from ChEMBL36 (training data for the hop translator).

A hop pair (A, B): high fingerprint similarity (same side-chain pattern)
but DIFFERENT Murcko scaffolds - the analogue of an mmpdb matched pair whose
transformation crosses the scaffold. Training the LLM on `A <hop> B` teaches
it, implicitly, WHICH environments tolerate WHICH core replacements: the
environment distribution lives in the weights.
"""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.Scaffolds import MurckoScaffold

RDLogger.DisableLog("rdApp.*")

_GEN = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
_POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint16)


def mine_hop_pairs(corpus_smi: list[str], out_path: Path, n_pairs: int = 400000,
                   sim_band=(0.33, 0.58), bucket_bytes: int = 3, seed: int = 0,
                   log=print) -> int:
    """Bucket molecules by fingerprint prefix; mine cross-scaffold near-duplicates."""
    rng = random.Random(seed)
    smis, scaffs, packed = [], [], []
    for s in corpus_smi:
        m = Chem.MolFromSmiles(s)
        if m is None or not (12 <= m.GetNumHeavyAtoms() <= 55):
            continue
        try:
            scaf = MurckoScaffold.MurckoScaffoldSmiles(mol=m, includeChirality=False)
        except Exception:
            continue
        if not scaf:
            continue
        arr = np.zeros((2048,), dtype=np.int8)
        DataStructs.ConvertToNumpyArray(_GEN.GetFingerprint(m), arr)
        packed.append(np.packbits(arr.astype(np.uint8)))
        smis.append(s)
        scaffs.append(scaf)
    log(f"[hop-pairs] {len(smis)} molecules scaffolded")
    bits = np.stack(packed)                                  # (N, 256) uint8
    row_counts = _POP[bits.astype(np.int32)].sum(axis=1)     # popcount per row

    buckets: dict[bytes, list[int]] = {}
    for i in range(len(bits)):
        buckets.setdefault(bytes(bits[i, :bucket_bytes]), []).append(i)
    big = [v for v in buckets.values() if len(v) > 1]
    log(f"[hop-pairs] {len(big)} non-trivial buckets ({sum(len(v) for v in big)} molecules)")

    pairs, seen_pairs = [], set()
    order = list(range(len(big)))
    rng.shuffle(order)
    for bi in order:
        if len(pairs) >= n_pairs:
            break
        bucket = big[bi]
        if len(bucket) > 500:
            bucket = rng.sample(bucket, 500)
        rng.shuffle(bucket)
        for x in range(len(bucket)):
            if len(pairs) >= n_pairs:
                break
            i = bucket[x]
            got = 0
            tried = 0
            for j in bucket[x + 1:]:
                if got >= 3 or tried >= 30:  # up to 3 partners, 30 attempts per anchor
                    break
                tried += 1
                if scaffs[i] == scaffs[j]:
                    continue
                a, b = (smis[i], smis[j]) if smis[i] < smis[j] else (smis[j], smis[i])
                if (a, b) in seen_pairs:
                    continue
                AND = _POP[(bits[i] & bits[j]).astype(np.int32)].sum()
                sim = AND / (row_counts[i] + row_counts[j] - AND)
                if sim_band[0] <= sim <= sim_band[1]:
                    seen_pairs.add((a, b))
                    pairs.append((a, b, round(float(sim), 3)))
                    got += 1
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for a, b, s in pairs:
            f.write(f"{a}\t{b}\t{s}\n")
    log(f"[hop-pairs] wrote {len(pairs)} hop pairs to {out_path}")
    return len(pairs)


def load_hop_pairs(path: Path, limit: int | None = None) -> list[tuple[str, str]]:
    out = []
    for line in Path(path).read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[0], parts[1]))
        if limit and len(out) >= limit:
            break
    return out
