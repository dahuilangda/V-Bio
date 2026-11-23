from __future__ import annotations

import pytest

from backend.runtime import candidate_pool
from backend.runtime.candidate_pool import (
    REFINE_CANDIDATES,
    SCORE_CANDIDATES,
    SubtaskFailed,
    drain,
    forget_subtasks,
    registered_subtasks,
    register_subtask,
    subtask_registry_key,
)

TERMINAL = {"SUCCESS", "FAILURE", "REVOKED"}


class FakeResult:
    def __init__(self, name: str, states: list[str]):
        self.id = name
        self._states = list(states)
        self.state = self._states.pop(0) if self._states else "PENDING"
        self.result = None
        self.revoked = False

    def advance(self) -> None:
        if self.state in TERMINAL:
            return
        self.state = self._states.pop(0) if self._states else "PENDING"
        if self.state == "SUCCESS":
            self.result = f"archive:{self.id}"

    def revoke(self, **_kwargs) -> None:
        self.revoked = True


class Harness:
    def __init__(self, monkeypatch, plan: dict[str, list[str] | None]):
        self.plan = plan
        self.submitted: list[str] = []
        self.results: dict[str, FakeResult] = {}
        self.outcomes: list[tuple[str, str, object]] = []
        self.registrations: list[tuple[str, str]] = []
        self.reports: list[dict] = []
        monkeypatch.setattr(candidate_pool.time, "sleep", lambda _seconds: self._tick())
        monkeypatch.setattr(candidate_pool, "register_subtask", self._register)

    def _tick(self) -> None:
        for result in self.results.values():
            result.advance()

    def _register(self, parent: str, subtask: str) -> None:
        self.registrations.append((parent, subtask))

    def submit(self, item: dict) -> FakeResult:
        name = item["name"]
        self.submitted.append(name)
        states = self.plan[name]
        if states is None:
            raise RuntimeError(f"submit failed: {name}")
        result = FakeResult(name, states)
        self.results[name] = result
        return result

    def on_done(self, item: dict, result, state: str) -> None:
        self.outcomes.append((item["name"], state, result))

    def run(self, names: str, policy, parent: str = "parent", **kwargs):
        return drain([{"name": n} for n in names], self.submit, parent, self.on_done, policy, **kwargs)


def test_window_limits_in_flight_and_refills_as_results_land(monkeypatch):
    monkeypatch.setenv("PEPTIDE_CANDIDATE_DISPATCH_WINDOW", "2")
    harness = Harness(monkeypatch, {n: ["SUCCESS"] for n in "abcd"})
    done = harness.run("abcd", SCORE_CANDIDATES)
    assert harness.submitted == ["a", "b", "c", "d"]
    assert [item["name"] for item in done] == ["a", "b", "c", "d"]


def test_window_applies_to_both_policies(monkeypatch):
    monkeypatch.setenv("PEPTIDE_CANDIDATE_DISPATCH_WINDOW", "2")
    harness = Harness(monkeypatch, {n: ["PENDING", "PENDING", "SUCCESS"] for n in "abcd"})
    harness.run("abcd", REFINE_CANDIDATES)
    assert harness.submitted == ["a", "b", "c", "d"]


def test_score_policy_first_failure_revokes_the_rest_and_raises(monkeypatch):
    monkeypatch.setenv("PEPTIDE_CANDIDATE_DISPATCH_WINDOW", "3")
    harness = Harness(monkeypatch, {"bad": ["FAILURE"], "slow": ["PENDING"]})
    with pytest.raises(SubtaskFailed, match="bad 以 FAILURE") as failure:
        harness.run(["bad", "slow"], SCORE_CANDIDATES)
    assert failure.value.celery_state == "FAILURE"
    assert failure.value.item == {"name": "bad"}
    assert harness.results["slow"].revoked is True
    assert [name for name, _state, _result in harness.outcomes] == ["bad"]


def test_refine_policy_isolates_failures_and_submit_errors(monkeypatch):
    harness = Harness(monkeypatch, {"boom": None, "bad": ["FAILURE"], "ok": ["PENDING", "SUCCESS"]})
    done = harness.run(["boom", "bad", "ok"], REFINE_CANDIDATES)
    assert [item["name"] for item in done] == ["ok"]
    by_name = {name: (state, result) for name, state, result in harness.outcomes}
    assert by_name["bad"][:2] == ("FAILURE", None)
    assert by_name["boom"][0] == "SUBMIT_FAILED" and isinstance(by_name["boom"][1], RuntimeError)
    assert by_name["ok"] == ("SUCCESS", "archive:ok")
    assert not any(result.revoked for result in harness.results.values())


