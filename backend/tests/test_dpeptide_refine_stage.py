"""Dispatch semantics of the per-candidate GPU refine stage executor.

The design loop's D-pocket/L-pocket/D-plain routes all dispatch one
protenix2dock refine per candidate; _run_dpeptide_refine_stage dispatches
every refine immediately (the shared GPU pool + worker concurrency are the
only bounds) and finalizes each candidate as its refine lands. A
per-candidate failure must reject only that candidate.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from backend.runtime.run_single_prediction import _run_dpeptide_refine_stage


class _FakeAsyncResult:
    def __init__(self, name: str, state_sequence: List[str]):
        self.id = name
        self._states = list(state_sequence)
        self.state = self._states.pop(0) if self._states else "PENDING"

    def tick(self) -> None:
        if self._states:
            self.state = self._states.pop(0)


def _patch_sleep(monkeypatch, results: List[_FakeAsyncResult]) -> List[float]:
    sleeps: List[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        for result in results:
            result.tick()

    monkeypatch.setattr(time, "sleep", fake_sleep)
    return sleeps


def test_all_refines_dispatch_immediately(monkeypatch):
    dispatch_order: List[str] = []
    live: List[str] = []
    peak = 0
    results: List[_FakeAsyncResult] = []
    finalized: List[str] = []

    def make_dispatch(name: str):
        def dispatch():
            dispatch_order.append(name)
            live.append(name)
            nonlocal peak
            peak = max(peak, len(live))
            result = _FakeAsyncResult(name, ["PENDING", "PENDING", "SUCCESS"])
            results.append(result)
            return result

        return dispatch

    def finalize_one_candidate(name: str):
        def finalize(ctx: Dict[str, Any]) -> None:
            live.remove(ctx["name"])
            finalized.append(ctx["name"])

        return finalize

    # No orchestrator window: every context dispatches before any lands.
    contexts = [
        {"name": f"cand-{i}", "dispatch": make_dispatch(f"cand-{i}")}
        for i in range(3)
    ]
    _patch_sleep(monkeypatch, results)
    _run_dpeptide_refine_stage(
        contexts,
        parent_task_id="parent-test",
        finalize_context=finalize_one_candidate("shared"),
        poll_interval=0.01,
    )
    assert peak == 3, f"every refine must be in flight at once (peak={peak})"
    assert dispatch_order == ["cand-0", "cand-1", "cand-2"]
    assert sorted(finalized) == ["cand-0", "cand-1", "cand-2"]


def test_dispatch_free_contexts_finalize_first_without_dispatch(monkeypatch):
    finalized: List[str] = []
    results: List[_FakeAsyncResult] = []

    def dispatch():
        result = _FakeAsyncResult("gpu-task", ["PENDING", "SUCCESS"])
        results.append(result)
        return result

    contexts = [
        {"name": "no-refine", "dispatch": None},
        {"name": "with-refine", "dispatch": dispatch},
    ]
    _patch_sleep(monkeypatch, results)
    _run_dpeptide_refine_stage(
        contexts,
        parent_task_id="parent-test",
        finalize_context=lambda ctx: finalized.append(ctx["name"]),
        poll_interval=0.01,
    )
    # The no-refine candidate finalizes without occupying a GPU task at all.
    assert finalized[0] == "no-refine"
    assert sorted(finalized) == ["no-refine", "with-refine"]


def test_failed_refine_rejects_only_its_candidate(monkeypatch):
    finalized: List[Dict[str, Any]] = []
    results: List[_FakeAsyncResult] = []

    def make_dispatch(name: str, states: List[str]):
        def dispatch():
            result = _FakeAsyncResult(name, states)
            results.append(result)
            return result

        return dispatch

    contexts = [
        {"name": "fails", "dispatch": make_dispatch("fails", ["PENDING", "FAILURE"])},
        {"name": "succeeds", "dispatch": make_dispatch("succeeds", ["PENDING", "SUCCESS"])},
    ]
    _patch_sleep(monkeypatch, results)
    _run_dpeptide_refine_stage(
        contexts,
        parent_task_id="parent-test",
        finalize_context=finalized.append,
        poll_interval=0.01,
    )
    by_name = {ctx["name"]: ctx for ctx in finalized}
    assert isinstance(by_name["fails"].get("refine_error"), RuntimeError)
    assert by_name["succeeds"].get("refine_error") is None


def test_dispatch_exception_rejects_candidate_and_others_still_run(monkeypatch):
    finalized: List[Dict[str, Any]] = []
    results: List[_FakeAsyncResult] = []

    def exploding_dispatch():
        raise RuntimeError("staging race")

    def good_dispatch():
        result = _FakeAsyncResult("good", ["PENDING", "SUCCESS"])
        results.append(result)
        return result

    contexts = [
        {"name": "explodes", "dispatch": exploding_dispatch},
        {"name": "good", "dispatch": good_dispatch},
    ]
    _patch_sleep(monkeypatch, results)
    _run_dpeptide_refine_stage(
        contexts,
        parent_task_id="parent-test",
        finalize_context=finalized.append,
        poll_interval=0.01,
    )
    by_name = {ctx["name"]: ctx for ctx in finalized}
    assert isinstance(by_name["explodes"].get("refine_error"), RuntimeError)
    assert by_name["good"].get("refine_error") is None
