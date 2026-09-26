"""Structure-mode ss8 template builders: length contract and shape."""
from __future__ import annotations

import pytest

from peplm.integrate.ss_modes import (
    MODES,
    build_ss_profile,
    hairpin_profile,
    helix_profile,
    hth_profile,
    normalize_mode,
    strand_loop_profile,
)

VALID = set("GHITEBSC")


@pytest.mark.parametrize("mode", ["helix", "hairpin", "strand_loop", "hth"])
@pytest.mark.parametrize("n", range(6, 26))
def test_profile_length_is_exact(mode, n):
    profile = build_ss_profile(mode, n)
    assert profile is not None and len(profile) == n
    assert set(profile) <= VALID


def test_auto_and_env_and_unknown_return_none():
    assert build_ss_profile("auto", 14) is None
    assert build_ss_profile(None, 14) is None
    assert build_ss_profile("bogus", 14) is None
    # "env" resolves to the deployment profile at the proposer, never here
    assert build_ss_profile("env", 14) is None


def test_helix_caps_only_when_long_enough():
    assert helix_profile(10) == "H" * 10
    assert helix_profile(12) == "C" + "H" * 10 + "C"
    assert helix_profile(18)[0] == "C" and helix_profile(18)[-1] == "C"


def test_hairpin_turn_sits_midchain():
    # validated shape: strands flank a central turn, split near-evenly
    assert hairpin_profile(12) == "EEEEE" + "TT" + "EEEEE"
    p16 = hairpin_profile(16)
    assert len(p16) == 16 and p16.count("T") == 2
    assert set(p16) == {"E", "T"}
    first_t = p16.index("T")
    assert abs(first_t - (16 - 2) / 2) <= 1  # turn starts mid-chain
    # longer peptides widen the turn to 3
    assert hairpin_profile(20).count("T") == 3


def test_hth_splits_into_two_helices():
    p = hth_profile(20)
    assert len(p) == 20 and set(p) == {"H", "T"}
    assert p.count("T") == 4
    assert p.startswith("H") and p.endswith("H")


def test_strand_loop_shape():
    p = strand_loop_profile(16)
    assert len(p) == 16
    assert p[0] == "C" and "E" in p and "T" in p
    assert p.endswith("C")


def test_normalize_mode_rejects_unknown():
    assert normalize_mode("Helix ") == "helix"
    assert normalize_mode(None) == "auto"
    with pytest.raises(ValueError):
        normalize_mode("coil")
    assert set(MODES) >= {"auto", "helix", "hairpin", "strand_loop", "hth"}
