"""Multitask SAFE dataset construction.

Item formats (round-trip verified: decode(prompt-span + generation) is the
source molecule):
  unconditional:  <bos> SAFE(A) <eos>
  decoration:     <bos> scaffold-prefix <cont> . decorations <eos>
  hop:            <bos> env_frags <hop> . core_frags <eos>
"""

from __future__ import annotations

import random
from collections import Counter

from rdkit import Chem, RDLogger
from rdkit.Chem.Scaffolds import MurckoScaffold

from halo.generate.safe_prior import CORE, CONT, HOP

RDLogger.DisableLog("rdApp.*")
try:  # safe's loguru stereo warnings flood build logs
    from loguru import logger as _loguru

    _loguru.disable("safe")
except Exception:
    pass


def safe_encode_robust(smiles: str, canonical: bool = True) -> str | None:
    """safe.encode that returns None instead of raising (some molecules have
    no BRICS-cutable bond -> SAFEFragmentationError)."""
    import safe as safe_lib

    try:
        return safe_lib.encode(smiles, canonical=canonical)
    except Exception:
        return None

_MIN_CORE_HEAVY = 4      # a "core" fragment smaller than this stays env
_MIN_FRAGS = 3           # molecules with <3 fragments are unusable for edits


def _decode_frag(frag: str):
    import safe as safe_lib

    try:
        return Chem.MolFromSmiles(safe_lib.decode(frag, ignore_errors=True) or "")
    except Exception:
        return None


class FragmentClassifier:
    """Cache-heavy fragment -> {scaffold, decoration} classifier."""

    def __init__(self):
        self._mol_cache: dict[str, Chem.Mol | None] = {}

    def frag_mol(self, frag: str):
        if frag not in self._mol_cache:
            self._mol_cache[frag] = _decode_frag(frag)
        return self._mol_cache[frag]

    def classify(self, smiles: str, safe_str: str) -> tuple[list[int], list[int], str] | None:
        """Return (core_idx, env_idx, scaffold_smiles) or None if unusable.

        Core fragments = Murcko-scaffold fragments identified by substructure
        match of the decoded fragment inside the Murcko scaffold (SAFE digits
        differ from raw SMILES, so string matching would be wrong).
        """
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        try:
            scaf = MurckoScaffold.GetScaffoldForMol(mol)
            scaf_smiles = Chem.MolToSmiles(scaf)
        except Exception:
            return None
        if not scaf_smiles or scaf.GetNumAtoms() == 0:
            return None
        core, env = [], []
        for i, frag in enumerate(safe_str.split(".")):
            fm = self.frag_mol(frag)
            if (
                fm is not None
                and fm.GetNumHeavyAtoms() >= _MIN_CORE_HEAVY
                and scaf.HasSubstructMatch(fm)
            ):
                core.append(i)
            else:
                env.append(i)
        if not core or not env:
            return None
        return core, env, scaf_smiles


def reorder(core_idx: list[int], env_idx: list[int], frags: list[str],
            first: str) -> str:
    """Join fragments; `first` in {'core','env'} picks the leading block."""
    a = [frags[i] for i in (core_idx if first == "core" else env_idx)]
    b = [frags[i] for i in (env_idx if first == "core" else core_idx)]
    return ".".join(a + b)


def roundtrip_ok(safe_lib, safe_str: str, smiles: str) -> bool:
    try:
        dec = safe_lib.decode(safe_str, ignore_errors=True)
    except Exception:
        return False
    if not dec:
        return False
    m = Chem.MolFromSmiles(dec)
    ref = Chem.MolFromSmiles(smiles)
    return m is not None and ref is not None and Chem.MolToSmiles(m) == Chem.MolToSmiles(ref)


import re

_PCT = re.compile(r"%(\d{2,3})")
_DIGIT = re.compile(r"(?<=%\d{2})|(\d)")


def digit_parity_ok(safe_str: str) -> bool:
    """Every ring-closure label must appear an even number of times; digits
    inside bracket atoms (isotopes like [3H]) are not ring closures."""
    import re as _re

    toks = [t for t in _re.findall(r"\[[^\]]*\]|%\d{2,3}|.", safe_str)
            if (len(t) == 1 and t.isdigit()) or (t.startswith("%") and t[1:].isdigit())]
    counts: dict[str, int] = {}
    for t in toks:
        counts[t] = counts.get(t, 0) + 1
    return all(v % 2 == 0 for v in counts.values())


