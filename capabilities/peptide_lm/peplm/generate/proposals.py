"""Proposal operators for the production design loop: de novo sampling,
FIM span edits of scored elites, and epsilon-exploration point moves.
Proposals carry a GRPO grouping key (de novo shares one group, edit and
mutation children are keyed by parent); SS conditioning rides the
additive ss_track channel, indexed by the de novo residue stream."""

from __future__ import annotations

import random
import statistics

from peplm.candidate import Candidate
from peplm.vocab import from_modifications

SS_IDS = {"h": 1, "e": 2, "l": 3}

# T=1.0 emits NNNN/GGGG low-complexity runs from the 87M prior; 0.85
# sharpens motifs while top_p=0.95 preserves diversity.
DE_NOVO_TEMPERATURE = 0.85
DE_NOVO_TOP_P = 0.95

# Prompt-side tokens must never be emitted by the decoder (SS tokens ride
# the track channel, not the token stream).
BANNED_DECODE_TOKENS = [
    "<sol_h>", "<sol_m>", "<sol_l>", "<syn_h>", "<syn_m>", "<syn_l>",
    "<liab_h>", "<liab_m>", "<liab_l>", "<dev_hi>", "<dev_md>", "<dev_lo>",
    "<cont>", "<mask>", "<pre>", "<suf>", "<mid>", "<lin>", "<cyc>", "<bicy>",
    "<h>", "<e>", "<l>", "<s>",
] + [f"<L{5 * k}>" for k in range(1, 25)]


def normalize_len(peptide_length: int | None,
                  len_range: tuple[int, int] | None = None) -> tuple[int, int]:
    """Length is optional in the UI: None = free optimization over a wide
    adaptive range; a fixed value pins the range; an explicit range wins
    over both."""
    if len_range is not None and len_range[0] <= len_range[1]:
        return (int(len_range[0]), int(len_range[1]))
    if peptide_length is None or peptide_length <= 0:
        return (10, 30)
    return (peptide_length, peptide_length)


def ss_track_ids(ss_profile: str) -> list[int]:
    """Per-residue SS track ids; 's' and anything else map to 0 (free
    position) — partial specification is native to the track channel."""
    return [SS_IDS.get(ch, 0) for ch in ss_profile]


def de_novo_prompt(target_len: int, struct_token: str) -> list[str]:
    """Aspirational property tags + length bucket + modality token."""
    from peplm.data.build_corpus import bucket_tag

    return ["<sol_h>", "<syn_h>", "<liab_h>",
            bucket_tag(target_len), struct_token]


def sample_de_novo(agent, device, count: int, *, target_len: int,
                   struct_token: str, constraints=None,
                   ss_profile: str | None = None) -> list[list[str]]:
    """Prior samples as token lists; SS conditioning via the additive
    track channel when a profile is active."""
    return agent.sample_with_prompt(
        de_novo_prompt(target_len, struct_token), count, device,
        temperature=DE_NOVO_TEMPERATURE, top_p=DE_NOVO_TOP_P,
        constraints=constraints, target_len=target_len,
        return_tokens=True,
        ss_track=ss_track_ids(ss_profile) if ss_profile else None,
        ban_tokens=BANNED_DECODE_TOKENS)


def explore_count(recent_rewards: list[float], n: int) -> int:
    """De novo quota for a scored generation: scales with reward std —
    more de novo while the search is still exploring, fewer once it is
    converging on the elites."""
    if len(recent_rewards) >= 3:
        sd = statistics.stdev(recent_rewards)
        ratio = max(0.15, min(0.35, sd * 2))
    else:
        ratio = 0.25
    return max(1, int(n * ratio))


def candidate_tokens(sequence: str, modifications: list[dict],
                     struct_token: str) -> list[str]:
    """Token list of a scored row: structure token + residue/modification
    tokens."""
    return [struct_token] + from_modifications(sequence.upper(),
                                               modifications or [])


def elite_parent(row: dict, *, cyclic: bool, struct_token: str,
                 anchors: tuple = ()) -> Candidate:
    """Wrap a production result row as an edit/mutation parent, carrying
    its per-residue pLDDTs (the edit-position signal) and Cys anchors."""
    cand = Candidate(tokens=candidate_tokens(row["sequence"],
                                             row.get("modifications") or [],
                                             struct_token),
                     cyclic=cyclic)
    cand.metrics = {"binder_plddt": row.get("plddts") or None}
    cand._bicy_anchors = tuple(anchors)
    return cand


def _protect(parent: Candidate, fixed_map: dict, pool_tokens) -> None:
    """Fixed residues and Cys anchors are never edit/mutation targets."""
    parent._protected = set(fixed_map) | set(parent._bicy_anchors)
    parent.ncaa_pool = list(pool_tokens)


def edit_children(agent, vocab, parent: Candidate, count: int, device,
                  rng: random.Random, *, ncaa_max: int,
                  pool_tokens, fixed_map: dict,
                  plan_kwargs: dict) -> list[Candidate]:
    """FIM span-edit children of one elite parent."""
    from peplm.generate.edit import edit_candidates

    _protect(parent, fixed_map, pool_tokens)
    return edit_candidates(
        agent, vocab, parent, count, device, rng,
        ncaa_max=ncaa_max, fixed_abs=dict(fixed_map),
        pool_tokens=list(pool_tokens), plan_kwargs=plan_kwargs)


def mutated_child(parent: Candidate, rng: random.Random, *,
                  pool_tokens, ncaa_max: int,
                  fixed_map: dict) -> Candidate:
    """One point move on a random parent: conservative aa swap or an NCAA
    swap drawn from the user pool, capped by the NCAA budget."""
    from peplm.generate.edit import mutate_candidate

    _protect(parent, fixed_map, pool_tokens)
    return mutate_candidate(parent, rng, ncaa_pool=list(pool_tokens),
                            ncaa_max=ncaa_max)


def learn_row(residues: list[str], *, cyclic: bool, struct_token: str,
              ss_profile: str | None) -> tuple[list[str], list[int] | None]:
    """GRPO training row for one candidate under the active conditioning:
    conditioning prefix + residues, plus the SS track aligned to the
    residue span (track[j] conditions residue j; a shorter track leaves
    the tail free, a longer one is truncated)."""
    from peplm.generate.edit import cond_prefix

    prefix = cond_prefix(residues, cyclic, modality=struct_token)
    toks = prefix + residues
    if not ss_profile:
        return toks, None
    track = ss_track_ids(ss_profile)
    ss = [0] * len(toks)
    base = len(prefix)
    for j in range(len(residues)):
        ss[base + j] = track[j] if j < len(track) else 0
    return toks, ss
