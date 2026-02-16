from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

_PROTENIX_SUMMARY_SAMPLE_RE = re.compile(r"_summary_confidence_sample_(\d+)\.json$", re.IGNORECASE)


class ProtenixResultError(RuntimeError):
    """Raised when Protenix result artifacts are incomplete or inconsistent."""


def _require_gemmi():
    try:
        import gemmi  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise ProtenixResultError(
            "gemmi is required for Protenix structure parsing. "
            "Run the worker with the project venv (e.g. ./venv/bin/python)."
        ) from exc
    return gemmi


def normalize_plddt_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    if 0.0 <= number <= 1.0:
        number *= 100.0
    return max(0.0, min(100.0, number))


def to_float_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    output: list[float] = []
    for item in value:
        try:
            number = float(item)
        except Exception:
            continue
        if math.isfinite(number):
            output.append(number)
    return output


def mean_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _resolve_best_summary(metrics_payload: dict[str, Any]) -> dict[str, Any]:
    best_summary = metrics_payload.get("best_summary")
    if isinstance(best_summary, dict):
        return best_summary
    raise ProtenixResultError("Missing best_summary in protenix2score_metrics.json.")


def _summary_to_structure_candidates(artifacts_root: Path, summary_rel: str) -> list[Path]:
    summary_path = artifacts_root / Path(summary_rel)
    if not summary_path.exists():
        raise ProtenixResultError(f"Summary file listed in metrics does not exist: {summary_rel}")

    candidates: list[Path] = []
    replaced_name = _PROTENIX_SUMMARY_SAMPLE_RE.sub(r"_sample_\1.cif", summary_path.name)
    if replaced_name != summary_path.name:
        candidates.append(summary_path.with_name(replaced_name))

    sample_match = _PROTENIX_SUMMARY_SAMPLE_RE.search(summary_path.name)
    if sample_match:
        sample_index = sample_match.group(1)
        for ext in (".cif", ".mmcif", ".pdb"):
            candidates.extend(sorted(summary_path.parent.glob(f"*_sample_{sample_index}{ext}")))

    deduped: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item.resolve()) if item.exists() else str(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def resolve_protenix_best_structure(output_dir: str | Path, metrics_payload: dict[str, Any]) -> Path:
    artifacts_root = Path(output_dir) / "protenix_output"
    if not artifacts_root.exists():
        raise ProtenixResultError(f"Protenix output directory does not exist: {artifacts_root}")

    best_summary = _resolve_best_summary(metrics_payload)
    summary_file = best_summary.get("file")
    if not isinstance(summary_file, str) or not summary_file.strip():
        raise ProtenixResultError("best_summary.file is missing in protenix2score_metrics.json.")

    candidates = _summary_to_structure_candidates(artifacts_root, summary_file.strip())
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    nearby_structures = sorted(
        str(path.relative_to(artifacts_root))
        for path in artifacts_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".cif", ".mmcif", ".pdb"}
    )[:20]
    raise ProtenixResultError(
        "Failed to map best_summary to a structure file. "
        f"summary={summary_file}; candidates={[str(item) for item in candidates]}; "
        f"nearby_structures={nearby_structures}"
    )


