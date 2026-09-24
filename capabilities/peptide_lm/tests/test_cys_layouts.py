"""resolve_bicyclic_anchors: length-function Cys layouts (ring / ratio).

Mirrors frontend/src/utils/peptideCysLayout.test.ts — keep the expectations
identical on BOTH sides (positions shift by the 0-/1-based convention).
"""

import pytest

from peplm.loop.constraints import (
    apply_post_edit,
    choose_bicyclic_anchors,
    min_feasible_length_ratio,
    resolve_bicyclic_anchors,
)


def test_ring_layout_anchors_block_to_c_terminus():
    ring = {"mode": "ring", "ring1": 4, "ring2": 6}
    # 1-based [8, 13, 20] -> 0-based (7, 12, 19)
    assert resolve_bicyclic_anchors(20, {}, (), ring) == (7, 12, 19)
    # ring sizes preserved at every length
    assert resolve_bicyclic_anchors(25, {}, (), ring) == (12, 17, 24)


def test_ring_layout_refuses_lengths_below_core():
    ring = {"mode": "ring", "ring1": 4, "ring2": 6}
    assert resolve_bicyclic_anchors(12, {}, (), ring) is None
    assert resolve_bicyclic_anchors(13, {}, (), ring) == (0, 5, 12)


def test_ratio_layout_scales_and_keeps_terminal_anchor():
    ratio = {"mode": "ratio", "pct1": 20, "pct2": 50, "pct3": 100}
    assert resolve_bicyclic_anchors(20, {}, (), ratio) == (3, 9, 19)
    assert resolve_bicyclic_anchors(10, {}, (), ratio) == (1, 4, 9)


def test_ratio_layout_forward_fixes_spacing_or_refuses():
    tight = {"mode": "ratio", "pct1": 40, "pct2": 45, "pct3": 100}
    # 1-based (4, 6, 10) -> 0-based (3, 5, 9)
    assert resolve_bicyclic_anchors(10, {}, (), tight) == (3, 5, 9)
    assert resolve_bicyclic_anchors(5, {}, (), tight) is None


def test_ratio_layout_round_half_up_parity_with_frontend():
    half = {"mode": "ratio", "pct1": 25, "pct2": 50, "pct3": 100}
    # 1-based (3, 5, 10): 2.5 rounds UP (JS Math.round parity, not banker's)
    assert resolve_bicyclic_anchors(10, {}, (), half) == (2, 4, 9)


def test_malformed_layout_falls_back_to_auto():
    assert resolve_bicyclic_anchors(10, {}, (), {"mode": "ring"}) == \
        choose_bicyclic_anchors(10, {}, ())
    assert resolve_bicyclic_anchors(10, {}, (), None) == \
        choose_bicyclic_anchors(10, {}, ())
    assert resolve_bicyclic_anchors(10, {}, (), {"mode": "ratio", "pct1": "x"}) == \
        choose_bicyclic_anchors(10, {}, ())


def test_min_feasible_length_ratio():
    assert min_feasible_length_ratio(15, 50, 100) <= 8
    assert min_feasible_length_ratio(90, 95, 100) > 12


def test_apply_post_edit_uses_layout():
    ring = {"mode": "ring", "ring1": 4, "ring2": 6}

    class _Cfg:
        pass

    from peplm.loop.constraints import ConstraintPlan, Vocab  # noqa: F401
    pytest.importorskip("torch")
    from peplm.loop.constraints import plan_for_post_edit

    plan = plan_for_post_edit({}, None, (), allow_extra_cys=False,
                              cys_layout=ring)
    # 1-based [1, 6, 13] at L=13 -> 0-based (0, 5, 12); stray C scrubbed
    tokens = list("AGGCAGGAGGAGG")
    out = apply_post_edit(tokens, plan)
    assert [i for i, t in enumerate(out) if t == "C"] == [0, 5, 12]
