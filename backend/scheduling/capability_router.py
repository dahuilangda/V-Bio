from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable

CAPABILITY_QUEUE_PREFIX = "cap"
CAPABILITY_QUEUE_PRIORITIES = ("high", "default")

# Capability names are intentionally stable because they are encoded in queue names.
KNOWN_CAPABILITIES: tuple[str, ...] = (
    "boltz2",
    "alphafold3",
    "protenix",
    "pocketxmol",
    "affinity",
    "boltz2score",
    "lead_opt",
    "peptide_design",
)

GPU_CAPABILITIES = {
    "boltz2",
    "alphafold3",
    "protenix",
    "pocketxmol",
    "affinity",
    "boltz2score",
}
CPU_CAPABILITIES = {"lead_opt", "peptide_design"}

_CAPABILITY_ALIASES = {
    "boltz": "boltz2",
    "boltz2": "boltz2",
    "predict_boltz": "boltz2",
    "alphafold3": "alphafold3",
    "af3": "alphafold3",
    "protenix": "protenix",
    "pocketxmol": "pocketxmol",
    "affinity": "affinity",
    "boltz2score": "boltz2score",
    "score": "boltz2score",
    "lead_opt": "lead_opt",
    "leadopt": "lead_opt",
    "lead-optimization": "lead_opt",
    "lead_optimization": "lead_opt",
    "mmp": "lead_opt",
    "peptide_design": "peptide_design",
    "peptide-design": "peptide_design",
    "peptide": "peptide_design",
    "peptide_designer": "peptide_design",
    "designer": "peptide_design",
}

_GROUP_ALIASES = {
    "all": tuple(KNOWN_CAPABILITIES),
    "all_gpu": tuple(sorted(GPU_CAPABILITIES)),
    "all_cpu": tuple(sorted(CPU_CAPABILITIES)),
}

_PREDICT_BACKEND_RE = re.compile(r"""["']backend["']\s*:\s*["']([a-zA-Z0-9_\-]+)["']""")
_TASK_NAME_CAPABILITY_FALLBACK = {
    "affinity_task": "affinity",
    "boltz2score_task": "boltz2score",
    "lead_optimization_mmp_query_task": "lead_opt",
    "lead_optimization_task": "lead_opt",
}
_MAX_TASK_DETAILS_PER_WORKER = 128
_MAX_TASK_DETAILS_PER_CAPABILITY = 256


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _truncate_text(value: Any, limit: int = 400) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    keep = max(0, limit - 24)
    return text[:keep] + f"...[+{len(text) - keep} chars]"


def _extract_worker_host(worker_name: str) -> str:
    token = str(worker_name or "").strip()
    if "@" not in token:
        return token
    return token.split("@", 1)[1].strip()


def _infer_worker_type(worker_name: str, capabilities: list[str]) -> str:
    token = str(worker_name or "").strip().lower()
    if token.startswith("gpu@"):
        return "gpu"
    if token.startswith("cpu@"):
        return "cpu"
    cap_set = set(capabilities)
    if cap_set and cap_set.issubset(CPU_CAPABILITIES):
        return "cpu"
    if cap_set and cap_set.issubset(GPU_CAPABILITIES):
        return "gpu"
    return "mixed"


def _infer_task_capability(task_name: str, args_repr: str, kwargs_repr: str, fallback_capabilities: list[str]) -> str | None:
    normalized_task_name = str(task_name or "").strip()
    short_name = normalized_task_name.rsplit(".", 1)[-1]
    mapped = _TASK_NAME_CAPABILITY_FALLBACK.get(short_name)
    if mapped:
        return mapped
    if short_name == "predict_task":
        merged = f"{args_repr} {kwargs_repr}"
        backend_match = _PREDICT_BACKEND_RE.search(merged)
        if backend_match:
            return capability_from_prediction_backend(backend_match.group(1))
        if len(fallback_capabilities) == 1:
            return fallback_capabilities[0]
    return None


