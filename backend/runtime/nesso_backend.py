from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Optional

import yaml
from rdkit import Chem

from backend.core.config import (
    NESSO_CONTAINER_CACHE_DIR,
    NESSO_DOCKER_EXTRA_ARGS,
    NESSO_DOCKER_IMAGE,
    NESSO_HOST_CACHE_DIR,
    NESSO_MODEL_REVISION,
    NESSO_NO_KERNELS,
    NESSO_NUM_WORKERS,
    NESSO_PRECISION,
    NESSO_RECYCLING_STEPS,
)
from backend.services.common_utils import coerce_bool


_SUPPORTED_SEQUENCE_TYPES = {"protein", "ligand"}
_MAX_SCREENING_COMPOUNDS = 200
_PASSTHROUGH_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HF_ENDPOINT",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
)
_SENSITIVE_ENV_KEY_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "ACCESS_KEY")
_OPTIONAL_AFFINITY_NUMERIC_FIELDS = (
    "affinity_pred_value1",
    "affinity_pred_value2",
    "affinity_logits_binary",
    "affinity_probability_binary",
    "entropy_pp",
    "entropy_pl",
    "entropy_ll",
    "entropy_crop_pp",
    "entropy_crop_pl",
    "entropy_crop_ll",
)


def _normalize_chain_ids(value: Any, *, context: str) -> list[str]:
    if isinstance(value, str):
        chain_ids = [value.strip()]
    elif isinstance(value, (list, tuple)):
        chain_ids = [str(item or "").strip() for item in value]
    else:
        raise ValueError(f"{context}.id must be a chain id string or a list of chain ids.")

    if not chain_ids or any(not chain_id for chain_id in chain_ids):
        raise ValueError(f"{context}.id must contain at least one non-empty chain id.")
    if len(set(chain_ids)) != len(chain_ids):
        raise ValueError(f"{context}.id contains duplicate chain ids.")
    return chain_ids


def _property_entries(raw_value: Any) -> list[dict[str, Any]]:
    if isinstance(raw_value, dict):
        return [raw_value]
    if isinstance(raw_value, list):
        return [item for item in raw_value if isinstance(item, dict)]
    return []


