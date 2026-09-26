"""Secondary-structure modes for ESM3 peptide proposals.

A mode maps the ACTUAL peptide length to an ss8 (DSSP) profile string;
the HTTP client and the inference service share these builders so a
request only carries the mode name and every call site builds the
template at the length it actually samples.

Modes:
  auto         no conditioning (the model decides)
  helix        amphipathic alpha helix: C-capped H core
  hairpin      beta hairpin: two E strands flanking a T-turn; short
               peptides need a covalent constraint to hold the pair --
               ss8 only biases residue propensity, the strand pairing
               itself is downstream structure validation's job
  strand_loop  extended strand + turn + free tails (diversity mode)
  hth          helix-turn-helix; the inter-helix packing cannot be
               expressed by a one-dimensional track, so this is weak
               guidance only (API-only; not exposed in the UI)

helix and hairpin templates were verified against the live ESM3 service
(composition A/B on MDM2, lengths 12/16/20: helix shifts helix-former
residues +25..34pp, hairpin shifts strand-former residues +17..23pp
with aromatics up; a flat "E" profile is unstable at some lengths, so
beta modes must use the explicit hairpin template).
"""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

MODES = ("auto", "helix", "hairpin", "strand_loop", "hth", "env")

# "env" keeps the legacy ESM3_SS_PROFILE behaviour for callers that never
# choose a mode; it resolves at the proposer, not here.


def helix_profile(n: int) -> str:
    """C-capped H core; below 12 residues the caps eat the helix."""
    if n < 12:
        return "H" * n
    return "C" + "H" * (n - 2) + "C"


def hairpin_profile(n: int) -> str:
    """Two E strands flanking a central T turn.

    Turn width: 2 residues up to 16 (validated 12/16/20 with t=2), 3 for
    longer peptides (Gellman-type beta-turns read longer loops as the
    chains grow); strands split as evenly as the parity allows."""
    if n < 8:
        return "E" * n
    t = 2 if n <= 16 else 3
    n_e = n - t
    n1 = n_e - n_e // 2
    n2 = n_e // 2
    return "E" * n1 + "T" * t + "E" * n2


def strand_loop_profile(n: int) -> str:
    """Leading C tail, one extended strand, a turn, then free C tail."""
    if n < 8:
        return "C" * n
    n_e = min(max(round(n / 3), 3), 6)
    n_t = min(max((n - n_e - 1) // 2, 2), 4)
    return "C" + "E" * n_e + "T" * n_t + "C" * (n - 1 - n_e - n_t)


def hth_profile(n: int) -> str:
    """Helix-turn-helix: split the length into two H segments and a
    3-4 residue T turn."""
    if n < 14:
        return helix_profile(n)
    t = 3 if n < 18 else 4
    n_h = n - t
    h1 = n_h - n_h // 2
    return "H" * h1 + "T" * t + "H" * (n_h // 2)


_BUILDERS = {
    "helix": helix_profile,
    "hairpin": hairpin_profile,
    "strand_loop": strand_loop_profile,
    "hth": hth_profile,
}


def build_ss_profile(mode: str | None, n: int) -> str | None:
    """ss8 profile for a peptide of length ``n`` under ``mode``.

    Returns None for auto (and any unknown mode): conditioning stays off
    instead of guessing."""
    if not mode or mode in ("auto", "env"):
        return None
    builder = _BUILDERS.get(mode)
    if builder is None:
        _logger.warning(
            "unknown structure mode %r: ss8 conditioning left off", mode)
        return None
    n = max(int(n), 1)
    profile = builder(n)
    assert len(profile) == n, f"{mode} profile length {len(profile)} != {n}"
    return profile


def resolve_ss_profile(mode: str | None, legacy_profile: str | None,
                       n: int) -> str | None:
    """Effective ss8 template: an explicit legacy profile string wins
    (raw-ss_profile HTTP callers), else the mode's length-aware builder."""
    if legacy_profile:
        return legacy_profile
    return build_ss_profile(mode, n)


def normalize_mode(value: str | None) -> str:
    """Validate a user-supplied mode string; unknown values raise."""
    mode = (str(value or "auto")).strip().lower()
    if mode not in MODES:
        raise ValueError(
            f"structure_mode 必须是 {'/'.join(MODES)} 之一，收到 {value!r}")
    return mode
