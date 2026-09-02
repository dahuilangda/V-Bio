"""Radius-conditioned unified fragment editing tasks.

A molecule is its SAFE fragment sequence, ordered near->far by fragment-graph
BFS distance from the Murcko core, so the prompt prefix length IS the
environment radius around the edit site (the mmpdb radius concept, but
learned into the model rather than looked up):

    <bos> [kept fragments, near->far] <cont> . [regenerated] <eos>

  kept = {}                 -> de novo
  kept = core + env@cut     -> decoration
  kept = env@r (no core)    -> scaffold hop at radius r
  kept = core + env@r       -> keep-and-redesign

One globally consistent ordering lets a single autoregressive model learn the
whole conditional family {p(rest | env@r)}_r at once. A fraction of examples
drop part of the kept context (raft_drop) so the model also works with little
or no environment visible - the closed-book behaviour pure-model inference
needs.
"""

from __future__ import annotations

import random
import re
from collections import Counter, deque

from rdkit import Chem, RDLogger

from halo.generate.safe_prior import CONT
from halo.generate.safe_tasks import (FragmentClassifier, canonicalize_digits,
                                     digit_parity_ok, roundtrip_ok)

RDLogger.DisableLog("rdApp.*")

# <cont> is the single generation marker for every capability; it already
# exists in the trained BPE vocabulary.
GEN = CONT
_PCT = re.compile(r"%(\d{2,3})")

# kept-set policy weights (sum 1.0): de novo / decoration / hop / radius ball.
# Production multi-view recipe (mv): hop is the eval-critical capability and
# carries the exact-rebuild supervision, so it takes the largest share.
POLICIES = (("denovo", 0.12), ("decor", 0.28), ("hop", 0.28), ("ball", 0.32))
MV_POLICIES = (("denovo", 0.10), ("decor", 0.25), ("hop", 0.35), ("ball", 0.30))
# hop radius sampling: the paired ladder metric lives at the extremes
# r=1 (only the adjacent shell visible) and r=99 (full environment), so the
# mv recipe concentrates 70% of hop mass there instead of the flat 1:1:1:1.
MV_HOP_R = ((1, 0.30), (2, 0.15), (3, 0.15), (99, 0.40))


def fragment_graph(frags: list[str]) -> dict[int, set[int]]:
    """Fragment adjacency from shared ring-closure digits (SAFE attachment)."""
    digit_owner: dict[str, list[int]] = {}
    for i, f in enumerate(frags):
        masked = f
        for m in _PCT.finditer(f):
            digit_owner.setdefault(m.group(1), []).append(i)
            masked = masked.replace(m.group(0), "  ")
        for ch in masked:
            if ch.isdigit():
                digit_owner.setdefault(ch, []).append(i)
    adj: dict[int, set[int]] = {i: set() for i in range(len(frags))}
    for owners in digit_owner.values():
        if len(owners) >= 2:
            for a in owners:
                for b in owners:
                    if a != b:
                        adj[a].add(b)
    return adj


def fragment_distances(frags: list[str], core_idx: list[int]) -> dict[int, int]:
    """BFS distance (in fragment-hops) of every fragment to the core block -
    the fragment-graph analogue of mmpdb's attachment-radius."""
    adj = fragment_graph(frags)
    dist = {i: 0 for i in core_idx}
    q = deque(core_idx)
    while q:
        u = q.popleft()
        for v in adj.get(u, ()):
            if v not in dist:
                dist[v] = dist[u] + 1
                q.append(v)
    # fragments not connected to the core (rare, e.g. mixtures) get a large d
    return {i: dist.get(i, 99) for i in range(len(frags))}


def order_near_far(frags: list[str], core_idx: list[int]) -> tuple[list[str], list[str], list[int], list[int]]:
    """Reorder: core block first, then environment fragments by ascending
    BFS distance (ties by original index - deterministic). Returns
    (core_frags, env_near_far, core_positions_in_original, env_positions)."""
    env_idx = [i for i in range(len(frags)) if i not in set(core_idx)]
    dist = fragment_distances(frags, core_idx)
    env_idx.sort(key=lambda i: (dist[i], i))
    return ([frags[i] for i in core_idx], [frags[i] for i in env_idx],
            list(core_idx), env_idx)


