"""Scaffold-based diversity control (REINVENT-style scaffold memory)."""

from __future__ import annotations

from collections import defaultdict

from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold


class ScaffoldMemory:
    def __init__(self, limit_per_scaffold: int = 3):
        self.limit = limit_per_scaffold
        self.counts: dict[str, int] = defaultdict(int)
        self.seen_smiles: set[str] = set()

    def scaffold(self, smiles: str) -> str:
        m = Chem.MolFromSmiles(smiles)
        if m is None:
            return ""
        try:
            return MurckoScaffold.MurckoScaffoldSmiles(mol=m, includeChirality=False)
        except Exception:
            return ""

    def register(self, smiles_list: list[str]) -> None:
        for s in smiles_list:
            self.counts[self.scaffold(s)] += 1
            self.seen_smiles.add(Chem.MolToSmiles(Chem.MolFromSmiles(s)) if Chem.MolFromSmiles(s) else s)

    def is_duplicate(self, smiles: str) -> bool:
        m = Chem.MolFromSmiles(smiles)
        return m is None or Chem.MolToSmiles(m) in self.seen_smiles

    def scaffold_full(self, smiles: str) -> bool:
        return self.counts[self.scaffold(smiles)] >= self.limit

    def filter(self, smiles_list: list[str]) -> list[str]:
        out = []
        for s in smiles_list:
            if self.is_duplicate(s) or self.scaffold_full(s):
                continue
            out.append(s)
        return out
