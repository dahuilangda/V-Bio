"""The epsilon-exploration mutation must respect the user's NCAA pool.

Historical defect: `mutate_candidate` treated an EMPTY user pool (natural-only
design) as "no pool set" and silently fell back to the FULL preset catalog —
30% of fallback mutations then injected SEP/DAL/ORN even though the user capped
non-natural residues at 0, and elite selection spread the modifications across
the whole population (observed: SEP in 85/192 shipped sequences with
peptideNonNaturalMax=0).

Contract under test:
  * explicit empty pool (argument or parent attribute) -> NO natural->NCAA move, ever
  * explicit non-empty pool -> NCAA moves draw only from that pool
  * no pool information at all -> legacy standalone behavior (full catalog allowed)
  * ncaa_max=0 -> the natural->NCAA channel is closed even with a non-empty pool
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from peplm.candidate import Candidate
from peplm.generate.edit import mutate_candidate
from peplm.residues import NCAA_TOKENS


PARENT = Candidate(tokens=["<lin>"] + list("GKGKGKGKGK"), cyclic=False)


def _run_mutations(pool, ncaa_max=None, n=400):
    out = []
    for _ in range(n):
        cand = mutate_candidate(PARENT, random.Random(random.random()), ncaa_pool=pool, ncaa_max=ncaa_max)
        out.extend(t for t in cand.residues if t.startswith("["))
    return out


def test_empty_pool_never_produces_ncaa():
    assert _run_mutations([]) == []


def test_pool_attribute_empty_never_produces_ncaa():
    parent = Candidate(tokens=PARENT.tokens, cyclic=False)
    parent.ncaa_pool = []
    rng = random.Random(7)
    for _ in range(200):
        cand = mutate_candidate(parent, rng)
        assert all(not t.startswith("[") for t in cand.residues)


def test_nonempty_pool_draws_only_from_pool():
    pool = ["[AIB]", "[NLE]"]
    produced = set(_run_mutations(pool))
    assert produced <= set(pool)
    assert produced  # the channel is exercised, not dead


def test_ncaa_max_zero_closes_channel_even_with_pool():
    assert _run_mutations(["[AIB]", "[NLE]"], ncaa_max=0) == []


def test_no_pool_info_keeps_legacy_catalog_behavior():
    parent = Candidate(tokens=PARENT.tokens, cyclic=False)
    rng = random.Random(11)
    produced = set()
    for _ in range(400):
        cand = mutate_candidate(parent, rng)
        produced.update(t for t in cand.residues if t.startswith("["))
    # standalone use may draw from the full catalog — and only from it
    assert produced <= set(NCAA_TOKENS)
