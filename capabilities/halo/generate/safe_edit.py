"""SAFE-space edit operator.

The prompt lists KEPT fragments (core first if kept, then environment
fragments near->far); the model regenerates everything after <cont>.
`keep_core` and `radius` select the edit type - see halo/generate/radius_tasks.
"""

from __future__ import annotations

import random

from rdkit import Chem, RDLogger
from rdkit.Chem.Scaffolds import MurckoScaffold

from halo.generate.safe_tasks import (FragmentClassifier, canonicalize_digits,
                                     reorder, roundtrip_ok)
from halo.generate.radius_tasks import fragment_distances, order_near_far
from halo.score.properties import is_pains, passes_window, compute_descriptors, DEFAULT_WINDOW

RDLogger.DisableLog("rdApp.*")

_HOP, CONT = "<hop>", "<cont>"
_CLF = FragmentClassifier()


def _safe_valid(smiles: str | None, window=None) -> bool:
    if not smiles:
        return False
    m = Chem.MolFromSmiles(smiles)
    if m is None or m.GetNumHeavyAtoms() < 10 or m.GetNumHeavyAtoms() > 60:
        return False
    if is_pains(smiles):
        return False
    return passes_window(compute_descriptors(m), window or DEFAULT_WINDOW)


def _safe_lib():
    import safe as safe_lib

    return safe_lib





def _digits_of(safe_span: str) -> list[str]:
    """Ring-closure labels only: bracket atoms may contain digits (isotopes)
    and are never ring closures."""
    import re as _re

    toks = _re.findall(r"\[[^\]]*\]|%\d{2,3}|.", safe_span)
    return [t for t in toks if (len(t) == 1 and t.isdigit()) or (t.startswith("%") and t[1:].isdigit())]


def dangling_digits(safe_span: str) -> set[str]:
    """Ring-closure labels appearing an odd number of times: the attachment
    points the kept context offers to the regenerated part."""
    from collections import Counter as _C

    c = _C(_digits_of(safe_span))
    return {d for d, n in c.items() if n % 2 == 1}

def unified_edit(model, vocab, smiles: str, safe_str: str, device, n: int,
                 rng: random.Random, window=None, radius: int | None = None,
                 keep_core: bool = True, n_samples: int = 16):
    """Single edit operator for every capability.

    The prompt lists KEPT fragments in the training ordering (core first if
    kept, then environment fragments near->far by fragment-graph BFS), the
    model regenerates everything else:
      radius=99, keep_core=True  -> conservative decoration
      radius=r,  keep_core=True  -> radius-r ball redesign
      radius=r,  keep_core=False -> environment-conditioned scaffold hop at r
    radius=None samples r per attempt (the operator explores the ladder).
    Returns dicts with smiles/cond_prompt/cond_tail for process-level GRPO.
    """
    import safe as safe_lib
    from rdkit import DataStructs
    from rdkit.Chem import rdFingerprintGenerator

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return []
    frags = safe_str.split(".")
    if len(frags) < 2:
        return []
    cls = _CLF.classify(smiles, safe_str)
    if cls is None:
        return []
    core_frags, env_nf, core_idx, env_idx = order_near_far(frags, cls[0])
    if not env_nf:
        return []
    d_env = fragment_distances(frags, core_idx)
    gen_fp = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    ref_fp = gen_fp.GetFingerprint(mol)
    ref_scaf = cls[2]

    out, seen = [], set()
    attempts = 0
    while len(out) < n and attempts < n * 3:
        attempts += 1
        r = radius if radius is not None else rng.choice([1, 2, 3, 99])
        env_kept = [f for f, i in zip(env_nf, env_idx) if d_env[i] <= r]
        if not env_kept:
            env_kept = env_nf[:1]
        if keep_core:
            kept = core_frags + env_kept
        else:
            kept = env_kept
        # canonicalize the prompt sequence: kept leads the final sequence in
        # training, so appearance-order numbering of the prompt matches the
        # training distribution exactly
        prompt = canonicalize_digits(".".join(kept))
        kept = prompt.split(".")
        use_fsm = hasattr(vocab, "pattern_id") or type(vocab).__name__ == "DigitBPEVocab"
        gen = model.sample_with_prompt(prompt + CONT, n=n_samples, device=device,
                                       temperature=1.0, top_p=0.95,
                                       include_prompt=False,
                                       ban_tokens=["<hop>", "<core>", CONT],
                                       canonical_fsm=use_fsm,
                                       require_digit_closure=use_fsm)
        offered = dangling_digits(prompt)
        for tail in gen:
            if not tail:
                continue
            full = prompt + (tail if tail.startswith(".") else "." + tail)
            # every offered attachment must be consumed exactly once by the
            # generated part; digits internal to the generation (pairing two
            # of its own fragments) are fine and left alone
            from collections import Counter as _C2

            tail_counts = _C2(_digits_of(tail[1:] if tail.startswith('.') else tail))
            if any(tail_counts.get(d, 0) != 1 for d in offered):
                continue
            try:
                smi = safe_lib.decode(full, ignore_errors=True)
            except Exception:
                smi = None
            if not smi:
                from halo.generate.safe_tasks import decode_repaired

                smi = decode_repaired(full)
            if not _safe_valid(smi, window) or smi in seen or smi == smiles:
                continue
            m2 = Chem.MolFromSmiles(smi)
            try:
                scaf = MurckoScaffold.MurckoScaffoldSmiles(mol=m2)
            except Exception:
                continue
            sim = DataStructs.TanimotoSimilarity(ref_fp, gen_fp.GetFingerprint(m2))
            if not keep_core:
                # hop semantics: core must differ, stay in the analog band
                if scaf == ref_scaf or not (0.15 <= sim <= 0.85):
                    continue
            else:
                rm = Chem.MolFromSmiles(ref_scaf)
                if scaf != ref_scaf and (rm is None or not m2.HasSubstructMatch(rm)):
                    continue
            seen.add(smi)
            compat = 1.0
            if not keep_core:
                compat = attachment_compatibility(smiles, prompt, full[len(prompt):])
            out.append({"smiles": smi, "safe": full, "scaffold": scaf,
                        "sim": round(sim, 3), "radius": r, "attach_compat": round(compat, 3),
                        "cond_prompt": prompt + CONT,
                        "cond_tail": tail if tail.startswith(".") else "." + tail})
    # attachment-faithful first: wrong-regiochemistry cores rank below
    # parent-characteristic attachments even at similar similarity
    out.sort(key=lambda r: -(r.get("attach_compat", 1.0) * 2 + r.get("sim", 0)))
    return out[:n]