def extract_structure_confidence(structure_path: Path) -> dict[str, Any]:
    gemmi = _require_gemmi()
    try:
        structure = gemmi.read_structure(str(structure_path))
    except Exception as exc:  # noqa: BLE001
        raise ProtenixResultError(f"Failed to parse structure for confidence extraction: {structure_path}") from exc

    solvent_names = {"HOH", "WAT", "DOD", "SOL"}
    chain_ids: list[str] = []
    chain_atom_scores: dict[str, list[float]] = {}
    residue_scores_by_chain: dict[str, list[float]] = {}
    ligand_scores_by_chain: dict[str, list[float]] = {}
    polymer_chain_ids: list[str] = []
    all_atom_scores: list[float] = []
    polymer_atom_scores: list[float] = []

    for model in structure:
        for chain in model:
            chain_id = (chain.name or "").strip()
            if not chain_id:
                continue
            if chain_id not in chain_ids:
                chain_ids.append(chain_id)
            chain_atom_scores.setdefault(chain_id, [])
            residue_scores_by_chain.setdefault(chain_id, [])

            for residue in chain:
                resname = (residue.name or "").strip().upper()
                is_polymer = residue.het_flag == "A"
                is_ligand = residue.het_flag != "A" and resname not in solvent_names
                if is_polymer and chain_id not in polymer_chain_ids:
                    polymer_chain_ids.append(chain_id)
                if is_ligand:
                    ligand_scores_by_chain.setdefault(chain_id, [])

                residue_atom_scores: list[float] = []
                for atom in residue:
                    element = (atom.element.name or "").strip().upper()
                    atom_name = (atom.name or "").strip().upper()
                    if element in {"H", "D", "T"}:
                        continue
                    if not element and atom_name.startswith(("H", "D", "T")):
                        continue

                    score = normalize_plddt_value(getattr(atom, "b_iso", None))
                    if score is None:
                        continue
                    score = float(score)
                    score_rounded = round(score, 4)
                    all_atom_scores.append(score_rounded)
                    chain_atom_scores[chain_id].append(score)
                    residue_atom_scores.append(score_rounded)
                    if is_polymer:
                        polymer_atom_scores.append(score_rounded)
                    if is_ligand:
                        ligand_scores_by_chain[chain_id].append(score_rounded)
                residue_mean = mean_or_none(residue_atom_scores)
                if residue_mean is not None and is_polymer:
                    residue_scores_by_chain[chain_id].append(round(residue_mean, 4))

    payload: dict[str, Any] = {}
    if chain_ids:
        payload["chain_ids"] = chain_ids
    if polymer_chain_ids:
        payload["polymer_chain_ids"] = polymer_chain_ids

    chain_mean_plddt = {
        chain_id: round(mean_score, 4)
        for chain_id, values in chain_atom_scores.items()
        if (mean_score := mean_or_none(values)) is not None
    }
    if chain_mean_plddt:
        payload["chain_mean_plddt"] = chain_mean_plddt

    residue_plddt_by_chain = {
        chain_id: values
        for chain_id, values in residue_scores_by_chain.items()
        if values
    }
    if residue_plddt_by_chain:
        payload["residue_plddt_by_chain"] = residue_plddt_by_chain

    complex_plddt = mean_or_none(all_atom_scores)
    if complex_plddt is not None:
        payload["complex_plddt"] = round(complex_plddt, 4)
    complex_plddt_protein = mean_or_none(polymer_atom_scores)
    if complex_plddt_protein is not None:
        payload["complex_plddt_protein"] = round(complex_plddt_protein, 4)

    ligand_atom_plddts_by_chain = {
        chain_id: [round(v, 2) for v in values]
        for chain_id, values in ligand_scores_by_chain.items()
        if values
    }
    if ligand_atom_plddts_by_chain:
        payload["ligand_atom_plddts_by_chain"] = ligand_atom_plddts_by_chain
        first_chain = next(iter(ligand_atom_plddts_by_chain))
        payload["ligand_atom_plddts"] = ligand_atom_plddts_by_chain[first_chain]
        ligand_chain_ids = list(ligand_atom_plddts_by_chain.keys())
        payload["ligand_chain_ids"] = ligand_chain_ids
        if len(ligand_chain_ids) == 1:
            payload["ligand_chain_id"] = ligand_chain_ids[0]
        ligand_values: list[float] = []
        for values in ligand_atom_plddts_by_chain.values():
            ligand_values.extend(values)
        ligand_mean = mean_or_none(ligand_values)
        if ligand_mean is not None:
            ligand_mean = round(ligand_mean, 4)
            payload["ligand_mean_plddt"] = ligand_mean
            payload["ligand_plddt"] = ligand_mean

    return payload