def normalize_nesso_input_yaml(yaml_content: str) -> tuple[str, dict[str, Any]]:
    """Convert the V-Bio prediction YAML into Nesso's sequence/affinity schema."""
    try:
        raw_data = yaml.safe_load(yaml_content) or {}
    except Exception as exc:
        raise ValueError(f"Nesso input is not valid YAML: {exc}") from exc
    if not isinstance(raw_data, dict):
        raise ValueError("Nesso input must be a YAML mapping.")
    if raw_data.get("version", 1) != 1:
        raise ValueError("Nesso supports YAML schema version 1 only.")

    raw_sequences = raw_data.get("sequences")
    if not isinstance(raw_sequences, list) or not raw_sequences:
        raise ValueError("Nesso input must contain a non-empty sequences list.")

    if raw_data.get("constraints"):
        raise ValueError("Nesso does not support prediction constraints; remove constraints for backend=nesso.")
    if raw_data.get("templates"):
        raise ValueError("Nesso does not support structure templates; remove templates for backend=nesso.")

    normalized_sequences: list[dict[str, Any]] = []
    occupied_chain_ids: set[str] = set()
    protein_chain_ids: list[str] = []
    ligand_chain_ids: list[str] = []
    ligand_sources: dict[str, dict[str, str]] = {}

    for sequence_index, raw_item in enumerate(raw_sequences, start=1):
        if not isinstance(raw_item, dict) or not raw_item:
            raise ValueError(f"Nesso sequence entry {sequence_index} must be a mapping.")

        supported_keys = [key for key in raw_item if str(key).lower() in _SUPPORTED_SEQUENCE_TYPES]
        if len(supported_keys) != 1:
            declared = ", ".join(str(key) for key in raw_item) or "<empty>"
            raise ValueError(
                f"Nesso sequence entry {sequence_index} must contain exactly one protein or ligand "
                f"entity (received: {declared})."
            )

        source_key = supported_keys[0]
        kind = str(source_key).lower()
        raw_block = raw_item.get(source_key)
        if not isinstance(raw_block, dict):
            raise ValueError(f"Nesso {kind} entry {sequence_index} must be a mapping.")

        chain_ids = _normalize_chain_ids(raw_block.get("id"), context=f"Nesso {kind} entry {sequence_index}")
        duplicates = [chain_id for chain_id in chain_ids if chain_id in occupied_chain_ids]
        if duplicates:
            raise ValueError(f"Nesso input reuses chain id(s): {', '.join(duplicates)}.")
        occupied_chain_ids.update(chain_ids)
        id_value: str | list[str] = chain_ids[0] if len(chain_ids) == 1 else chain_ids

        if kind == "protein":
            sequence = re.sub(r"\s+", "", str(raw_block.get("sequence") or "")).upper()
            if not sequence:
                raise ValueError(f"Nesso protein entry {sequence_index} is missing sequence.")
            if not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", sequence):
                raise ValueError(
                    f"Nesso protein entry {sequence_index} contains non-standard amino acids; "
                    "use only ACDEFGHIKLMNPQRSTVWY."
                )
            if raw_block.get("modifications"):
                raise ValueError("Nesso does not support protein modifications.")
            if coerce_bool(raw_block.get("cyclic"), False):
                raise ValueError("Nesso does not support cyclic proteins.")

            protein_block: dict[str, Any] = {"id": id_value, "sequence": sequence}
            pocket_mask = str(raw_block.get("pocket_mask") or "").strip()
            if pocket_mask:
                protein_block["pocket_mask"] = pocket_mask
            normalized_sequences.append({"protein": protein_block})
            protein_chain_ids.extend(chain_ids)
            continue

        ligand_keys = [
            key
            for key in ("smiles", "ccd", "sdf")
            if key in raw_block and str(raw_block.get(key) or "").strip()
        ]
        if len(ligand_keys) != 1:
            raise ValueError(
                f"Nesso ligand entry {sequence_index} needs exactly one non-empty smiles, ccd, or sdf value."
            )
        ligand_key = ligand_keys[0]
        if ligand_key == "sdf":
            raise ValueError(
                "Nesso SDF path inputs are not supported by the V-Bio /predict upload contract; "
                "use ligand SMILES or CCD."
            )

        ligand_value = str(raw_block[ligand_key]).strip()
        if ligand_key == "smiles":
            if len(ligand_value) > 4096:
                raise ValueError(f"Nesso ligand entry {sequence_index} SMILES is too long.")
            try:
                ligand_molecule = Chem.MolFromSmiles(ligand_value)
            except Exception as exc:
                raise ValueError(
                    f"Nesso ligand entry {sequence_index} has invalid SMILES: {ligand_value}"
                ) from exc
            if ligand_molecule is None:
                raise ValueError(
                    f"Nesso ligand entry {sequence_index} has invalid SMILES: {ligand_value}"
                )
        if ligand_key == "ccd":
            ligand_value = ligand_value.upper()
        ligand_block = {"id": id_value, ligand_key: ligand_value}
        normalized_sequences.append({"ligand": ligand_block})
        ligand_chain_ids.extend(chain_ids)
        for chain_id in chain_ids:
            ligand_sources[chain_id] = {"kind": ligand_key, "value": ligand_value}

    if not protein_chain_ids:
        raise ValueError("Nesso requires at least one protein sequence.")
    if not ligand_chain_ids:
        raise ValueError("Nesso requires at least one ligand.")

    requested_binder = ""
    requested_target = ""
    for entry in _property_entries(raw_data.get("properties")):
        affinity = entry.get("affinity")
        candidate = ""
        if isinstance(affinity, dict):
            candidate = str(affinity.get("binder") or affinity.get("ligand") or "").strip()
        elif affinity is True:
            candidate = str(entry.get("binder") or entry.get("ligand") or "").strip()
        if not candidate:
            candidate = str(entry.get("binder") or entry.get("ligand") or "").strip()
        if candidate and not requested_binder:
            requested_binder = candidate
        target_candidate = str(entry.get("target") or "").strip()
        if target_candidate and not requested_target:
            requested_target = target_candidate

    binder_chain_id = requested_binder or ligand_chain_ids[0]
    if binder_chain_id not in ligand_chain_ids:
        raise ValueError(
            f"Nesso affinity binder '{binder_chain_id}' must reference a ligand chain "
            f"(available ligand chains: {', '.join(ligand_chain_ids)})."
        )
    if requested_target and requested_target not in protein_chain_ids:
        raise ValueError(
            f"Nesso affinity target '{requested_target}' must reference a protein chain "
            f"(available protein chains: {', '.join(protein_chain_ids)})."
        )

    target_chain_ids = [requested_target] if requested_target else list(protein_chain_ids)
    normalized_data = {
        "version": 1,
        "sequences": normalized_sequences,
        "properties": [{"affinity": {"binder": binder_chain_id}}],
    }
    metadata = {
        "ligand_chain_id": binder_chain_id,
        "target_chain_ids": target_chain_ids,
        "protein_chain_ids": protein_chain_ids,
        "ligand_chain_ids": ligand_chain_ids,
        "ligand_source": ligand_sources[binder_chain_id],
    }
    return (
        yaml.safe_dump(normalized_data, sort_keys=False, allow_unicode=True),
        metadata,
    )