def _build_task_brief(task_item: dict[str, Any], *, state: str, fallback_capabilities: list[str]) -> dict[str, Any]:
    task = dict(task_item or {}) if isinstance(task_item, dict) else {}
    if state == "SCHEDULED" and isinstance(task.get("request"), dict):
        request_payload = task.get("request") or {}
        task = dict(request_payload)
        task["_eta"] = task_item.get("eta")
        task["_priority"] = task_item.get("priority")

    task_id = str(task.get("id") or task.get("uuid") or "").strip()
    task_name = str(task.get("name") or "").strip()
    args_repr = _truncate_text(task.get("args"), 600)
    kwargs_repr = _truncate_text(task.get("kwargs"), 600)
    delivery_info = task.get("delivery_info") if isinstance(task.get("delivery_info"), dict) else {}
    queue_name = str(
        delivery_info.get("routing_key")
        or delivery_info.get("exchange")
        or delivery_info.get("queue")
        or ""
    ).strip()

    inferred_capability = None
    parsed_queue = parse_capability_queue(queue_name)
    if parsed_queue:
        inferred_capability = parsed_queue[0]
    if not inferred_capability:
        inferred_capability = _infer_task_capability(task_name, args_repr, kwargs_repr, fallback_capabilities)

    time_start = task.get("time_start")
    runtime_seconds = None
    start_time_iso = None
    if isinstance(time_start, (int, float)) and float(time_start) > 0:
        now_ts = datetime.now(timezone.utc).timestamp()
        runtime_seconds = max(0.0, now_ts - float(time_start))
        start_time_iso = datetime.fromtimestamp(float(time_start), tz=timezone.utc).isoformat()

    eta_raw = task.get("_eta") if state == "SCHEDULED" else None
    eta_text = str(eta_raw).strip() if eta_raw is not None else ""

    return {
        "id": task_id,
        "name": task_name,
        "capability": inferred_capability,
        "state": state,
        "queue": queue_name,
        "args": args_repr,
        "kwargs": kwargs_repr,
        "runtime_seconds": runtime_seconds,
        "time_start": start_time_iso,
        "eta": eta_text or None,
    }


def normalize_priority(priority: str | None) -> str:
    token = str(priority or "").strip().lower()
    return "high" if token == "high" else "default"


def normalize_capability(capability: str | None) -> str | None:
    token = str(capability or "").strip().lower()
    if not token:
        return None
    return _CAPABILITY_ALIASES.get(token)


def capability_from_prediction_backend(backend: str | None) -> str:
    normalized = normalize_capability(str(backend or "").strip().lower())
    if normalized in {"boltz2", "alphafold3", "protenix", "pocketxmol"}:
        return normalized
    return "boltz2"


def build_capability_queue(capability: str, priority: str | None = None) -> str:
    normalized_capability = normalize_capability(capability)
    if not normalized_capability:
        raise ValueError(f"Unsupported capability '{capability}'.")
    normalized_priority = normalize_priority(priority)
    return f"{CAPABILITY_QUEUE_PREFIX}.{normalized_capability}.{normalized_priority}"


def parse_capability_queue(queue_name: str | None) -> tuple[str, str] | None:
    token = str(queue_name or "").strip().lower()
    if not token:
        return None
    prefix = f"{CAPABILITY_QUEUE_PREFIX}."
    if not token.startswith(prefix):
        return None
    fragments = token.split(".")
    if len(fragments) != 3:
        return None
    _, capability_token, priority_token = fragments
    normalized_capability = normalize_capability(capability_token)
    if not normalized_capability:
        return None
    normalized_priority = normalize_priority(priority_token)
    return normalized_capability, normalized_priority


def parse_capability_tokens(raw: str | None, *, worker_type: str = "gpu") -> list[str]:
    normalized_raw = str(raw or "").replace(";", ",").replace("；", ",")
    tokens = [part.strip().lower() for part in normalized_raw.split(",") if part.strip()]
    if not tokens:
        if str(worker_type).strip().lower() == "cpu":
            return sorted(CPU_CAPABILITIES)
        return sorted(GPU_CAPABILITIES)

    capabilities: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        expanded = _GROUP_ALIASES.get(token)
        candidate_tokens: Iterable[str]
        if expanded:
            candidate_tokens = expanded
        else:
            candidate_tokens = (token,)
        for candidate in candidate_tokens:
            normalized = normalize_capability(candidate)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            capabilities.append(normalized)
    return capabilities