def build_t2_items(vocab, corpus_pairs, safe_encode_fn, *, max_len: int = 256,
                   frac: float = 0.25, seed: int = 0, log=print):
    """T2 scaffold-decoration items from (smiles, safe) corpus pairs.

    Prefix = scaffold fragments (+ optionally the first decoration fragment so
    the model also learns decoration-to-decoration continuation), target =
    remaining decoration fragments. Both come from the same SAFE string.
    Digit-parity gate on every item; full decode on a 2% sample.
    """
    import safe as safe_lib

    rng = random.Random(seed)
    clf = FragmentClassifier()
    items, stats = [], Counter()
    decode_checked = decode_ok = 0
    for smi, s in corpus_pairs[: int(len(corpus_pairs) * frac)]:
        frags = s.split(".")
        if len(frags) < _MIN_FRAGS:
            stats["too_few_frags"] += 1
            continue
        cls = clf.classify(smi, s)
        if cls is None:
            stats["no_core_or_env"] += 1
            continue
        core_idx, env_idx, _ = cls
        # scaffold first, decorations follow in original order
        ordered = reorder(core_idx, env_idx, frags, first="core")
        if not digit_parity_ok(ordered):
            stats["reorder_parity_fail"] += 1
            continue
        if rng.random() < 0.02:
            decode_checked += 1
            decode_ok += roundtrip_ok(safe_lib, ordered, smi)
        o_frags = ordered.split(".")
        # cut inside the decoration block only: prefix always contains ALL
        # core fragments (scaffold conservation) + k decorations
        n_core = len(core_idx)
        if len(o_frags) - n_core < 1:
            stats["no_decorations"] += 1
            continue
        k = n_core + rng.randrange(0, max(1, len(o_frags) - n_core))
        k = max(1, min(k, len(o_frags) - 1))
        prefix, target = ".".join(o_frags[:k]), ".".join(o_frags[k:])
        # the target's leading '.' is INSIDE the loss span so prompt and
        # generation concatenate to a well-formed SAFE string at inference
        ids = vocab.encode_text(prefix + CONT + "." + target)
        n_pre = len(vocab.tok.encode(prefix + CONT).ids) + 1  # +1 for <bos>
        if len(ids) <= max_len and 1 < n_pre < len(ids) - 1:
            items.append((ids, n_pre))
            stats["ok"] += 1
    log(f"[t2] {dict(stats)} decode-sample {decode_ok}/{decode_checked}")
    return items


def build_t3_items(vocab, hop_pairs, safe_encode_fn, *, max_len: int = 256,
                   upweight: int = 3, seed: int = 0, log=print):
    """T3 environment->core items from (a_smiles, b_smiles) hop pairs.

    Item: env_frags(B) <hop> core_frags(B); env keeps B's original fragment
    order minus core fragments, target = core fragments (B's order).
    Digit-parity gate on every item; full decode on a 5% sample.
    """
    import safe as safe_lib

    rng = random.Random(seed)
    clf = FragmentClassifier()
    items, stats = [], Counter()
    decode_checked = decode_ok = 0
    for a, b in hop_pairs:
        try:
            sb = safe_encode_fn(b)
        except Exception:
            sb = None
        if not sb:
            stats["encode_fail"] += 1
            continue
        frags = sb.split(".")
        if len(frags) < _MIN_FRAGS:
            stats["too_few_frags"] += 1
            continue
        cls = clf.classify(b, sb)
        if cls is None:
            stats["no_core_or_env"] += 1
            continue
        core_idx, env_idx, _ = cls
        env = ".".join(frags[i] for i in env_idx)
        core = ".".join(frags[i] for i in core_idx)
        if not digit_parity_ok(env + "." + core):
            stats["envcore_parity_fail"] += 1
            continue
        if rng.random() < 0.05:
            decode_checked += 1
            decode_ok += roundtrip_ok(safe_lib, env + "." + core, b)
        ids = vocab.encode_text(env + HOP + "." + core)
        n_pre = len(vocab.tok.encode(env + HOP).ids) + 1
        if len(ids) <= max_len and 1 < n_pre < len(ids) - 1:
            for _ in range(upweight):
                items.append((ids, n_pre))
            stats["ok"] += 1
    log(f"[t3] {dict(stats)} decode-sample {decode_ok}/{decode_checked}")
    return items


def canonicalize_digits(safe_str: str) -> str:
    """Renumber ring-closure digits by order of first appearance (1,2,...,9,
    then %10,%11,...). Digits INSIDE bracket atoms ([3H], [11C]...) are
    isotope/hydrogen labels, never ring closures, and are left untouched."""
    import re as _re

    # tokenize into bracket atoms / %NN / single digits / other chars
    tokens = _re.findall(r"\[[^\]]*\]|%\d{2,3}|\d|.", safe_str)
    mapping: dict[str, str] = {}
    nxt = 1

    def label(k: int) -> str:
        return str(k) if k <= 9 else f"%{k:02d}"

    out = []
    for t in tokens:
        if (t.isdigit() and len(t) == 1) or (t.startswith("%") and t[1:].isdigit()):
            if t not in mapping:
                mapping[t] = label(nxt)
                nxt += 1
            out.append(mapping[t])
        else:
            out.append(t)
    return "".join(out)


def safe_encode_canonical(smiles: str) -> str | None:
    """safe.encode + ring-digit canonicalization - the exact form every
    training item uses; inference prompts MUST go through the same path."""
    s = safe_encode_robust(smiles)
    if not s:
        return None
    return canonicalize_digits(s) or s


def decode_repaired(safe_str: str) -> str | None:
    """SAFE's official decoder with repair (fix=True): unpaired ring digits
    get dummy attachment atoms; dummies are replaced by carbons and removed.
    Rescues generations that stopped one closure short. Canonical SMILES or
    None; molecules that still carry dummies (multi-dangling fragments) are
    rejected."""
    try:
        import safe as safe_lib
        from rdkit import Chem as _Chem

        conv = safe_lib.converter.SAFEConverter()
        smi = conv.decoder(safe_str, canonical=False)
        if not smi:
            return None
        m = _Chem.MolFromSmiles(smi)
        if m is None or any(a.GetSymbol() == "*" for a in m.GetAtoms()):
            return None
        return _Chem.MolToSmiles(m)
    except Exception:
        return None