def build_unified_items(vocab, corpus_pairs, hop_pairs=None, *, max_len: int = 256,
                        seed: int = 0, raft_drop: float = 0.4,
                        decode_check_rate: float = 0.02, canonical: bool = True,
                        samples_per_mol: int = 2, log=print, mv: bool = False):
    """corpus_pairs: (smiles, safe). hop_pairs: (a_smiles, b_smiles) or
    (a_smiles, b_smiles, b_safe_view) when the B-side SAFE encoding is
    pre-computed (multi-view hop pairs) - the B-side molecule provides the
    hop-mode example (its env conditions a replacement core that REALLY
    occurred in a scaffold-hop context).
    Returns (ids, loss_start) items in the unified <gen> format.
    mv=True selects the multi-view recipe: hop-heavy policies and hop radii
    concentrated on the paired extremes {r=1, r=99} the ladder metric uses."""
    import safe as safe_lib

    rng = random.Random(seed)
    clf = FragmentClassifier()
    items, stats = [], Counter()
    decode_checked = decode_ok = 0
    recipe = MV_POLICIES if mv else POLICIES
    policies = [p for p, _ in recipe]
    pweights = [w for _, w in recipe]
    hop_r = ([r for r, _ in MV_HOP_R], [w for _, w in MV_HOP_R]) if mv else None

    def emit(kept_frags: list[str], gen_frags: list[str], upweight: int = 1):
        nonlocal decode_checked, decode_ok
        if not gen_frags:
            stats["empty_gen"] += 1
            return
        # canonicalize the FINAL sequence (kept first, then generated): the
        # kept block leads, so its digits number 1..k by appearance and the
        # generation continues the scheme - "next new digit = smallest
        # unused" becomes a deterministic rule the model can learn
        full = canonicalize_digits(".".join(list(kept_frags) + list(gen_frags)))
        frags_c = full.split(".")
        if len(frags_c) != len(kept_frags) + len(gen_frags):
            stats["canon_frag_mismatch"] += 1
            return
        kept_c, gen_c = frags_c[: len(kept_frags)], frags_c[len(kept_frags):]
        kept, rest = ".".join(kept_c), ".".join(gen_c)
        if not digit_parity_ok(full):
            stats["parity_fail"] += 1
            return
        if rng.random() < decode_check_rate:
            decode_checked += 1
            decode_ok += roundtrip_ok(safe_lib, full, cur_smiles)
        # kept non-empty: model emits '.' then the regenerated fragments;
        # de novo (empty kept): model emits the first fragment directly
        text = (kept + GEN + "." if kept else GEN) + rest
        try:
            ids = vocab.encode_text(text)
        except KeyError:
            stats["oov"] += 1
            return
        n_pre = len(vocab.tok.encode((kept + GEN) if kept else GEN).ids) + 1  # +1 = <bos>
        if len(ids) <= max_len and 1 < n_pre < len(ids) - 1:
            for _ in range(upweight):
                items.append((ids, n_pre))
            stats["ok"] += 1

    def process(smiles: str, safe_str: str, upweight: int = 1, hop_mode: bool = False):
        frags = safe_str.split(".")
        if len(frags) < 2:
            stats["too_few_frags"] += 1
            return
        cls = clf.classify(smiles, safe_str)
        core_frags, env_nf, core_idx, env_idx = None, None, None, None
        if cls is not None:
            core_frags, env_nf, core_idx, env_idx = order_near_far(frags, cls[0])
        policy = rng.choices(policies, weights=pweights, k=1)[0]
        if hop_mode:
            policy = rng.choice(["hop", "ball"])
        if policy == "denovo" or cls is None:
            emit([], frags, upweight)
            return
        d_env = fragment_distances(frags, core_idx)
        if policy == "decor":
            # keep core + a random near->far cut of the environment
            k = rng.randrange(0, len(env_nf) + 1)
            kept = core_frags + env_nf[:k]
            gen = env_nf[k:]
            emit(kept, gen, upweight)
        elif policy == "hop":
            # keep environment ball of radius r, regenerate core + outside
            r = rng.choices(hop_r[0], weights=hop_r[1], k=1)[0] if hop_r else rng.choice([1, 2, 3, 99])
            kept = [f for f, i in zip(env_nf, env_idx) if d_env[i] <= r]
            if not kept:  # tiny env: fall back to decoration cut
                k = max(1, len(env_nf) // 2)
                kept = env_nf[:k]
            gen = [f for f in core_frags] + [f for f, i in zip(env_nf, env_idx) if d_env[i] > r]
            emit(kept, gen, upweight)
        else:  # ball: keep core + radius-r ball, regenerate outside
            r = rng.choice([1, 2, 3])
            kept = core_frags + [f for f, i in zip(env_nf, env_idx) if d_env[i] <= r]
            gen = [f for f, i in zip(env_nf, env_idx) if d_env[i] > r]
            emit(kept, gen, upweight)

    for smi, s in corpus_pairs:
        cur_smiles = smi
        for _ in range(samples_per_mol):
            process(smi, s)
        if rng.random() < raft_drop:
            # RAFT-style context dropout: degrade toward closed-book behaviour
            frags = s.split(".")
            if len(frags) >= 2:
                k = rng.randrange(0, max(1, len(frags) // 2))
                kept = frags[:k]
                stats["raft"] += 1
                rest = frags[k:]
                full = canonicalize_digits(".".join(kept + rest))
                fc = full.split(".")
                if len(fc) == len(kept) + len(rest) and digit_parity_ok(full):
                    kept, rest = fc[:k], fc[k:]
                    text = ((".".join(kept) + GEN + ".") if kept else GEN) + ".".join(rest)
                    try:
                        ids = vocab.encode_text(text)
                    except KeyError:
                        stats["oov"] += 1
                        continue
                    n_pre = len(vocab.tok.encode((".".join(kept) + GEN) if kept else GEN).ids) + 1
                    if len(ids) <= max_len and 1 < n_pre < len(ids) - 1:
                        items.append((ids, n_pre))
                        stats["ok"] += 1
            continue
    if hop_pairs:
        from halo.generate.safe_tasks import safe_encode_robust

        for hp in hop_pairs:
            if len(hp) >= 3 and hp[2]:
                a, b, sb = hp[0], hp[1], hp[2]
            else:
                a, b = hp[0], hp[1]
                try:
                    sb = safe_encode_robust(b)
                except Exception:
                    sb = None
            if not sb:
                stats["hop_encode_fail"] += 1
                continue
            cur_smiles = b
            process(b, sb, upweight=3, hop_mode=True)
    rng.shuffle(items)
    log(f"[unified] {dict(stats)} decode-sample {decode_ok}/{decode_checked}")
    return items
