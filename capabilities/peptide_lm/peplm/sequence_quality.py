"""Degenerate-sequence detection for the peptide design loop.

Structure predictors assign systematically inflated confidence to
periodic and low-complexity sequences (Pratt et al. 2025), so degenerate
motifs survive confidence-based ranking and pollute the elite pool and
the GRPO reward. SEG-style low-complexity tools (window 12) cannot see
an isolated 8-mer in a 10-18 aa peptide, hence this bespoke gate.
"""
from __future__ import annotations

from collections import Counter

# Low-complexity window: short enough to localize the variable face of
# a 10-18 aa peptide, long enough to separate real periodicity from
# chance matches.
_WINDOW = 8
# >=4 identical residues in a row. Uniform side chains bury against any
# complementary surface and score well on contact rewards while being
# aggregation-prone and non-specific (an R7 proposal once shipped with
# inter_min 2.08 A and 27 clash pairs before this gate existed).
_MIN_RUN = 4
_PERIODS = (2, 3, 4)
# Residues an exact repeat must span to count: 6 for period 2-3, 8 for
# period 4. Shorter spans (AQAQ) are motifs, not collapse modes.
_PERIOD_SPAN = {2: 6, 3: 6, 4: 8}
# Fraction of the window held by one residue when >2 distinct residues
# are present (6/8): below the homopolymer threshold but still a
# degenerate composition for a binding face.
_TOP_FRACTION = 0.75
# Composite-score deduction per flagged pattern, capped so accumulation
# never drives a score below zero.
PENALTY_PER_FLAG = 0.15
PENALTY_CAP = 0.45

# Bicyclic layouts stamp the fixed p62-binding face onto the proposer's
# sequence, and a user mask can pin other faces. Fixed layout is design,
# not collapse — a pinned EEEE face must not flag every candidate — so
# every check (runs included) operates on the variable region after the
# fixed prefix/suffix are stripped. The CREEEC face holds no run >= 4
# and the single-C anchors never form one, so this is a no-op for the
# default layout.
DEFAULT_FIXED_PREFIX = "CREEEC"


def _max_run(seq: str) -> int:
    best = run = 0
    prev = ""
    for ch in seq:
        run = run + 1 if ch == prev else 1
        best = max(best, run)
        prev = ch
    return best


def _has_exact_period(seq: str, start: int, period: int, span: int) -> bool:
    """True when seq[start:start+span] repeats exactly with `period`."""
    if start + span > len(seq):
        return False
    return all(seq[start + i] == seq[start + i + period]
               for i in range(span - period))


def degeneracy_flags(sequence: str,
                     fixed_prefix: str = DEFAULT_FIXED_PREFIX,
                     fixed_suffix: str = "") -> list[str]:
    """Patterns marking a sequence as degenerate. An empty list means the
    sequence passes; each flag costs composite score (degeneracy_penalty)
    and blocks proposal acceptance (the diversity filter). Fixed layout
    (the stamped CREEEC face, a user mask) is stripped at both ends
    before the window and period checks when it matches."""
    if not sequence:
        return []
    start = len(fixed_prefix) if (fixed_prefix
                                  and sequence.startswith(fixed_prefix)) else 0
    end = len(sequence) - len(fixed_suffix) if (fixed_suffix
                                                and sequence.endswith(fixed_suffix)) else len(sequence)
    variable = sequence[start:max(start, end)]
    flags: list[str] = []

    run = _max_run(variable)
    if run >= _MIN_RUN:
        flags.append(f"homopolymer_run_{run}")

    n = len(variable)
    for period in _PERIODS:
        span = _PERIOD_SPAN[period]
        if any(_has_exact_period(variable, start_, period, span)
               for start_ in range(n - span + 1)):
            flags.append(f"period_{period}_repeat")
            break

    win = min(_WINDOW, n)
    for pos in range(n - win + 1) if win else ():
        seg = variable[pos:pos + win]
        counts = Counter(seg)
        if len(counts) <= 2:
            flags.append(f"{len(counts)}_residue_window")
            break
        top_aa, top_n = counts.most_common(1)[0]
        if top_n / win >= _TOP_FRACTION:
            flags.append(f"single_residue_dominant_{top_aa}")
            break
    return flags


def degeneracy_penalty(flags: list[str]) -> float:
    """Composite-score deduction for the flagged patterns."""
    if not flags:
        return 0.0
    return min(PENALTY_CAP, PENALTY_PER_FLAG * len(flags))
