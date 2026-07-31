"""A general computation/derivation skill category for the Copilot.

The lookup skills fetch authoritative records; this category DERIVES values from them — grounding
arithmetic the model would otherwise approximate from memory (e.g. the mean IC50 across activities
returned by ``chembl.bioactivity``, the min pLDDT across ``alphafold.resolve`` hits). It is a new
*kind* of read-only skill: pure local computation, no network, no code execution.

Registered onto the shared read-skill registry (``OnlineDatabaseSkills``) so the harness treats a
compute skill exactly like any other read skill — schema-audited, thread-pool-executed, its result
available to the next round and to ``copilot_memory``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from management_api.copilot_skills.online_databases import OnlineSkillDefinition


def _coerce_number(item: Any) -> float | None:
    if isinstance(item, bool):
        return None
    try:
        value = float(item)
    except (TypeError, ValueError):
        return None
    # Reject NaN/Infinity — json.loads accepts them by default, but they would silently corrupt
    # the aggregate (nan min/max/mean). JSON Schema's "number" doesn't permit them either.
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def aggregate(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Summary statistics over a list of numeric values (count/sum/min/max/mean/median)."""
    raw = arguments.get("values")
    if not isinstance(raw, list):
        raise ValueError("compute.aggregate requires a 'values' array.")
    numbers: List[float] = [coerced for item in raw if (coerced := _coerce_number(item)) is not None]
    if not numbers:
        return {"source": "compute", "operation": "aggregate", "count": 0, "results": []}
    numbers.sort()
    count = len(numbers)
    midpoint = count // 2
    median = numbers[midpoint] if count % 2 else (numbers[midpoint - 1] + numbers[midpoint]) / 2
    total = sum(numbers)
    summary = {
        "count": count,
        "sum": total,
        "min": numbers[0],
        "max": numbers[-1],
        "mean": total / count,
        "median": median,
    }
    return {
        "source": "compute",
        "operation": "aggregate",
        "query": f"{count} values",
        "count": 1,
        "results": [summary],
    }


def register_compute_skills(skills: Any) -> None:
    """Register the compute skill category onto a read-skill registry."""
    skills.register(
        OnlineSkillDefinition(
            name="compute.aggregate",
            description=(
                "Compute summary statistics (count, sum, min, max, mean, median) over a list of numeric "
                "values. Use this to ground arithmetic over retrieved data instead of approximating it — "
                "e.g. the mean potency across activities returned by chembl.bioactivity. Pass the values "
                "explicitly, or consume a numeric column from a prior observation with "
                "{\"$fromObservation\": \"<id>\", \"field\": \"<field>\", \"all\": true} and depends_on that id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "values": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 1,
                        "maxItems": 1000,
                    }
                },
                "required": ["values"],
                "additionalProperties": False,
            },
        ),
        aggregate,
    )