def test_score_policy_lets_submit_errors_propagate(monkeypatch):
    harness = Harness(monkeypatch, {"boom": None})
    with pytest.raises(RuntimeError, match="submit failed"):
        harness.run(["boom"], SCORE_CANDIDATES)


def test_progress_reports_only_on_change(monkeypatch):
    harness = Harness(monkeypatch, {"a": ["PENDING", "PENDING", "SUCCESS"]})
    harness.run("a", SCORE_CANDIDATES, on_progress=harness.reports.append)
    assert harness.reports[0]["queued"] == 1
    assert harness.reports[-1] == {"total": 1, "completed": 1, "queued": 0, "running": 0, "failed": 0}
    assert len(harness.reports) == len({tuple(report.values()) for report in harness.reports})


def test_subtasks_are_registered_under_the_parent(monkeypatch):
    harness = Harness(monkeypatch, {"a": ["SUCCESS"]})
    harness.run("a", SCORE_CANDIDATES, parent="parent-1")
    assert harness.registrations == [("parent-1", "a")]


def test_window_defaults_to_gpu_count(monkeypatch):
    monkeypatch.delenv("PEPTIDE_CANDIDATE_DISPATCH_WINDOW", raising=False)
    monkeypatch.setattr("gpu_manager.get_gpu_status", lambda: {"valid_count": 7})
    assert candidate_pool.dispatch_window() == 7
    monkeypatch.setenv("PEPTIDE_CANDIDATE_DISPATCH_WINDOW", "3")
    assert candidate_pool.dispatch_window() == 3


def test_registry_key():
    assert subtask_registry_key(" p1 ") == "boltz_peptide_subtasks:p1"
    assert subtask_registry_key("") == "boltz_peptide_subtasks:unknown"


def test_registration_writes_key_and_expiry(monkeypatch):
    calls: list[tuple] = []

    class Pipe:
        def sadd(self, *args):
            calls.append(("sadd", *args))

        def expire(self, *args):
            calls.append(("expire", *args))

        def execute(self):
            calls.append(("execute",))

    monkeypatch.setattr("gpu_manager.get_redis_client", lambda: type("Client", (), {"pipeline": lambda self: Pipe()})())
    register_subtask("p1", "s1")
    assert calls == [
        ("sadd", "boltz_peptide_subtasks:p1", "s1"),
        ("expire", "boltz_peptide_subtasks:p1", 14 * 24 * 3600),
        ("execute",),
    ]


def test_registration_failure_is_printed(monkeypatch, capsys):
    def broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr("gpu_manager.get_redis_client", broken)
    register_subtask("p1", "s1")
    assert "子任务登记失败" in capsys.readouterr().err


def test_registered_and_forgotten_subtasks(monkeypatch, capsys):
    class Client:
        def __init__(self):
            self.deleted = []

        def smembers(self, key):
            assert key == "boltz_peptide_subtasks:p1"
            return {"s1", "s2"}

        def delete(self, key):
            self.deleted.append(key)

    client = Client()
    assert registered_subtasks(client, "p1") == {"s1", "s2"}
    assert registered_subtasks(client, "") == set()
    monkeypatch.setattr("gpu_manager.get_redis_client", lambda: client)
    forget_subtasks("p1")
    forget_subtasks("")
    assert client.deleted == ["boltz_peptide_subtasks:p1"]

    def broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr("gpu_manager.get_redis_client", broken)
    forget_subtasks("p1")
    assert "子任务登记表清理失败" in capsys.readouterr().err


def test_revoking_registered_subtasks_reaches_the_registry_cleanup(monkeypatch):
    from backend.worker import tasks

    class Client:
        def __init__(self):
            self.deleted = []

        def smembers(self, key):
            assert key == "boltz_peptide_subtasks:parent-1"
            return {"cand-a", ""}

        def delete(self, key):
            self.deleted.append(key)

    revoked = []
    client = Client()
    monkeypatch.setattr(tasks, "get_redis_client", lambda: client)
    monkeypatch.setattr(tasks.celery_app.control, "revoke", lambda task_id, **kwargs: revoked.append((task_id, kwargs)))
    result = tasks._revoke_registered_peptide_subtasks("parent-1", terminate=True)
    assert result == {"found": ["cand-a"], "revoked": ["cand-a"], "failed": []}
    assert revoked == [("cand-a", {"terminate": True, "signal": "SIGTERM", "send_event": True})]
    assert client.deleted == ["boltz_peptide_subtasks:parent-1"]