def _screening_record_token(value: Any, fallback: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    token = re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-_").lower()
    token = re.sub(r"-{2,}", "-", token)
    return (token or fallback)[:64].rstrip("-_") or fallback


def _unique_token(base: str, occupied: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in occupied:
        suffix_text = f"-{suffix}"
        candidate = f"{base[: max(1, 64 - len(suffix_text))].rstrip('-_')}{suffix_text}"
        suffix += 1
    occupied.add(candidate)
    return candidate


def _allocate_screening_binder_chain_id(occupied_chain_ids: set[str]) -> str:
    for candidate in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789":
        if candidate not in occupied_chain_ids:
            return candidate
    for index in range(1, 1000):
        candidate = f"V{index}"
        if candidate not in occupied_chain_ids:
            return candidate
    raise ValueError("Unable to allocate a chain id for the screening binder.")


def normalize_nesso_screening_input_yaml(yaml_content: str) -> dict[str, Any]:
    """Canonicalize one target complex plus a library of scored SMILES binders.

    Every generated Nesso record contains all declared protein components, all
    optional fixed/context ligands, and exactly one library compound.  The
    library compound is the affinity ``binder``; context ligands never become
    the scored binder implicitly.
    """
    try:
        raw_data = yaml.safe_load(yaml_content) or {}
    except Exception as exc:
        raise ValueError(f"Nesso virtual-screening input is not valid YAML: {exc}") from exc
    if not isinstance(raw_data, dict):
        raise ValueError("Nesso virtual-screening input must be a YAML mapping.")
    if raw_data.get("version", 1) != 1:
        raise ValueError("Nesso virtual screening supports YAML schema version 1 only.")
    if raw_data.get("constraints"):
        raise ValueError("Nesso virtual screening does not support prediction constraints.")
    if raw_data.get("templates"):
        raise ValueError("Nesso virtual screening does not support structure templates.")
    if raw_data.get("properties"):
        raise ValueError("Nesso virtual screening derives affinity properties automatically.")

    raw_sequences = raw_data.get("sequences")
    if not isinstance(raw_sequences, list) or not raw_sequences:
        raise ValueError("Nesso virtual screening requires at least one target protein entry.")

    occupied_chain_ids: set[str] = set()
    for sequence_index, raw_item in enumerate(raw_sequences, start=1):
        if not isinstance(raw_item, dict) or not raw_item:
            raise ValueError(f"Nesso sequence entry {sequence_index} must be a mapping.")
        supported_keys = [key for key in raw_item if str(key).lower() in _SUPPORTED_SEQUENCE_TYPES]
        if len(supported_keys) != 1:
            declared = ", ".join(str(key) for key in raw_item) or "<empty>"
            raise ValueError(
                "Nesso virtual screening accepts protein and ligand components only "
                f"(entry {sequence_index}: {declared})."
            )
        source_key = supported_keys[0]
        raw_block = raw_item.get(source_key)
        if not isinstance(raw_block, dict):
            raise ValueError(f"Nesso sequence entry {sequence_index} must contain a mapping.")
        chain_ids = _normalize_chain_ids(
            raw_block.get("id"),
            context=f"Nesso {str(source_key).lower()} entry {sequence_index}",
        )
        duplicates = [chain_id for chain_id in chain_ids if chain_id in occupied_chain_ids]
        if duplicates:
            raise ValueError(f"Nesso input reuses chain id(s): {', '.join(duplicates)}.")
        occupied_chain_ids.update(chain_ids)

    ligand_chain_id = _allocate_screening_binder_chain_id(occupied_chain_ids)
    # Reuse the single-complex canonicalizer for all entity, chain, sequence,
    # cyclic, modification, SMILES, and CCD validation.  The final synthetic
    # ligand is replaced with one library compound per generated record.
    probe = {
        "version": 1,
        "sequences": [
            *raw_sequences,
            {"ligand": {"id": ligand_chain_id, "smiles": "C"}},
        ],
        "properties": [{"affinity": {"binder": ligand_chain_id}}],
    }
    normalized_probe_yaml, probe_metadata = normalize_nesso_input_yaml(
        yaml.safe_dump(probe, sort_keys=False, allow_unicode=True)
    )
    normalized_probe = yaml.safe_load(normalized_probe_yaml)
    normalized_sequences = list(normalized_probe["sequences"])
    complex_entries = normalized_sequences[:-1]
    protein_entries = [
        entry for entry in complex_entries
        if isinstance(entry, dict) and "protein" in entry
    ]
    context_ligand_entries = [
        entry for entry in complex_entries
        if isinstance(entry, dict) and "ligand" in entry
    ]

    screening = raw_data.get("virtual_screening")
    if not isinstance(screening, dict):
        raise ValueError("Nesso input must contain a virtual_screening mapping.")
    raw_compounds = screening.get("compounds")
    if not isinstance(raw_compounds, list) or not raw_compounds:
        raise ValueError("virtual_screening.compounds must contain at least one SMILES compound.")
    if len(raw_compounds) > _MAX_SCREENING_COMPOUNDS:
        raise ValueError(
            f"Nesso virtual screening accepts at most {_MAX_SCREENING_COMPOUNDS} compounds per batch."
        )

    compounds: list[dict[str, Any]] = []
    occupied_record_ids: set[str] = set()
    canonical_smiles_seen: dict[str, str] = {}
    warnings: list[str] = []
    for index, raw_compound in enumerate(raw_compounds, start=1):
        if not isinstance(raw_compound, dict):
            raise ValueError(f"virtual_screening.compounds[{index}] must be a mapping.")
        smiles = str(raw_compound.get("smiles") or "").strip()
        if not smiles:
            raise ValueError(f"virtual_screening.compounds[{index}].smiles is required.")
        if len(smiles) > 4096:
            raise ValueError(f"virtual_screening.compounds[{index}].smiles is too long.")
        try:
            molecule = Chem.MolFromSmiles(smiles)
        except Exception as exc:
            raise ValueError(f"Compound {index} has invalid SMILES: {smiles}") from exc
        if molecule is None:
            raise ValueError(f"Compound {index} has invalid SMILES: {smiles}")
        canonical_smiles = Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)
        requested_name = str(raw_compound.get("name") or raw_compound.get("id") or "").strip()
        display_name = requested_name[:160] or f"Compound {index}"
        if canonical_smiles in canonical_smiles_seen:
            warnings.append(
                f"Skipped duplicate SMILES for {display_name}; same molecule as "
                f"{canonical_smiles_seen[canonical_smiles]}."
            )
            continue
        base_record_id = _screening_record_token(
            raw_compound.get("id") or requested_name,
            f"compound-{index:03d}",
        )
        record_id = _unique_token(base_record_id, occupied_record_ids)
        compound_id = record_id
        canonical_smiles_seen[canonical_smiles] = display_name
        compounds.append({
            "id": compound_id,
            "source_id": str(raw_compound.get("id") or "").strip()[:160] or None,
            "name": display_name,
            "smiles": smiles,
            "canonical_smiles": canonical_smiles,
            "record_id": record_id,
            "input_index": index,
        })

    if not compounds:
        raise ValueError("Nesso virtual screening has no unique valid compounds to run.")
    batch_name = str(screening.get("name") or raw_data.get("name") or "Virtual screening").strip()
    return {
        "schema_version": 1,
        "batch_name": batch_name[:160] or "Virtual screening",
        "complex_entries": complex_entries,
        "protein_entries": protein_entries,
        "context_ligand_entries": context_ligand_entries,
        "target_chain_ids": probe_metadata["protein_chain_ids"],
        "context_ligand_chain_ids": [
            chain_id
            for chain_id in probe_metadata["ligand_chain_ids"]
            if chain_id != ligand_chain_id
        ],
        "ligand_chain_id": ligand_chain_id,
        "compounds": compounds,
        "submitted_compound_count": len(raw_compounds),
        "warnings": warnings,
    }


def _sanitize_docker_extra_args(raw_args: list[str]) -> list[str]:
    sanitized: list[str] = []
    index = 0
    while index < len(raw_args):
        token = raw_args[index]
        if token in {"--env", "-e"}:
            if index + 1 >= len(raw_args) or "=" not in raw_args[index + 1]:
                index += 2
                continue
            sanitized.extend([token, raw_args[index + 1]])
            index += 2
            continue
        sanitized.append(token)
        index += 1
    return sanitized


def _redact_env_assignment(value: str) -> str:
    if "=" not in value:
        return value
    key, _raw_value = value.split("=", 1)
    normalized_key = key.strip().upper()
    if normalized_key in {"HTTP_PROXY", "HTTPS_PROXY"} or any(
        marker in normalized_key for marker in _SENSITIVE_ENV_KEY_MARKERS
    ):
        return f"{key}=***"
    return value


def _format_docker_command_for_log(command: list[str]) -> str:
    """Render a Docker command without exposing credentials in task logs."""
    rendered: list[str] = []
    redact_next_env_value = False
    for token in command:
        display_token = token
        if redact_next_env_value:
            redact_next_env_value = False
            display_token = _redact_env_assignment(token)
        elif token.startswith("--env="):
            display_token = "--env=" + _redact_env_assignment(token.removeprefix("--env="))
        elif token.startswith("-e="):
            display_token = "-e=" + _redact_env_assignment(token.removeprefix("-e="))
        if token in {"--env", "-e"}:
            redact_next_env_value = True
        rendered.append(shlex.quote(display_token))
    return " ".join(rendered)


def _docker_args_has_flag(args: list[str], flag: str) -> bool:
    return any(token == flag or token.startswith(f"{flag}=") for token in args)


def _docker_gpu_arg() -> str:
    raw = str(
        os.environ.get("BOLTZ_ASSIGNED_GPU_ID")
        or os.environ.get("CUDA_VISIBLE_DEVICES")
        or ""
    ).strip()
    if not raw:
        return "all"

    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens:
        raise RuntimeError("Nesso received an empty GPU assignment.")
    invalid = [token for token in tokens if not re.fullmatch(r"[A-Za-z0-9_.:-]+", token)]
    if invalid:
        raise RuntimeError(f"Nesso received invalid GPU identifiers: {', '.join(invalid)}.")
    return f"device={','.join(dict.fromkeys(tokens))}"


def _gpu_device_group_ids() -> list[int]:
    candidate_nodes = [
        Path("/dev/nvidiactl"),
        Path("/dev/nvidia-uvm"),
        Path("/dev/nvidia-uvm-tools"),
        *sorted(Path("/dev").glob("nvidia[0-9]*")),
    ]
    if Path("/dev/dri").exists():
        candidate_nodes.extend(sorted(Path("/dev/dri").glob("renderD*")))

    group_ids: list[int] = []
    for node in candidate_nodes:
        try:
            group_id = node.stat().st_gid
        except FileNotFoundError:
            continue
        if group_id not in group_ids:
            group_ids.append(group_id)
    return group_ids


def _task_container_name(task_id: Optional[str]) -> str:
    token = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(task_id or "prediction")).strip(".-_").lower()
    return f"vbio-nesso-{(token or 'prediction')[:48]}"


