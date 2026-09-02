"""Neighbour retrieval over the ChEMBL36 corpus (for focused priors).

Given a user-provided reference compound (a lead), we retrieve its nearest
ChEMBL neighbours by Tanimoto similarity so the generative prior can be
re-focused on that chemical space (the REINVENT "focused prior" recipe for
lead optimization on an arbitrary reference).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from rdkit import Chem, RDLogger
from rdkit.Chem import rdFingerprintGenerator

RDLogger.DisableLog("rdApp.*")

_FP = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
_POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint16)


def fp_packed(smiles: str) -> np.ndarray | None:
    m = Chem.MolFromSmiles(smiles)
    if m is None:
        return None
    bits = np.asarray(_FP.GetFingerprintAsNumPy(m), dtype=np.uint8)
    return np.packbits(bits)


def build_corpus_fingerprints(corpus_path: Path, cache_path: Path) -> tuple[np.ndarray, list[str]]:
    """Pack ECFP4 fingerprints for the whole corpus (cached)."""
    cache_path = Path(cache_path)
    if cache_path.exists():
        data = np.load(cache_path, allow_pickle=True)
        return data["fps"], data["smiles"].tolist()
    smiles_list = []
    rows = []
    for line in Path(corpus_path).read_text().splitlines():
        smi = line.split("\t")[0].split()[0] if line.strip() else ""
        if not smi:
            continue
        p = fp_packed(smi)
        if p is not None:
            rows.append(p)
            smiles_list.append(smi)
    fps = np.stack(rows)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache_path, fps=fps, smiles=np.array(smiles_list, dtype=object))
    return fps, smiles_list


def tanimoto_to_matrix(query_smiles: str, fps: np.ndarray) -> np.ndarray | None:
    q = fp_packed(query_smiles)
    if q is None:
        return None
    q_bits = int(_POP[q].sum())
    AND = np.zeros(len(fps), dtype=np.uint16)
    packed = fps
    for byte in range(q.shape[0]):
        qb = int(q[byte])
        if qb:
            col = _POP[packed[:, byte].astype(np.int16) & qb]
            AND += col
    row_bits = _POP[packed.astype(np.int16)].sum(axis=1)
    return AND / (q_bits + row_bits - AND)


def find_neighbors(query_smiles_list: list[str], corpus_path: Path, cache_path: Path,
                   topk: int = 30000, min_sim: float = 0.35) -> tuple[list[str], np.ndarray]:
    """ChEMBL neighbours of any reference compound, ranked by best similarity."""
    fps, smiles_list = build_corpus_fingerprints(corpus_path, cache_path)
    best = np.zeros(len(fps), dtype=np.float32)
    for q in query_smiles_list:
        sims = tanimoto_to_matrix(q, fps)
        if sims is None:
            continue
        best = np.maximum(best, sims)
    order = np.argsort(-best)
    keep = [i for i in order[:topk] if best[i] >= min_sim]
    return [smiles_list[i] for i in keep], best[keep]
