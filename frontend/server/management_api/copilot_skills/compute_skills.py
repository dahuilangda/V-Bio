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
                "values. Use this to ground arithmetic over retrieved data instead of approximating it "
                "from memory. Pass the values explicitly, or consume a numeric column from a prior "
                "observation with {\"$fromObservation\": \"<id>\", \"field\": \"<field>\", \"all\": true} "
                "and depends_on that id."
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
    skills.register(
        OnlineSkillDefinition(
            name="compute.convert_units",
            description=(
                "Convert a concentration between pM/nM/uM(microM)/mM/M exactly. Never convert units "
                "in your head; pass the retrieved value through this skill. The value may consume "
                "a prior observation field via {\"$fromObservation\": \"<id>\", \"field\": \"<field>\", \"index\": <n>}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "number", "exclusiveMinimum": 0},
                    "from": {"type": "string", "enum": ["pM", "nM", "uM", "µM", "mM", "M"]},
                    "to": {"type": "string", "enum": ["pM", "nM", "uM", "µM", "mM", "M"]},
                },
                "required": ["value", "from", "to"],
                "additionalProperties": False,
            },
        ),
        convert_units,
    )
    skills.register(
        OnlineSkillDefinition(
            name="compute.sequence_stats",
            description=(
                "Compute local protein-sequence statistics: length, molecular weight (kDa), and residue "
                "class composition (hydrophobic/charged/aromatic/polar percent, most common residues). "
                "Use this to ground numeric claims about a retrieved sequence instead of estimating. The "
                "sequence may consume a prior observation field via {\"$fromObservation\": \"<id>\", "
                "\"field\": \"sequence\", \"index\": <n>}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sequence": {"type": "string", "minLength": 10, "maxLength": 100000},
                },
                "required": ["sequence"],
                "additionalProperties": False,
            },
        ),
        sequence_stats,
    )


# Average residue masses (Da) for the 20 standard amino acids plus water for the chain termini.
_RESIDUE_MASS_DA: Dict[str, float] = {
    "A": 71.0788, "R": 156.1875, "N": 114.1038, "D": 115.0886, "C": 103.1388,
    "E": 129.1155, "Q": 128.1307, "G": 57.0519, "H": 137.1411, "I": 113.1594,
    "L": 113.1594, "K": 128.1741, "M": 131.1926, "F": 147.1766, "P": 97.1167,
    "S": 87.0782, "T": 101.1051, "W": 186.2132, "Y": 163.1760, "V": 99.1326,
}
_WATER_MASS_DA = 18.01524
_HYDROPHOBIC = frozenset("AVILMFWC")
_CHARGED = frozenset("DEKRH")
_AROMATIC = frozenset("FWY")


def sequence_stats(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Local protein-sequence analysis: length, molecular weight, residue-class composition."""
    raw = str(arguments.get("sequence") or "").strip().upper()
    if len(raw) < 10:
        raise ValueError("compute.sequence_stats requires a sequence of at least 10 residues.")
    counts: Dict[str, int] = {}
    known = 0
    mass = _WATER_MASS_DA
    for residue in raw:
        if residue not in _RESIDUE_MASS_DA:
            continue
        known += 1
        counts[residue] = counts.get(residue, 0) + 1
        mass += _RESIDUE_MASS_DA[residue]
    if known == 0:
        raise ValueError("compute.sequence_stats found no standard residues in the sequence.")
    common = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    summary = {
        "length": len(raw),
        "molecularWeightKDa": round(mass / 1000.0, 2),
        "hydrophobicPercent": round(100.0 * sum(n for r, n in counts.items() if r in _HYDROPHOBIC) / known, 1),
        "chargedPercent": round(100.0 * sum(n for r, n in counts.items() if r in _CHARGED) / known, 1),
        "aromaticPercent": round(100.0 * sum(n for r, n in counts.items() if r in _AROMATIC) / known, 1),
        "mostCommonResidues": ", ".join(f"{residue}({100.0 * n / known:.1f}%)" for residue, n in common),
    }
    if known != len(raw):
        summary["nonStandardResidues"] = len(raw) - known
    return {
        "source": "compute",
        "operation": "sequence_stats",
        "query": f"{len(raw)}-residue sequence",
        "count": 1,
        "results": [summary],
    }


# Canonical unit factors to mol/L. "uM" and "µM" both mean micromolar.
_UNIT_FACTOR: Dict[str, float] = {
    "pm": 1e-12, "nm": 1e-9, "um": 1e-6, "µm": 1e-6, "mm": 1e-3, "m": 1.0,
}
_UNIT_ALIASES = {"pm": "pM", "nm": "nM", "um": "µM", "µm": "µM", "mm": "mM", "m": "M"}


def convert_units(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Exact concentration unit conversion (pM/nM/µM/mM/M)."""
    value = _coerce_number(arguments.get("value"))
    if value is None or value <= 0:
        raise ValueError("compute.convert_units requires a positive numeric 'value'.")
    source = _UNIT_FACTOR.get(str(arguments.get("from") or "").strip().lower())
    target = _UNIT_FACTOR.get(str(arguments.get("to") or "").strip().lower())
    source_name = _UNIT_ALIASES.get(str(arguments.get("from") or "").strip().lower())
    target_name = _UNIT_ALIASES.get(str(arguments.get("to") or "").strip().lower())
    if source is None or target is None:
        raise ValueError("compute.convert_units requires 'from'/'to' in pM, nM, uM, mM, M.")
    converted = value * source / target
    summary = {
        "value": value,
        "from": source_name,
        "to": target_name,
        "result": float(f"{converted:.6g}"),
        "text": f"{value:g} {source_name} = {converted:.6g} {target_name}",
    }
    return {
        "source": "compute",
        "operation": "convert_units",
        "query": summary["text"],
        "count": 1,
        "results": [summary],
    }