def attachment_profiles(safe_span: str) -> dict[str, dict]:
    """For every ring-closure label in a SAFE span, profile the host atom
    that carries it (the unit right before the digit): symbol, aromaticity,
    hetero flag. Returns {label: profile}."""
    import re as _re

    toks = _re.findall(r"\[[^\]]*\]|%\d{2,3}|Cl|Br|.", safe_span)
    profiles: dict[str, list[dict]] = {}
    for i, t in enumerate(toks):
        if (len(t) == 1 and t.isdigit()) or (t.startswith("%") and t[1:].isdigit()):
            host = toks[i - 1] if i > 0 else ""
            lower = host.lower()
            profiles.setdefault(t, []).append({
                "host": host,
                "aromatic": host != lower and host.isalpha(),
                "hetero": any(ch in lower for ch in "nso"),
            })
    return profiles


def attachment_compatibility(parent_smiles: str, env_span: str, gen_span: str) -> float:
    """Score how faithfully the generated part attaches to the environment,
    using the PARENT molecule's original attachment bonds as the reference.

    For every label the environment offers, the parent connected a specific
    (env atom, core atom) pair; the replacement core should form bonds of
    the same chemical character: aromaticity of the new host must match the
    original core-side host, and heteroatom attachment points (usually key
    polar contacts) score higher when preserved. Score in [0,1]."""
    try:
        from halo.generate.safe_tasks import safe_encode_canonical

        env_prof = attachment_profiles(env_span)
        full = safe_encode_canonical(parent_smiles)
        if not full:
            return 0.5
        all_prof = attachment_profiles(full)
        gen_prof = attachment_profiles(gen_span)
        score = 0.0
        n = 0
        for d, occs in env_prof.items():
            if d not in all_prof:
                continue
            env_side = occs[-1]
            # reference = the occurrence on the OTHER side of the bond
            others = [p for p in all_prof[d] if p["host"] != env_side["host"]] or all_prof[d]
            ref = others[-1]
            got_list = gen_prof.get(d)
            if not got_list:
                continue  # label consumed elsewhere (internal pair)
            got = got_list[0]
            n += 1
            s = 0.4
            if got["aromatic"] == ref["aromatic"]:
                s += 0.4
            if got["hetero"] == ref["hetero"]:
                s += 0.2
            score += s
        return score / max(n, 1)
    except Exception:
        return 0.5