def build_protenix_confidence_payload(
    metrics_payload: dict[str, Any],
    *,
    structure_confidence: dict[str, Any],
    preferred_ligand_chain: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"backend": "protenix"}
    mode = metrics_payload.get("mode")
    if isinstance(mode, str) and mode.strip():
        payload["mode"] = mode
    model_name = metrics_payload.get("model_name")
    if isinstance(model_name, str) and model_name.strip():
        payload["model_name"] = model_name

    best_summary = _resolve_best_summary(metrics_payload)
    for key in (
        "ranking_score",
        "plddt",
        "ptm",
        "iptm",
        "gpde",
        "chain_plddt",
        "chain_ptm",
        "chain_iptm",
        "chain_pair_iptm",
        "chain_pair_iptm_global",
    ):
        if key in best_summary:
            payload[key] = best_summary.get(key)

    chain_ids = structure_confidence.get("chain_ids")
    if isinstance(chain_ids, list) and chain_ids:
        payload["chain_ids"] = chain_ids

    chain_plddt_values = to_float_list(best_summary.get("chain_plddt"))
    if chain_plddt_values and isinstance(chain_ids, list) and len(chain_ids) == len(chain_plddt_values):
        chain_mean_plddt: dict[str, float] = {}
        for idx, chain_id in enumerate(chain_ids):
            if not isinstance(chain_id, str) or not chain_id.strip():
                continue
            normalized = normalize_plddt_value(chain_plddt_values[idx])
            if normalized is None:
                continue
            chain_mean_plddt[chain_id.strip()] = round(normalized, 4)
        if chain_mean_plddt:
            payload["chain_mean_plddt"] = chain_mean_plddt

    pair_matrix = best_summary.get("chain_pair_iptm")
    if not isinstance(pair_matrix, list):
        pair_matrix = best_summary.get("chain_pair_iptm_global")
    if isinstance(pair_matrix, list) and isinstance(chain_ids, list) and len(pair_matrix) == len(chain_ids):
        pair_map: dict[str, dict[str, float]] = {}
        for row_index, row in enumerate(pair_matrix):
            if not isinstance(row, list):
                continue
            chain_a = str(chain_ids[row_index]).strip()
            if not chain_a:
                continue
            for col_index, raw_value in enumerate(row):
                if col_index >= len(chain_ids):
                    break
                chain_b = str(chain_ids[col_index]).strip()
                if not chain_b:
                    continue
                try:
                    value = float(raw_value)
                except Exception:
                    continue
                if not math.isfinite(value):
                    continue
                pair_map.setdefault(chain_a, {})[chain_b] = value
        if pair_map:
            payload["pair_chains_iptm"] = pair_map

    for key in (
        "polymer_chain_ids",
        "complex_plddt",
        "complex_plddt_protein",
        "residue_plddt_by_chain",
        "ligand_atom_plddts_by_chain",
        "ligand_atom_plddts",
        "ligand_chain_ids",
        "ligand_chain_id",
        "ligand_mean_plddt",
        "ligand_plddt",
    ):
        value = structure_confidence.get(key)
        if value is not None:
            payload[key] = value

    preferred = str(preferred_ligand_chain or "").strip().upper()
    if preferred:
        by_chain = payload.get("ligand_atom_plddts_by_chain")
        if isinstance(by_chain, dict):
            selected_values = by_chain.get(preferred)
            selected_chain = preferred
            remapped_from_model_chain = ""
            if not isinstance(selected_values, list) or not selected_values:
                if len(by_chain) == 1:
                    only_chain = next(iter(by_chain.keys()))
                    only_values = by_chain.get(only_chain)
                    if isinstance(only_values, list) and only_values:
                        selected_values = only_values
                        selected_chain = preferred
                        remapped_from_model_chain = str(only_chain).strip()
                else:
                    selected_values = None

            if isinstance(selected_values, list) and selected_values:
                normalized_selected = [value for value in selected_values if isinstance(value, (int, float))]
                if normalized_selected:
                    payload["ligand_chain_id"] = selected_chain
                    payload["ligand_chain_ids"] = [selected_chain]
                    payload["ligand_atom_plddts"] = normalized_selected
                    payload["ligand_atom_plddts_by_chain"] = {selected_chain: normalized_selected}
                    if remapped_from_model_chain:
                        payload["requested_ligand_chain_id"] = preferred
                        payload["model_ligand_chain_id"] = remapped_from_model_chain
                    ligand_mean = mean_or_none([float(item) for item in normalized_selected])
                    if ligand_mean is not None:
                        ligand_mean = round(ligand_mean, 4)
                        payload["ligand_mean_plddt"] = ligand_mean
                        payload["ligand_plddt"] = ligand_mean

    if "chain_mean_plddt" not in payload:
        chain_mean_value = structure_confidence.get("chain_mean_plddt")
        if chain_mean_value is not None:
            payload["chain_mean_plddt"] = chain_mean_value

    ligand_chain_ids = payload.get("ligand_chain_ids")
    chain_mean_map = payload.get("chain_mean_plddt")
    ligand_mean_existing = normalize_plddt_value(payload.get("ligand_mean_plddt"))
    if (
        isinstance(ligand_chain_ids, list)
        and isinstance(chain_mean_map, dict)
        and (ligand_mean_existing is None or ligand_mean_existing <= 1.0)
    ):
        ligand_chain_means: list[float] = []
        for chain_id in ligand_chain_ids:
            value = chain_mean_map.get(chain_id) if isinstance(chain_id, str) else None
            normalized = normalize_plddt_value(value)
            if normalized is not None:
                ligand_chain_means.append(normalized)
        ligand_mean = mean_or_none(ligand_chain_means)
        if ligand_mean is not None:
            ligand_mean = round(ligand_mean, 4)
            payload["ligand_mean_plddt"] = ligand_mean
            payload["ligand_plddt"] = ligand_mean

    return payload


