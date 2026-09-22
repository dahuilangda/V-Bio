"""Residue-monomer vocabulary and sequence parsing.

A peptide is a string like ``AC[AIB]GK[CIT]W``: single letters are natural
amino acids, bracketed triplets are NCAA monomer tokens. The same string is
the token sequence (one token per monomer), so encode/decode are lossless
regex splits/joins — no BPE needed at this vocabulary size (~55 types).

Sequence <-> production format conversion (base one-letter sequence plus
``modifications: [{position, ccd, baseResidue}]``) is exact, so candidates
drop straight into the existing Boltz YAML protocol.
"""

from __future__ import annotations

import torch

import re

from peplm.residues import (
    NATURAL_AA,
    NCAA_TOKEN_TO_CCD,
    NCAA_PRESETS,
    NCAA_TOKENS,
)

_TOKEN_RE = re.compile(r"(\[[A-Z0-9]{2,4}\]|<[^>]{1,12}>|[A-Z])")

SPECIALS = ["<pad>", "<bos>", "<eos>"]
STRUCTURE_TOKENS = ["<lin>", "<cyc>", "<bicy>"]
EDIT_TOKENS = ["<cont>", "<mask>", "<pre>", "<suf>", "<mid>"]
DEV_TAGS = ["<dev_hi>", "<dev_md>", "<dev_lo>"]
# conditioning: per-property tags + length buckets (IgLM/ProGen style)
PROP_TAGS = ["<sol_h>", "<sol_m>", "<sol_l>",
             "<syn_h>", "<syn_m>", "<syn_l>",
             "<liab_h>", "<liab_m>", "<liab_l>"]
LEN_TAGS = [f"<L{5 * k}>" for k in range(1, 10)]  # <L5>..<L45>

ALL_RESIDUE_TOKENS = list(NATURAL_AA) + NCAA_TOKENS
ALL_TOKENS = (SPECIALS + STRUCTURE_TOKENS + EDIT_TOKENS + DEV_TAGS
              + PROP_TAGS + LEN_TAGS + ALL_RESIDUE_TOKENS)


def parse_tokens(seq: str) -> list[str]:
    """Split a sequence string into monomer/special tokens. Raises on
    characters that are neither tokens nor whitespace (protects silent
    corruption of candidate sequences)."""
    out: list[str] = []
    pos = 0
    for m in _TOKEN_RE.finditer(seq):
        if seq[pos:m.start()].strip():
            raise ValueError(f"unparsable segment {seq[pos:m.start()]!r} in {seq!r}")
        out.append(m.group(0))
        pos = m.end()
    if seq[pos:].strip():
        raise ValueError(f"unparsable tail {seq[pos:]!r} in {seq!r}")
    return out


def join_tokens(tokens: list[str]) -> str:
    return "".join(tokens)


def is_residue_token(tok: str) -> bool:
    return (len(tok) == 1 and tok in NATURAL_AA) or tok in NCAA_TOKENS


def residue_tokens(tokens: list[str]) -> list[str]:
    return [t for t in tokens if is_residue_token(t)]


def to_modifications(tokens: list[str]) -> tuple[str, list[dict]]:
    """(base one-letter sequence, modifications list) — the production format.

    Positions are 1-based over the full peptide, baseResidue is the natural
    parent written into the base sequence (V-Bio contract)."""
    base: list[str] = []
    mods: list[dict] = []
    for tok in residue_tokens(tokens):
        if tok in NCAA_TOKENS:
            ccd = NCAA_TOKEN_TO_CCD[tok]
            base_res = NCAA_PRESETS[ccd]["base"]
            mods.append({
                "position": len(base) + 1,
                "ccd": ccd,
                "baseResidue": base_res,
            })
            base.append(base_res)
        else:
            base.append(tok)
    return "".join(base), mods


def from_modifications(base_sequence: str, modifications: list[dict]) -> list[str]:
    """Inverse of to_modifications (ignores positions, rebuilds by base
    residue order — production modifications are always position-sorted)."""
    tokens = list(str(base_sequence).upper())
    by_pos = {int(m.get("position", 0)): str(m.get("ccd", "")).upper() for m in modifications or []}
    for pos, ccd in by_pos.items():
        if 1 <= pos <= len(tokens) and ccd in NCAA_PRESETS:
            tokens[pos - 1] = f"[{ccd}]"
    return tokens


