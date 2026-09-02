"""SMILES tokenizer + vocabulary.

Two tokenizations:
  * atom-level regex (default, REINVENT-style)
  * fragment-level: frequent drug-like fragments (BRICS pieces + Murcko ring
    systems mined from the corpus) become single tokens; tokens are literal
    SMILES text so encode/decode are exact string segmentations.
"""

from __future__ import annotations

import re
from collections import Counter

PAD, BOS, EOS = "<pad>", "<bos>", "<eos>"

_TOKEN_RE = re.compile(
    r"(\[[^\]]+\]|Br|Cl|Si|Se|As|B|C|N|O|P|S|F|I|"
    r"b|c|n|o|p|s|"
    r"%\d{2}|\d|=|#|\+|-|\.|/|\\|\(|\)|:|\*|~)"
)


def _atom_regex() -> str:
    return (
        r"\[[^\]]+\]|Br|Cl|Si|Se|As|B|C|N|O|P|S|F|I|"
        r"b|c|n|o|p|s|"
        r"%\d{2}|\d|=|#|\+|-|\.|/|\\|\(|\)|:|\*|~"
    )


def build_fragment_regex(fragments: list[str]) -> re.Pattern:
    """Longest-match-first regex over fragment literals + atoms."""
    parts = sorted({re.escape(f) for f in fragments if len(f) >= 3}, key=len, reverse=True)
    pattern = "(" + "|".join(parts) + "|" + _atom_regex() + ")"
    return re.compile(pattern)


def mine_fragments(smiles_iterable, max_molecules: int = 8000, min_count: int = 100,
                   max_heavy: int = 18) -> list[str]:
    """Mine frequent BRICS pieces + Murcko ring systems from a corpus sample."""
    from rdkit import Chem, RDLogger
    from rdkit.Chem import BRICS
    from rdkit.Chem.Scaffolds import MurckoScaffold

    RDLogger.DisableLog("rdApp.info")
    counter: Counter[str] = Counter()
    seen = 0
    for smi in smiles_iterable:
        seen += 1
        if seen > max_molecules:
            break
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        # ring systems: connected clusters of ring atoms (the drug-design building blocks)
        try:
            ring_atoms = {a.GetIdx() for a in mol.GetAtoms() if a.IsInRing()}
            seen_sys: set[int] = set()
            for idx in ring_atoms:
                if idx in seen_sys:
                    continue
                # BFS over ring bonds
                sys_atoms, stack = set(), [idx]
                while stack:
                    cur = stack.pop()
                    if cur in sys_atoms:
                        continue
                    sys_atoms.add(cur)
                    for nbr in mol.GetAtomWithIdx(cur).GetNeighbors():
                        if nbr.GetIdx() in ring_atoms and mol.GetBondBetweenAtoms(cur, nbr.GetIdx()).IsInRing():
                            stack.append(nbr.GetIdx())
                seen_sys |= sys_atoms
                if not (3 <= len(sys_atoms) <= max_heavy):
                    continue
                piece = Chem.RWMol(mol)
                for rm in sorted(set(range(mol.GetNumAtoms())) - sys_atoms, reverse=True):
                    piece.RemoveAtom(rm)
                piece = piece.GetMol()
                try:
                    Chem.SanitizeMol(piece)
                    counter[Chem.MolToSmiles(piece)] += 1
                except Exception:
                    pass
        except Exception:
            pass
        # whole Murcko framework
        try:
            framework = MurckoScaffold.MurckoScaffoldSmiles(mol=mol, includeChirality=False)
            if framework:
                counter[framework] += 1
        except Exception:
            pass
        # BRICS pieces: strip dummy attachment atoms, keep the largest piece
        try:
            for piece in BRICS.BRICSDecompose(mol, keepNonLeafNodes=False, minFragmentSize=4):
                pm = Chem.MolFromSmiles(piece)
                if pm is None:
                    continue
                if any(a.GetAtomicNum() == 0 for a in pm.GetAtoms()):
                    rw = Chem.RWMol(pm)
                    for idx in sorted((a.GetIdx() for a in pm.GetAtoms() if a.GetAtomicNum() == 0), reverse=True):
                        rw.RemoveAtom(idx)
                    try:
                        pm = rw.GetMol()
                        Chem.SanitizeMol(pm)
                    except Exception:
                        continue
                    frs = Chem.GetMolFrags(pm, asMols=True)
                    if not frs:
                        continue
                    pm = max(frs, key=lambda x: x.GetNumAtoms())
                if pm.GetNumHeavyAtoms() <= max_heavy:
                    counter[Chem.MolToSmiles(pm)] += 1
        except Exception:
            continue
    frags = []
    for frag, count in counter.most_common(4000):
        if count < min_count:
            break
        if not (4 <= len(frag) <= 40):
            continue
        m = Chem.MolFromSmiles(frag)
        if m is None:
            continue
        frags.append(frag)
    return frags


def tokenize(smiles: str, pattern: re.Pattern | None = None) -> list[str]:
    p = pattern or _TOKEN_RE
    return p.findall(smiles)


def detokenize(tokens: list[str]) -> str:
    out = "".join(t for t in tokens if t not in (PAD, BOS, EOS))
    # collapse ring-closure splits like "1" "1" are fine; nothing to fix here
    return out


class SmilesVocab:
    def __init__(self, smiles_iterable=None, fragment_pattern: re.Pattern | None = None,
                 fragments: list[str] | None = None):
        self.fragment_pattern = fragment_pattern
        self.fragments = fragments or []
        self.itos: list[str] = [PAD, BOS, EOS]
        self.stoi: dict[str, int] = {t: i for i, t in enumerate(self.itos)}
        if smiles_iterable is not None:
            for smi in smiles_iterable:
                self.add_smiles(smi)

    def add_smiles(self, smiles: str) -> None:
        for t in tokenize(smiles, self.fragment_pattern):
            if t not in self.stoi:
                self.stoi[t] = len(self.itos)
                self.itos.append(t)

    def encode(self, smiles: str) -> list[int]:
        return [self.stoi[BOS]] + [self.stoi.get(t, self.stoi[EOS]) for t in tokenize(smiles, self.fragment_pattern)] + [self.stoi[EOS]]

    def decode(self, ids: list[int]) -> str:
        toks = []
        for i in ids:
            t = self.itos[int(i)]
            if t == EOS:
                break
            if t in (PAD, BOS) or (t.startswith("<") and t.endswith(">")):
                continue
            toks.append(t)
        return detokenize(toks)

    def n_tokens_of(self, smiles: str) -> int:
        return len(tokenize(smiles, self.fragment_pattern))

    def __len__(self) -> int:
        return len(self.itos)

    # ---- persistence ------------------------------------------------------
    def save(self, path):
        import json
        from pathlib import Path

        Path(path).write_text(json.dumps({"itos": self.itos, "fragments": self.fragments}))

    @classmethod
    def load(cls, path) -> "SmilesVocab":
        import json
        from pathlib import Path

        data = json.loads(Path(path).read_text())
        if isinstance(data, list):  # legacy atom-level format
            v = cls()
            v.itos = data
        else:
            v = cls()
            v.itos = data["itos"]
            v.fragments = data.get("fragments") or []
            v.fragment_pattern = build_fragment_regex(v.fragments) if v.fragments else None
        v.stoi = {t: i for i, t in enumerate(v.itos)}
        return v
