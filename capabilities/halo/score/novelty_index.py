"""ChEMBL-backed novelty lookup for reward shaping and pool filtering.

Exact-copy membership over the full corpus plus max-Tanimoto against a
diverse reference subset, fast enough for per-round use on the whole pool.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np


class NoveltyIndex:
    def __init__(self, npz_path, smiles_path=None, n_reference: int = 100000,
                 n_threads: int = 8):
        from rdkit import DataStructs

        self.n_threads = n_threads
        z = np.load(str(npz_path), allow_pickle=True)
        if "fps" in z.files:
            fps_arr = z["fps"]
            smi_arr = z["smiles"] if "smiles" in z.files else None
        else:
            k0 = z.files[0]
            fps_arr = z[k0]
            smi_arr = None
        n_bits = fps_arr.shape[1] * 8 if fps_arr.dtype == np.uint8 else fps_arr.shape[1]
        # exact-copy set from the full corpus (cheap hash membership)
        self._known_smiles = set()
        if smi_arr is not None:
            self._known_smiles = {s for s in smi_arr.tolist() if isinstance(s, str)}
        elif smiles_path and Path(smiles_path).exists():
            for ln in Path(smiles_path).read_text().splitlines():
                parts = ln.replace("\t", " ").split()
                if parts:
                    self._known_smiles.add(parts[0])
        # reference subset for Tanimoto: exact copies are caught by the full
        # hash set above; the subset catches near-copies
        rng = np.random.default_rng(0)
        n = fps_arr.shape[0]
        idx = rng.choice(n, size=min(n_reference, n), replace=False)
        from rdkit.DataStructs import cDataStructs

        self.ref_fps = []
        for i in idx:
            row = fps_arr[i]
            bits = np.unpackbits(row) if row.dtype == np.uint8 else row.astype(bool)
            bv = cDataStructs.ExplicitBitVect(int(n_bits))
            bv.SetBitsFromList(np.nonzero(bits)[0].tolist())
            self.ref_fps.append(bv)
        self.n_bits = int(n_bits)
        self.DataStructs = DataStructs

    def is_known(self, smiles: str) -> bool:
        return smiles in self._known_smiles

    def max_tanimoto(self, smiles_or_fp) -> float:
        from rdkit import Chem
        from rdkit.Chem import rdFingerprintGenerator

        if isinstance(smiles_or_fp, str):
            m = Chem.MolFromSmiles(smiles_or_fp)
            if m is None:
                return 1.0  # unparseable -> treat as "known" (no novelty credit)
            gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=self.n_bits)
            fp = gen.GetFingerprint(m)
        else:
            fp = smiles_or_fp
        if not self.ref_fps:
            return 0.0
        return float(max(self.DataStructs.BulkTanimotoSimilarity(fp, self.ref_fps)))

    def max_tanimoto_batch(self, smiles_list, chunk: int = 64) -> list[float]:
        """Threaded bulk query (rdkit releases the GIL); ~4s for 400 queries."""
        from rdkit import Chem
        from rdkit.Chem import rdFingerprintGenerator

        gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=self.n_bits)
        fps = []
        for s in smiles_list:
            m = Chem.MolFromSmiles(s)
            fps.append(gen.GetFingerprint(m) if m is not None else None)

        def work(fp):
            if fp is None:
                return 1.0
            return float(max(self.DataStructs.BulkTanimotoSimilarity(fp, self.ref_fps)))

        with ThreadPoolExecutor(max_workers=self.n_threads) as ex:
            return list(ex.map(work, fps))

    def novelty(self, smiles: str) -> float:
        """1 - max_Tc, 0.0 for exact known copies."""
        if self.is_known(smiles):
            return 0.0
        return 1.0 - self.max_tanimoto(smiles)


def load_default(run_root: Path | None = None):
    import os
    from halo import RUNS_DIR
    run_root = run_root or Path(os.environ.get("HALO_RUNS_DIR", str(RUNS_DIR)))
    npz = run_root / "chembl36_corpus.fp2048.npz"
    smi = run_root / "chembl36_safe.smi"
    if not npz.exists():
        return None
    try:
        return NoveltyIndex(npz, smi)
    except Exception:
        return None