def collect_confidence_issues(metrics_payload: dict[str, Any], confidence_payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    best_summary = metrics_payload.get("best_summary")
    if isinstance(best_summary, dict):
        chain_plddt_values = to_float_list(best_summary.get("chain_plddt"))
        chain_ids = confidence_payload.get("chain_ids")
        if chain_plddt_values and isinstance(chain_ids, list) and len(chain_ids) != len(chain_plddt_values):
            issues.append(
                f"chain_plddt length ({len(chain_plddt_values)}) does not match chain_ids length ({len(chain_ids)})."
            )

    chain_ids = confidence_payload.get("chain_ids")
    polymer_chain_ids = confidence_payload.get("polymer_chain_ids")
    has_non_polymer_chain = (
        isinstance(chain_ids, list)
        and chain_ids
        and isinstance(polymer_chain_ids, list)
        and len(polymer_chain_ids) < len(chain_ids)
    )
    if has_non_polymer_chain:
        ligand_atom = confidence_payload.get("ligand_atom_plddts_by_chain")
        if not isinstance(ligand_atom, dict) or not ligand_atom:
            issues.append("missing ligand_atom_plddts_by_chain.")

        ligand_mean = normalize_plddt_value(confidence_payload.get("ligand_mean_plddt"))
        if ligand_mean is None:
            issues.append("missing ligand_mean_plddt.")

    residue_map = confidence_payload.get("residue_plddt_by_chain")
    if not isinstance(residue_map, dict) or not residue_map:
        issues.append("missing residue_plddt_by_chain.")

    return issues


def find_ligand_chain_and_resname(structure_path: Path, preferred_chain: str | None = None) -> tuple[str, str] | None:
    gemmi = _require_gemmi()
    try:
        structure = gemmi.read_structure(str(structure_path))
    except Exception as exc:  # noqa: BLE001
        raise ProtenixResultError(f"Failed to parse structure for ligand lookup: {structure_path}") from exc

    preferred = str(preferred_chain or "").strip()
    solvent_names = {"HOH", "WAT", "DOD"}
    for model in structure:
        chains = list(model)
        if preferred:
            preferred_matches = [chain for chain in chains if (chain.name or "").strip().lower() == preferred.lower()]
            chains = preferred_matches + [chain for chain in chains if chain not in preferred_matches]

        for chain in chains:
            chain_id = (chain.name or "").strip()
            for residue in chain:
                resname = (residue.name or "").strip()
                if not resname or resname.upper() in solvent_names:
                    continue
                if residue.het_flag == "A":
                    continue
                return chain_id, resname
    return None