def ncaa_count(tokens: list[str]) -> int:
    return sum(1 for t in tokens if t in NCAA_TOKENS)


def sequence_key(tokens: list[str], cyclic: bool) -> str:
    """Canonical dedup key (residues only, structure flag folded in)."""
    return ("" if not cyclic else "c#") + "".join(residue_tokens(tokens))


class Vocab:
    """stoi/itos/encode/decode over the fixed token inventory."""

    def __init__(self, tokens: list[str] | None = None):
        toks = tokens or ALL_TOKENS
        self.itos: dict[int, str] = {i: t for i, t in enumerate(toks)}
        self.stoi: dict[str, int] = {t: i for i, t in enumerate(toks)}
        self.pad = self.stoi["<pad>"]
        self.bos = self.stoi["<bos>"]
        self.eos = self.stoi["<eos>"]

    def __len__(self) -> int:
        return len(self.itos)

    def encode(self, text: str) -> list[int]:
        return [self.stoi[t] for t in parse_tokens(text)]

    def decode(self, ids: list[int]) -> str:
        return "".join(self.itos[int(i)] for i in ids)

    def encode_tokens(self, tokens: list[str]) -> list[int]:
        return [self.stoi[t] for t in tokens]

    def decode_tokens(self, ids: list[int]) -> list[str]:
        return [self.itos[int(i)] for i in ids]


DEFAULT_VOCAB = Vocab()


class PlacementMask:
    """Per-position allowed-token mask factory for residue sequences.

    Track how many residues were already emitted (special tokens like dev tags
    and <lin>/<cyc> do not count); placement rules then decide which NCAA
    tokens remain legal at the current position. The NCAA pool is the preset
    table plus any user-registered residues (peplm.residues.USER_RESIDUES)."""

    def __init__(self, vocab: Vocab, max_len: int,
                 ncaa_max: int | None = None,
                 banned_ncaa: list[str] | None = None,
                 extra_tokens: list[str] | None = None,
                 pool_tokens: list[str] | None = None):
        """pool_tokens: the explicit user-specified NCAA pool (strict — only
        these bracket tokens are legal). None = the full catalog (preset
        table + user-registered residues), used at training time."""
        from peplm.residues import USER_RESIDUES, placement_lookup

        self.vocab = vocab
        self.max_len = max_len
        self.ncaa_max = ncaa_max
        banned = set(banned_ncaa or [])
        if pool_tokens is not None:
            self.tokens = [t for t in dict.fromkeys(pool_tokens)
                           if t not in banned]
        else:
            self.tokens = [f"[{c}]" for c in NCAA_PRESETS] \
                + [f"[{c}]" for c in USER_RESIDUES] \
                + list(extra_tokens or [])
            self.tokens = [t for t in dict.fromkeys(self.tokens)
                           if t not in banned]
        self._placement = placement_lookup
        self.ncaa_ids = {vocab.stoi[t] for t in self.tokens if t in vocab.stoi}
        # strict pool: bracket tokens NOT in the user pool are banned outright
        self.strict_ban: set[int] = set()
        if pool_tokens is not None:
            allowed = set(self.tokens)
            self.strict_ban = {vocab.stoi[t] for t in vocab.itos.values()
                               if t.startswith("[") and t not in allowed}
            self.strict_ban -= self.ncaa_ids

    def mask(self, emitted_residues: int, ncaa_used: int, total_len_hint: int,
             logits: torch.Tensor):
        """Apply in-place legality mask to a [B, V] logits batch."""
        stoi = self.vocab.stoi
        at_n = emitted_residues == 0
        near_c = total_len_hint and emitted_residues >= total_len_hint - 1
        ncaa_ok = self.ncaa_max is None or ncaa_used < self.ncaa_max
        banned_positions: set[int] = set(self.strict_ban)
        if not ncaa_ok:
            banned_positions |= self.ncaa_ids
        else:
            for t in self.tokens:
                pl = self._placement(t)
                tid = stoi.get(t)
                if tid is None:
                    continue
                if pl == "n_term" and not at_n:
                    banned_positions.add(tid)
                elif pl == "c_term" and not near_c:
                    banned_positions.add(tid)
                elif pl == "terminal" and not (at_n or near_c):
                    banned_positions.add(tid)
        if banned_positions:
            idx = torch.tensor(sorted(banned_positions), dtype=torch.long,
                               device=logits.device)
            logits.index_fill_(1, idx, float("-inf"))
