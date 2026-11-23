from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


def subtask_registry_key(parent_task_id: str) -> str:
    from backend.core.config import PEPTIDE_SUBTASK_REGISTRY_KEY_PREFIX

    return f"{PEPTIDE_SUBTASK_REGISTRY_KEY_PREFIX}{str(parent_task_id or '').strip() or 'unknown'}"


def register_subtask(parent_task_id: str, subtask_id: str) -> None:
    if not str(parent_task_id or "").strip() or not str(subtask_id or "").strip():
        return
    from gpu_manager import get_redis_client

    key = subtask_registry_key(parent_task_id)
    try:
        pipe = get_redis_client().pipeline()
        pipe.sadd(key, subtask_id)
        pipe.expire(key, 14 * 24 * 3600)
        pipe.execute()
    except Exception as exc:
        print(f"[design] WARNING: 子任务登记失败（{exc}）；取消时看不到 {subtask_id}",
              file=sys.stderr)


def registered_subtasks(redis_client, parent_task_id: str) -> set:
    if not str(parent_task_id or "").strip():
        return set()
    return set(redis_client.smembers(subtask_registry_key(parent_task_id)) or set())


def forget_subtasks(parent_task_id: str) -> None:
    if not str(parent_task_id or "").strip():
        return
    from gpu_manager import get_redis_client

    try:
        get_redis_client().delete(subtask_registry_key(parent_task_id))
    except Exception as exc:
        print(f"[design] WARNING: 子任务登记表清理失败（{exc}）", file=sys.stderr)


def dispatch_window() -> int:
    raw = str(os.environ.get("PEPTIDE_CANDIDATE_DISPATCH_WINDOW", "") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    from gpu_manager import get_gpu_status

    return max(1, int(get_gpu_status().get("valid_count") or 1))


@dataclass(frozen=True)
class DispatchPolicy:
    fail_fast: bool
    submit_raises: bool


SCORE_CANDIDATES = DispatchPolicy(fail_fast=True, submit_raises=True)
REFINE_CANDIDATES = DispatchPolicy(fail_fast=False, submit_raises=False)


class SubtaskFailed(RuntimeError):
    def __init__(self, item, async_result, celery_state: str, detail) -> None:
        self.item = item
        self.async_result = async_result
        self.celery_state = celery_state
        self.detail = detail
        super().__init__(
            f"子任务 {getattr(async_result, 'id', '')} 以 {celery_state} 结束：{detail}")


def revoke(async_results) -> None:
    for async_result in async_results:
        try:
            async_result.revoke(terminate=True, signal="SIGTERM")
        except Exception as exc:
            print(f"[design] WARNING: 撤销子任务失败（{exc}）", file=sys.stderr)


def drain(
    items: list,
    submit: Callable[[Any], Any],
    parent_task_id: str,
    on_done: Callable[[Any, Any, str], None],
    policy: DispatchPolicy,
    on_progress: Optional[Callable[[dict], None]] = None,
    poll_interval: float = 0.8,
) -> list:
    pending = list(items)
    running: dict[str, tuple[Any, Any]] = {}
    finished: list = []
    first_failure: Optional[tuple] = None
    last_signature: Optional[tuple] = None
    window = dispatch_window()

    def fill() -> None:
        while pending and first_failure is None and len(running) < window:
            item = pending.pop(0)
            try:
                async_result = submit(item)
            except Exception as exc:
                if policy.submit_raises:
                    raise
                on_done(item, exc, "SUBMIT_FAILED")
                continue
            task_id = str(getattr(async_result, "id", "") or "")
            register_subtask(parent_task_id, task_id)
            running[task_id or str(id(item))] = (async_result, item)

    def report(counts: dict[str, str]) -> None:
        nonlocal last_signature
        if not callable(on_progress):
            return
        payload = {"total": len(items), "completed": len(finished)}
        for state in ("queued", "running", "failed"):
            payload[state] = sum(1 for value in counts.values() if value == state)
        signature = tuple(payload.values())
        if signature != last_signature:
            last_signature = signature
            on_progress(payload)

    fill()
    while pending or running:
        if not running:
            break
        counts: dict[str, str] = {}
        done: list[tuple[str, str, Any]] = []
        for key, (async_result, item) in running.items():
            state = str(getattr(async_result, "state", "") or "").upper()
            if state in {"SUCCESS", "FAILURE", "REVOKED"}:
                done.append((key, state, getattr(async_result, "result", None)))
            else:
                counts[key] = "queued" if state in {"PENDING", "RECEIVED", "RETRY"} else "running"

        if done:
            for key, state, result in done:
                async_result, item = running.pop(key)
                if state == "SUCCESS":
                    finished.append(item)
                else:
                    counts[key] = "failed"
                    if first_failure is None:
                        first_failure = (item, async_result, state, result)
                on_done(item, result, state)
            report(counts)
            fill()
            if first_failure is not None and policy.fail_fast:
                break
            continue

        report(counts)
        time.sleep(max(0.2, poll_interval))

    if first_failure is not None and policy.fail_fast:
        revoke(async_result for async_result, _item in running.values())
        raise SubtaskFailed(*first_failure)
    return finished