def _run_docker_command(command: list[str], log_path: Path) -> None:
    output_tail: list[str] = []
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if process.stdout:
            for line in process.stdout:
                log_file.write(line)
                log_file.flush()
                print(line, end="", file=sys.stderr)
                output_tail.append(line)
                if len(output_tail) > 200:
                    output_tail.pop(0)
        return_code = process.wait()

    if return_code != 0:
        raise RuntimeError(
            f"Nesso Docker run failed with exit code {return_code}. "
            f"Last output:\n{''.join(output_tail[-200:])}\nFull log: {log_path}"
        )


def _write_nesso_archive(
    output_archive_path: str,
    *,
    input_dir: Path,
    output_dir: Path,
    affinity_paths: list[Path],
    canonical_affinity_path: Path,
    screening_path: Path,
    manifest_path: Path,
    log_path: Path,
) -> None:
    with zipfile.ZipFile(output_archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for input_path in sorted(input_dir.glob("*.yaml")):
            archive.write(input_path, f"nesso/inputs/{input_path.name}")
        archive.write(manifest_path, "nesso/manifest.json")
        archive.write(screening_path, "nesso/screening.json")
        archive.write(canonical_affinity_path, "nesso/affinity.json")
        for affinity_path in affinity_paths:
            relative_affinity = affinity_path.relative_to(output_dir)
            archive.write(affinity_path, f"nesso/output/{relative_affinity.as_posix()}")
        if log_path.exists():
            archive.write(log_path, "nesso/nesso_docker.log")
        archive.writestr(
            "nesso/README.txt",
            "Nesso-1 virtual-screening result generated by V-Bio.\n"
            "This backend predicts affinity only and does not produce a CIF/PDB structure.\n"
            "screening.json contains the ranked batch; affinity.json is the best hit.\n"
            "affinity_pred_value is log10(IC50 / uM); lower values indicate stronger binding.\n",
        )


def _finite_number(value: Any, *, field: str, record_id: str) -> float:
    if isinstance(value, bool):
        raise RuntimeError(f"Nesso result '{record_id}' has invalid {field}.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Nesso result '{record_id}' is missing numeric {field}.") from exc
    if not math.isfinite(number):
        raise RuntimeError(f"Nesso result '{record_id}' has non-finite {field}.")
    return number


def _reject_non_finite_json_numbers(value: Any, *, field: str, record_id: str) -> None:
    """Reject NaN/Infinity anywhere in the model JSON before archiving it."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeError(f"Nesso result '{record_id}' has non-finite {field}.")
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            _reject_non_finite_json_numbers(nested, field=f"{field}.{key}", record_id=record_id)
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_non_finite_json_numbers(nested, field=f"{field}[{index}]", record_id=record_id)


def _build_screening_results(
    preparation: dict[str, Any],
    *,
    output_dir: Path,
    model_revision: str,
    seed: int,
) -> tuple[dict[str, Any], list[Path]]:
    expected_by_record = {
        str(compound["record_id"]): compound
        for compound in preparation["compounds"]
    }
    affinity_paths = sorted((output_dir / "predictions").glob("*/affinity.json"))
    actual_by_record = {path.parent.name: path for path in affinity_paths}
    missing = sorted(set(expected_by_record) - set(actual_by_record))
    unexpected = sorted(set(actual_by_record) - set(expected_by_record))
    if missing or unexpected or len(actual_by_record) != len(affinity_paths):
        details: list[str] = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        if len(actual_by_record) != len(affinity_paths):
            details.append("duplicate output record directories")
        raise RuntimeError(
            "Nesso batch output does not match submitted compounds"
            + (f" ({'; '.join(details)})" if details else "")
            + "."
        )

    ranked: list[dict[str, Any]] = []
    for record_id, compound in expected_by_record.items():
        affinity_path = actual_by_record[record_id]
        try:
            affinity = json.loads(affinity_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Nesso produced invalid affinity JSON for '{record_id}': {exc}") from exc
        if not isinstance(affinity, dict):
            raise RuntimeError(f"Nesso affinity result '{record_id}' must be a JSON object.")
        _reject_non_finite_json_numbers(affinity, field="affinity.json", record_id=record_id)
        normalized_affinity = dict(affinity)
        for field in _OPTIONAL_AFFINITY_NUMERIC_FIELDS:
            if affinity.get(field) is not None:
                normalized_affinity[field] = _finite_number(
                    affinity[field],
                    field=field,
                    record_id=record_id,
                )
        affinity_value = _finite_number(
            normalized_affinity.get("affinity_pred_value"),
            field="affinity_pred_value",
            record_id=record_id,
        )
        try:
            ic50_um = math.pow(10.0, affinity_value)
        except OverflowError as exc:
            raise RuntimeError(f"Nesso result '{record_id}' has out-of-range affinity_pred_value.") from exc
        if not math.isfinite(ic50_um):
            raise RuntimeError(f"Nesso result '{record_id}' has out-of-range affinity_pred_value.")
        member_one = normalized_affinity.get("affinity_pred_value1")
        member_two = normalized_affinity.get("affinity_pred_value2")
        ensemble_spread = None
        if member_one is not None and member_two is not None:
            ensemble_spread = abs(
                _finite_number(member_one, field="affinity_pred_value1", record_id=record_id)
                - _finite_number(member_two, field="affinity_pred_value2", record_id=record_id)
            )
        enriched = {
            **normalized_affinity,
            **compound,
            "backend": "nesso",
            "model": "Nesso-1",
            "affinity_scale": "log10(IC50 / uM)",
            "affinity_pred_value": affinity_value,
            "ic50_um": ic50_um,
            "pic50": 6.0 - affinity_value,
            "ensemble_spread": ensemble_spread,
            "target_chain_ids": preparation["target_chain_ids"],
            "binder_chain_id": preparation["ligand_chain_id"],
            "context_ligand_chain_ids": preparation["context_ligand_chain_ids"],
            "structure_available": False,
        }
        affinity_path.write_text(
            json.dumps(enriched, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        ranked.append(enriched)

    ranked.sort(key=lambda item: (float(item["affinity_pred_value"]), int(item["input_index"])))
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    affinity_values = [float(item["affinity_pred_value"]) for item in ranked]
    screening = {
        "schema_version": 1,
        "workflow": "virtual_screening",
        "backend": "nesso",
        "model": "Nesso-1",
        "model_revision": model_revision or None,
        "seed": seed,
        "batch_name": preparation["batch_name"],
        "submitted_compound_count": preparation["submitted_compound_count"],
        "compound_count": len(ranked),
        "duplicate_count": preparation["submitted_compound_count"] - len(ranked),
        "warnings": preparation["warnings"],
        "target_chain_ids": preparation["target_chain_ids"],
        "binder_chain_id": preparation["ligand_chain_id"],
        "context_ligand_chain_ids": preparation["context_ligand_chain_ids"],
        "structure_available": False,
        "summary": {
            "best_compound_id": ranked[0]["id"],
            "best_ic50_um": ranked[0]["ic50_um"],
            "median_ic50_um": math.pow(10.0, statistics.median(affinity_values)),
            "high_binding_probability_count": sum(
                1
                for item in ranked
                if isinstance(item.get("affinity_probability_binary"), (int, float))
                and not isinstance(item.get("affinity_probability_binary"), bool)
                and float(item["affinity_probability_binary"]) >= 0.5
            ),
        },
        "compounds": ranked,
    }
    return screening, [actual_by_record[str(item["record_id"])] for item in preparation["compounds"]]


def run_nesso_backend(
    temp_dir: str,
    yaml_content: str,
    output_archive_path: str,
    *,
    seed: Optional[int] = None,
    task_id: Optional[str] = None,
    low_vram: bool = False,
) -> None:
    """Run one V-Bio virtual-screening batch in the dedicated Nesso GPU image."""
    if low_vram:
        raise ValueError("Nesso does not expose a low-VRAM execution mode.")

    preparation = normalize_nesso_screening_input_yaml(yaml_content)
    runtime_root = Path(temp_dir) / "nesso_runtime"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    input_dir = runtime_root / "inputs"
    output_dir = runtime_root / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    for compound in preparation["compounds"]:
        nesso_input = {
            "version": 1,
            "sequences": [
                *preparation["complex_entries"],
                {
                    "ligand": {
                        "id": preparation["ligand_chain_id"],
                        "smiles": compound["canonical_smiles"],
                    }
                },
            ],
            "properties": [
                {"affinity": {"binder": preparation["ligand_chain_id"]}}
            ],
        }
        input_path = input_dir / f"{compound['record_id']}.yaml"
        input_path.write_text(
            yaml.safe_dump(nesso_input, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    log_path = runtime_root / "nesso_docker.log"

    image = str(NESSO_DOCKER_IMAGE or "").strip()
    if not image:
        raise RuntimeError("NESSO_DOCKER_IMAGE is not configured.")

    host_cache_raw = str(NESSO_HOST_CACHE_DIR or "").strip()
    if not host_cache_raw:
        raise RuntimeError("NESSO_HOST_CACHE_DIR is not configured.")
    host_cache_dir = Path(host_cache_raw).expanduser()
    if not host_cache_dir.is_absolute():
        raise ValueError("NESSO_HOST_CACHE_DIR must be an absolute host path.")
    host_cache_dir.mkdir(parents=True, exist_ok=True)
    container_cache_dir = str(NESSO_CONTAINER_CACHE_DIR or "/workspace/nesso-cache").strip()
    if not container_cache_dir.startswith("/"):
        raise ValueError("NESSO_CONTAINER_CACHE_DIR must be an absolute container path.")

    raw_extra_args = shlex.split(NESSO_DOCKER_EXTRA_ARGS) if NESSO_DOCKER_EXTRA_ARGS else []
    extra_args = _sanitize_docker_extra_args(raw_extra_args)
    runtime_task_id = str(task_id or os.environ.get("BOLTZ_TASK_ID") or "").strip()
    container_name = _task_container_name(runtime_task_id)

    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass

    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        f"boltz.task_id={runtime_task_id}",
        "--label",
        "boltz.runtime=nesso",
    ]
    if not _docker_args_has_flag(extra_args, "--runtime"):
        command.extend(["--runtime", "nvidia"])
    command.extend(
        [
            "--gpus",
            _docker_gpu_arg(),
            "--volume",
            f"{runtime_root}:/workspace/task",
            "--volume",
            f"{host_cache_dir}:{container_cache_dir}:ro",
            "--workdir",
            "/workspace/task",
            "--env",
            f"NESSO_CACHE={container_cache_dir}",
            "--env",
            f"HF_HOME={container_cache_dir}/huggingface",
            "--env",
            f"HF_HUB_CACHE={container_cache_dir}/huggingface",
            "--env",
            "HF_HUB_OFFLINE=1",
            "--env",
            "TRANSFORMERS_OFFLINE=1",
            "--env",
            "PYTHONUNBUFFERED=1",
        ]
    )
    for env_key in _PASSTHROUGH_ENV_KEYS:
        env_value = str(os.environ.get(env_key, "") or "").strip()
        if env_value:
            command.extend(["--env", f"{env_key}={env_value}"])

    command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    for group_id in _gpu_device_group_ids():
        command.extend(["--group-add", str(group_id)])
    command.extend(extra_args)

    effective_seed = max(0, int(seed)) if seed is not None else 42
    command.extend(
        [
            image,
            "nesso",
            "predict",
            "/workspace/task/inputs",
            "--out_dir",
            "/workspace/task/output",
            "--cache",
            container_cache_dir,
            "--accelerator",
            "gpu",
            "--devices",
            "1",
            "--num_workers",
            str(max(1, int(NESSO_NUM_WORKERS))),
            "--recycling_steps",
            str(max(0, int(NESSO_RECYCLING_STEPS))),
            "--precision",
            str(NESSO_PRECISION or "bf16-mixed"),
            "--require_affinity",
            "--override",
            "--seed",
            str(effective_seed),
        ]
    )
    model_revision = str(NESSO_MODEL_REVISION or "").strip()
    if model_revision:
        command.extend(["--model_revision", model_revision])
    if coerce_bool(NESSO_NO_KERNELS, True):
        command.append("--no_kernels")

    display_command = _format_docker_command_for_log(command)
    print(f"Running Nesso Docker: {display_command}", file=sys.stderr)
    _run_docker_command(command, log_path)

    screening, affinity_paths = _build_screening_results(
        preparation,
        output_dir=output_dir,
        model_revision=model_revision,
        seed=effective_seed,
    )
    screening_path = runtime_root / "screening.json"
    screening_path.write_text(
        json.dumps(screening, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    best_affinity = screening["compounds"][0]
    canonical_affinity_path = runtime_root / "affinity.json"
    canonical_affinity_path.write_text(
        json.dumps(best_affinity, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    best_record_id = str(best_affinity["record_id"])
    manifest = {
        "schema_version": 1,
        "workflow": "virtual_screening",
        "backend": "nesso",
        "model": "Nesso-1",
        "model_revision": model_revision or None,
        "seed": effective_seed,
        "structure_available": False,
        "compound_count": len(screening["compounds"]),
        "submitted_compound_count": preparation["submitted_compound_count"],
        "screening_file": "nesso/screening.json",
        "affinity_file": "nesso/affinity.json",
        "best_raw_affinity_file": f"nesso/output/predictions/{best_record_id}/affinity.json",
        "ligand_chain_id": preparation["ligand_chain_id"],
        "target_chain_ids": preparation["target_chain_ids"],
        "context_ligand_chain_ids": preparation["context_ligand_chain_ids"],
        "warnings": preparation["warnings"],
    }
    manifest_path = runtime_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    _write_nesso_archive(
        output_archive_path,
        input_dir=input_dir,
        output_dir=output_dir,
        affinity_paths=affinity_paths,
        canonical_affinity_path=canonical_affinity_path,
        screening_path=screening_path,
        manifest_path=manifest_path,
        log_path=log_path,
    )
    print(f"Nesso archive created: {output_archive_path}", file=sys.stderr)