def build_worker_queue_list(
    *,
    config_module,
    worker_type: str,
    raw_capabilities: str | None,
    include_high_priority: bool = True,
) -> list[str]:
    normalized_type = str(worker_type or "").strip().lower()
    capabilities = parse_capability_tokens(raw_capabilities, worker_type=normalized_type)
    queues: list[str] = []
    seen: set[str] = set()

    def _append(queue_name: str) -> None:
        if not queue_name or queue_name in seen:
            return
        seen.add(queue_name)
        queues.append(queue_name)

    for capability in capabilities:
        priorities = CAPABILITY_QUEUE_PRIORITIES
        if normalized_type == "cpu" and include_high_priority is False:
            priorities = ("default",)
        for priority in priorities:
            _append(build_capability_queue(capability, priority))

    return queues


def resolve_queue_for_capability(
    *,
    capability: str | None,
    priority: str | None,
    has_worker_for_queue_fn: Callable[[str], bool],
) -> Dict[str, Any]:
    normalized_priority = normalize_priority(priority)
    normalized_capability = normalize_capability(capability)
    if not normalized_capability:
        return {
            "queue": "",
            "capability": None,
            "priority": normalized_priority,
            "online": False,
            "selected_via": "unsupported_capability",
            "warning": f"Unsupported capability '{capability}'.",
            "checked_queues": [],
        }

    preferred_queues: list[str] = [build_capability_queue(normalized_capability, normalized_priority)]
    if normalized_priority == "high":
        preferred_queues.append(build_capability_queue(normalized_capability, "default"))

    for queue_name in preferred_queues:
        if has_worker_for_queue_fn(queue_name):
            return {
                "queue": queue_name,
                "capability": normalized_capability,
                "priority": normalized_priority,
                "online": True,
                "selected_via": "capability_queue",
                "checked_queues": preferred_queues,
            }

    warning = (
        f"No online worker detected for capability '{normalized_capability}' "
        f"(checked queues: {', '.join(preferred_queues)})."
    )
    return {
        "queue": preferred_queues[0],
        "capability": normalized_capability,
        "priority": normalized_priority,
        "online": False,
        "selected_via": "capability_queue_unavailable",
        "warning": warning,
        "checked_queues": preferred_queues,
    }


def list_known_queues() -> list[str]:
    queues = []
    for capability in KNOWN_CAPABILITIES:
        for priority in CAPABILITY_QUEUE_PRIORITIES:
            queues.append(build_capability_queue(capability, priority))
    return queues


def build_worker_capability_snapshot(*, celery_app, logger) -> Dict[str, Any]:
    try:
        inspector = celery_app.control.inspect(timeout=1.5)
        active_queues = inspector.active_queues() or {}
        active_tasks_by_worker = inspector.active() or {}
        reserved_tasks_by_worker = inspector.reserved() or {}
        scheduled_tasks_by_worker = inspector.scheduled() or {}
        worker_stats = inspector.stats() or {}
        worker_registered = inspector.registered() or {}
    except Exception as exc:
        logger.warning("Failed to inspect worker capability queues: %s", exc)
        return {"workers": {}, "capabilities": {}, "error": str(exc)}

    worker_names = sorted(
        set(active_queues.keys())
        | set(active_tasks_by_worker.keys())
        | set(reserved_tasks_by_worker.keys())
        | set(scheduled_tasks_by_worker.keys())
        | set(worker_stats.keys())
        | set(worker_registered.keys())
    )

    workers: Dict[str, Dict[str, Any]] = {}
    capability_summary: Dict[str, Dict[str, Any]] = {
        capability: {
            "online": False,
            "workers": [],
            "worker_count": 0,
            "max_running_tasks_upper_bound": 0,
            "gpu_slots_total": 0,
            "cpu_slots_total": 0,
            "active_tasks_count": 0,
            "reserved_tasks_count": 0,
            "scheduled_tasks_count": 0,
            "active_tasks": [],
            "reserved_tasks": [],
            "scheduled_tasks": [],
            "tasks_truncated": {
                "active": False,
                "reserved": False,
                "scheduled": False,
            },
            # Estimate only; if one worker serves multiple capabilities, totals overlap.
            "executed_tasks_total_estimate": 0,
            "executed_by_task_name_estimate": {},
        }
        for capability in KNOWN_CAPABILITIES
    }

    total_slots = 0
    total_busy = 0
    total_gpu_slots = 0
    total_cpu_slots = 0

    for worker_name in worker_names:
        queues = active_queues.get(worker_name) or []
        queue_names: list[str] = []
        worker_capabilities: set[str] = set()
        for queue in queues or []:
            queue_name = str((queue or {}).get("name") or "").strip()
            if not queue_name:
                continue
            queue_names.append(queue_name)
            parsed = parse_capability_queue(queue_name)
            if parsed:
                worker_capabilities.add(parsed[0])
        sorted_capabilities = sorted(worker_capabilities)

        worker_stats_payload = worker_stats.get(worker_name) if isinstance(worker_stats.get(worker_name), dict) else {}
        pool_info = worker_stats_payload.get("pool") if isinstance(worker_stats_payload.get("pool"), dict) else {}
        max_concurrency = _safe_int(pool_info.get("max-concurrency"), 0)
        if max_concurrency <= 0 and isinstance(pool_info.get("processes"), list):
            max_concurrency = len(pool_info.get("processes") or [])

        raw_active_tasks = active_tasks_by_worker.get(worker_name) if isinstance(active_tasks_by_worker.get(worker_name), list) else []
        raw_reserved_tasks = reserved_tasks_by_worker.get(worker_name) if isinstance(reserved_tasks_by_worker.get(worker_name), list) else []
        raw_scheduled_tasks = scheduled_tasks_by_worker.get(worker_name) if isinstance(scheduled_tasks_by_worker.get(worker_name), list) else []

        active_tasks_all = [
            _build_task_brief(task_row, state="ACTIVE", fallback_capabilities=sorted_capabilities)
            for task_row in raw_active_tasks
            if isinstance(task_row, dict)
        ]
        reserved_tasks_all = [
            _build_task_brief(task_row, state="RESERVED", fallback_capabilities=sorted_capabilities)
            for task_row in raw_reserved_tasks
            if isinstance(task_row, dict)
        ]
        scheduled_tasks_all = [
            _build_task_brief(task_row, state="SCHEDULED", fallback_capabilities=sorted_capabilities)
            for task_row in raw_scheduled_tasks
            if isinstance(task_row, dict)
        ]

        active_count = len(active_tasks_all)
        reserved_count = len(reserved_tasks_all)
        scheduled_count = len(scheduled_tasks_all)
        active_tasks = active_tasks_all[:_MAX_TASK_DETAILS_PER_WORKER]
        reserved_tasks = reserved_tasks_all[:_MAX_TASK_DETAILS_PER_WORKER]
        scheduled_tasks = scheduled_tasks_all[:_MAX_TASK_DETAILS_PER_WORKER]

        slots_total = max_concurrency if max_concurrency > 0 else max(active_count + reserved_count + scheduled_count, 0)
        slots_busy = active_count
        slots_idle = max(0, slots_total - slots_busy)
        slot_utilization = (float(slots_busy) / float(slots_total)) if slots_total > 0 else 0.0

        worker_type = _infer_worker_type(worker_name, sorted_capabilities)
        gpu_slots_total = slots_total if worker_type == "gpu" else 0
        cpu_slots_total = slots_total if worker_type == "cpu" else 0

        executed_by_task_name: Dict[str, int] = {}
        total_counter_payload = worker_stats_payload.get("total")
        if isinstance(total_counter_payload, dict):
            for task_name, task_count in total_counter_payload.items():
                executed_by_task_name[str(task_name)] = _safe_int(task_count, 0)
        executed_total = sum(executed_by_task_name.values())

        total_slots += slots_total
        total_busy += slots_busy
        total_gpu_slots += gpu_slots_total
        total_cpu_slots += cpu_slots_total

        registered_payload = worker_registered.get(worker_name)
        registered_list = registered_payload if isinstance(registered_payload, list) else []

        worker_payload = {
            "server": worker_name,
            "host": _extract_worker_host(worker_name),
            "worker_type": worker_type,
            "queues": sorted(set(queue_names)),
            "capabilities": sorted_capabilities,
            "capability_count": len(sorted_capabilities),
            "resources": {
                "slots_total": slots_total,
                "slots_busy": slots_busy,
                "slots_idle": slots_idle,
                "gpu_slots_total": gpu_slots_total,
                "cpu_slots_total": cpu_slots_total,
            },
            "utilization": {
                "slot_utilization": slot_utilization,
            },
            "tasks": {
                "active": active_tasks,
                "reserved": reserved_tasks,
                "scheduled": scheduled_tasks,
            },
            "tasks_truncated": {
                "active": active_count > len(active_tasks),
                "reserved": reserved_count > len(reserved_tasks),
                "scheduled": scheduled_count > len(scheduled_tasks),
            },
            "task_counts": {
                "active": active_count,
                "reserved": reserved_count,
                "scheduled": scheduled_count,
            },
            "task_counters": {
                "executed_total_since_start": executed_total,
                "executed_by_task_name": executed_by_task_name,
            },
            "worker_stats": {
                "uptime_seconds": _safe_int(worker_stats_payload.get("uptime"), 0),
                "pid": _safe_int(worker_stats_payload.get("pid"), 0),
                "clock": _safe_int(worker_stats_payload.get("clock"), 0),
            },
            "registered_task_count": len(registered_list),
            "registered_tasks_sample": [
                str(item).strip()
                for item in registered_list[:32]
                if str(item).strip()
            ],
        }
        workers[worker_name] = worker_payload

        for capability in sorted_capabilities:
            cap_payload = capability_summary.get(capability)
            if not isinstance(cap_payload, dict):
                continue
            cap_payload["workers"].append(worker_name)
            cap_payload["worker_count"] = len(cap_payload["workers"])
            cap_payload["online"] = bool(cap_payload["worker_count"])
            cap_payload["max_running_tasks_upper_bound"] += slots_total
            cap_payload["gpu_slots_total"] += gpu_slots_total
            cap_payload["cpu_slots_total"] += cpu_slots_total
            cap_payload["executed_tasks_total_estimate"] += executed_total

            for task_name, task_count in executed_by_task_name.items():
                current_count = _safe_int(cap_payload["executed_by_task_name_estimate"].get(task_name), 0)
                cap_payload["executed_by_task_name_estimate"][task_name] = current_count + _safe_int(task_count, 0)

            for task_payload in active_tasks_all:
                if task_payload.get("capability") == capability:
                    if len(cap_payload["active_tasks"]) < _MAX_TASK_DETAILS_PER_CAPABILITY:
                        cap_payload["active_tasks"].append(task_payload)
                    else:
                        cap_payload["tasks_truncated"]["active"] = True
                    cap_payload["active_tasks_count"] += 1
            for task_payload in reserved_tasks_all:
                if task_payload.get("capability") == capability:
                    if len(cap_payload["reserved_tasks"]) < _MAX_TASK_DETAILS_PER_CAPABILITY:
                        cap_payload["reserved_tasks"].append(task_payload)
                    else:
                        cap_payload["tasks_truncated"]["reserved"] = True
                    cap_payload["reserved_tasks_count"] += 1
            for task_payload in scheduled_tasks_all:
                if task_payload.get("capability") == capability:
                    if len(cap_payload["scheduled_tasks"]) < _MAX_TASK_DETAILS_PER_CAPABILITY:
                        cap_payload["scheduled_tasks"].append(task_payload)
                    else:
                        cap_payload["tasks_truncated"]["scheduled"] = True
                    cap_payload["scheduled_tasks_count"] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "worker_count": len(workers),
        "summary": {
            "workers_total": len(workers),
            "capabilities_total": len(capability_summary),
            "capabilities_online": sum(1 for row in capability_summary.values() if row.get("online")),
            "slots_total": total_slots,
            "slots_busy": total_busy,
            "slots_idle": max(0, total_slots - total_busy),
            "gpu_slots_total": total_gpu_slots,
            "cpu_slots_total": total_cpu_slots,
        },
        "workers": workers,
        # Alias kept for dashboard naming consistency.
        "servers": workers,
        "capabilities": capability_summary,
    }
