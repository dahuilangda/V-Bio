from __future__ import annotations

import copy
import glob
import hashlib
import io
import csv
import json
from collections import Counter
import logging
import math
import os
import shutil
import sys
import re
import threading
import time
import zipfile
from pathlib import Path
import tempfile
from typing import Any, Callable, Dict, Optional

import gemmi
from celery.result import AsyncResult
from werkzeug.utils import secure_filename
from backend.services.common_utils import coerce_bool, to_finite_float


_BOLTZ_WATER_RESNAMES = frozenset({'HOH', 'WAT', 'H2O', 'DOD'})
_BOLTZ_ION_RESNAMES = {
    'NA', 'CL', 'MG', 'CA', 'K', 'ZN', 'FE', 'MN', 'CU', 'CO', 'NI',
    'CD', 'HG', 'SR', 'BA', 'CS', 'LI', 'BR', 'I',
}
_PROTENIX_SUMMARY_SAMPLE_RE = re.compile(r'_summary_confidence_sample_(\d+)\.json$', re.IGNORECASE)
_PEPTIDE_DESIGN_RANK_RE = re.compile(r'(?:^|/)rank_(\d+)(?:_|\.|$)', re.IGNORECASE)


def _normalize_json_value_for_output(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _normalize_json_value_for_output(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_normalize_json_value_for_output(item) for item in value]
    return value


def _sanitize_json_bytes(payload: bytes) -> bytes:
    try:
        parsed = json.loads(payload)
    except Exception:
        return payload
    try:
        normalized = _normalize_json_value_for_output(parsed)
        return (json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except Exception:
        return payload


_SC_LIGAND_RADII = {
    'C': ('ALA', 'CB'),
    'N': ('ALA', 'N'),
    'O': ('ALA', 'O'),
    'S': ('MET', 'SD'),
    'BR': ('MET', 'SD'),
    'P': ('MET', 'SD'),
    'SE': ('MET', 'SE'),
    'CL': ('ALA', 'O'),
    'F': ('ALA', 'O'),
    'I': ('MET', 'SE'),
}


class ResultArchiveService:
    def __init__(self, *, app, celery_app, logger, get_redis_client_fn: Callable[[], Any]):
        self.app = app
        self.celery_app = celery_app
        self.logger = logger
        self.get_redis_client = get_redis_client_fn
        self._compact_metrics_lock = threading.Lock()
        self._compact_metrics_cache: Dict[str, Dict[str, Any]] = {}
        # task_id -> (archive_name|None, expires_at)；正/负结果统一短缓存
        self._archive_cache: Dict[str, tuple] = {}
        self.ARCHIVE_CACHE_TTL_SECONDS = 5.0

    def find_result_archive(self, task_id: str) -> Optional[str]:
        base_dir = self.app.config.get('UPLOAD_FOLDER')
        if not base_dir or not os.path.isdir(base_dir):
            return None

        # 状态轮询对同一 task 反复解析归档；正/负结果都做短 TTL 缓存，
        # 避免 glob 兜底在批次轮询下放大为全目录扫描。归档在任务完成时
        # 一次性出现，短 TTL 不影响可见性。
        now = time.monotonic()
        cached = self._archive_cache.get(task_id)
        if cached is not None and cached[1] > now:
            return cached[0]

        candidates = [
            f'{task_id}_results.zip',
            f'{task_id}_affinity_results.zip',
            f'{task_id}_lead_optimization_results.zip',
        ]
        resolved = None
        for name in candidates:
            candidate_path = os.path.join(base_dir, name)
            if os.path.exists(candidate_path):
                resolved = name
                break

        if resolved is None:
            try:
                matches = glob.glob(os.path.join(base_dir, glob.escape(task_id) + '_*.zip'))
                if matches:
                    resolved = os.path.basename(max(matches, key=os.path.getmtime))
            except Exception as exc:
                self.logger.warning('Failed to scan results directory for task %s: %s', task_id, exc)

        self._archive_cache[task_id] = (resolved, now + self.ARCHIVE_CACHE_TTL_SECONDS)
        if len(self._archive_cache) > 4096:  # 有界：批次轮询下的键空间可控
            for stale_key in list(self._archive_cache)[:1024]:
                self._archive_cache.pop(stale_key, None)
        return resolved

    def resolve_result_archive_path(self, task_id: str) -> tuple[str, str]:
        directory = self.app.config.get('UPLOAD_FOLDER')
        if not directory:
            raise FileNotFoundError('Result upload directory is not configured.')

        # Primary path: resolve by files on disk first, so result reads do not depend on Celery/Redis health.
        archive_name = self.find_result_archive(task_id)
        if archive_name:
            self.logger.debug('Resolved result archive for task %s directly from disk: %s', task_id, archive_name)
            filename = secure_filename(archive_name)
            filepath = os.path.join(directory, filename)

            abs_filepath = os.path.abspath(filepath)
            abs_upload_folder = os.path.abspath(directory)
            if not abs_filepath.startswith(abs_upload_folder):
                raise PermissionError(f'Invalid file path outside upload folder: {filepath}')
            if not os.path.exists(filepath):
                raise FileNotFoundError(f'Result file not found on disk: {filepath}')

            return filename, filepath

        # Fallback path: use Celery metadata when disk scan cannot locate the archive.
        try:
            task_result = AsyncResult(task_id, app=self.celery_app)
            task_ready = bool(task_result.ready())
            task_state = str(task_result.state)
            task_info = task_result.info if task_ready else {}
        except Exception as exc:
            raise FileNotFoundError(
                f'Result file not found on disk and task backend is unavailable: {exc}'
            ) from exc

        if not task_ready:
            raise FileNotFoundError(f'Task has not completed yet. State: {task_state}')

        result_info: Dict[str, Any]
        if isinstance(task_info, dict) and task_info.get('result_file'):
            result_info = task_info
        else:
            raise FileNotFoundError('Result file information not found in task metadata or on disk.')

        filename = secure_filename(str(result_info['result_file']))
        filepath = os.path.join(directory, filename)

        abs_filepath = os.path.abspath(filepath)
        abs_upload_folder = os.path.abspath(directory)
        if not abs_filepath.startswith(abs_upload_folder):
            raise PermissionError(f'Invalid file path outside upload folder: {filepath}')
        if not os.path.exists(filepath):
            raise FileNotFoundError(f'Result file not found on disk: {filepath}')

        return filename, filepath

    @staticmethod
    def _is_structure_file(name: str) -> bool:
        lower = name.lower()
        return lower.endswith('.cif') or lower.endswith('.mmcif') or lower.endswith('.pdb')

    @staticmethod
    def _normalize_structure_name_token(value: str) -> str:
        return str(value or '').strip().lstrip('/\\').lower()

    @classmethod
    def _find_structure_file_by_name(cls, names: list[str], preferred_structure_name: str | None) -> Optional[str]:
        token = cls._normalize_structure_name_token(preferred_structure_name or '')
        if not token:
            return None
        basename_token = cls._normalize_structure_name_token(os.path.basename(token))
        structure_names = [name for name in names if cls._is_structure_file(name)]
        for name in structure_names:
            if cls._normalize_structure_name_token(name) == token:
                return name
        for name in structure_names:
            if cls._normalize_structure_name_token(os.path.basename(name)) == basename_token:
                return name
        return None

    @staticmethod
    def _resolve_boltz_confidence_for_structure(names: list[str], structure_path: str | None) -> Optional[str]:
        if not structure_path:
            return None
        structure_base = os.path.basename(structure_path)
        structure_stem, _ = os.path.splitext(structure_base)
        if not structure_stem.strip():
            return None
        structure_dir = os.path.dirname(structure_path)
        candidates = [
            os.path.join(structure_dir, f'confidence_{structure_stem}.json') if structure_dir else f'confidence_{structure_stem}.json',
            f'confidence_{structure_stem}.json',
        ]
        return next((item for item in candidates if item in names), None)

    @staticmethod
    def _resolve_protenix_confidence_for_structure(names: list[str], structure_path: str | None) -> Optional[str]:
        if not structure_path:
            return None
        canonical_confidence = 'protenix/output/confidence_protenix_model_0.json'
        if canonical_confidence in names:
            return canonical_confidence
        structure_base = os.path.basename(structure_path)
        sample_match = re.search(r'_sample_(\d+)\.(?:cif|mmcif|pdb)$', structure_base, re.IGNORECASE)
        if not sample_match:
            return None
        structure_dir = os.path.dirname(structure_path)
        expected_suffix = f'_summary_confidence_sample_{sample_match.group(1)}.json'.lower()
        for name in names:
            if not name.lower().endswith(expected_suffix):
                continue
            if structure_dir and not name.startswith(f'{structure_dir}/'):
                continue
            return name
        return None

    @staticmethod
    def _collect_peptide_design_structure_files(names: list[str]) -> list[str]:
        scored: list[tuple[int, int, int, str]] = []
        for name in names:
            lower = name.lower()
            if not ResultArchiveService._is_structure_file(name):
                continue
            if 'af3/output/' in lower:
                continue
            if 'confidence_' in lower or 'summary_confidence' in lower:
                continue

            is_design_path = (
                lower.startswith('structures/')
                or '/structures/' in lower
                or '/designs/' in lower
                or '/results/' in lower
            )
            rank_match = _PEPTIDE_DESIGN_RANK_RE.search(lower)
            has_rank = rank_match is not None
            rank_value = int(rank_match.group(1)) if rank_match else 10**9
            if not is_design_path and not has_rank:
                continue
            scored.append((0 if has_rank else 1, rank_value, len(name), name))

        scored.sort()
        unique: list[str] = []
        seen: set[str] = set()
        for _, _, _, name in scored:
            if name in seen:
                continue
            seen.add(name)
            unique.append(name)
        return unique

    @staticmethod
    def _choose_peptide_design_results_file(names: list[str]) -> Optional[str]:
        candidates = [
            name
            for name in names
            if name.lower().endswith('.json')
            and (
                os.path.basename(name).lower() == 'design_results.json'
                or 'design_results' in os.path.basename(name).lower()
            )
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (0 if os.path.basename(item).lower() == 'design_results.json' else 1, len(item)))[0]

    @staticmethod
    def _choose_preferred_path(candidates: list[str]) -> Optional[str]:
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (1 if 'seed-' in item.lower() else 0, len(item)))[0]

    @staticmethod
    def _choose_best_boltz_structure_file(names: list[str]) -> Optional[str]:
        candidates: list[tuple[int, int, str]] = []
        for name in names:
            lower = name.lower()
            if not re.search(r'\.(cif|mmcif|pdb)$', lower):
                continue
            if 'af3/output/' in lower:
                continue
            score = 100
            if lower.endswith('.cif'):
                score -= 5
            if 'model_0' in lower or 'ranked_0' in lower:
                score -= 20
            elif 'model_' in lower or 'ranked_' in lower:
                score -= 5
            candidates.append((score, len(name), name))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

    @staticmethod
    def _to_finite_float(value: object) -> Optional[float]:
        return to_finite_float(value)

    @staticmethod
    def _boltz_confidence_heuristic_score(path: str) -> int:
        lower = path.lower()
        score = 100
        if 'confidence_' in lower:
            score -= 5
        if 'model_0' in lower or 'ranked_0' in lower:
            score -= 20
        elif 'model_' in lower or 'ranked_' in lower:
            score -= 5
        return score

    @staticmethod
    def _resolve_boltz_structure_for_confidence(names: list[str], confidence_path: str) -> Optional[str]:
        base = os.path.basename(confidence_path)
        lower = base.lower()
        if not lower.startswith('confidence_') or not lower.endswith('.json'):
            return None
        structure_stem = base[len('confidence_'):-len('.json')]
        if not structure_stem.strip():
            return None

        confidence_dir = os.path.dirname(confidence_path)

        def with_dir(file_name: str) -> str:
            return os.path.join(confidence_dir, file_name) if confidence_dir else file_name

        candidates = [
            with_dir(f'{structure_stem}.cif'),
            with_dir(f'{structure_stem}.mmcif'),
            with_dir(f'{structure_stem}.pdb'),
        ]
        return next((item for item in candidates if item in names), None)

    @staticmethod
    def _normalize_plddt_value(value: float) -> float:
        return value * 100.0 if value <= 1.0 else value

    def _estimate_ligand_mean_plddt_from_structure_bytes(self, structure_name: str, payload: bytes) -> Optional[float]:
        suffix = os.path.splitext(structure_name)[1] or '.cif'
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                handle.write(payload)
                temp_path = handle.name
            structure = gemmi.read_structure(temp_path)
            structure.setup_entities()
        except Exception as exc:
            self.logger.warning('Failed to read structure %s for ligand pLDDT estimate: %s', structure_name, exc)
            return None
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

        entity_types = {
            subchain: entity.entity_type.name
            for entity in structure.entities
            for subchain in entity.subchains
        }

        values: list[float] = []
        for model in structure:
            for chain in model:
                for residue in chain:
                    if entity_types.get(residue.subchain) not in {'NonPolymer', 'Branched'}:
                        continue
                    resname = residue.name.strip().upper()
                    if resname in _BOLTZ_WATER_RESNAMES or resname in _BOLTZ_ION_RESNAMES:
                        continue
                    for atom in residue:
                        b_iso = self._to_finite_float(atom.b_iso)
                        if b_iso is None:
                            continue
                        values.append(self._normalize_plddt_value(b_iso))

        if not values:
            return None
        return sum(values) / len(values)

    def _choose_best_boltz_files(self, src_zip: zipfile.ZipFile, names: list[str]) -> tuple[Optional[str], Optional[str]]:
        confidence_candidates = [
            name for name in names
            if name.lower().endswith('.json')
            and 'confidence' in name.lower()
            and 'af3/output/' not in name.lower()
        ]
        if not confidence_candidates:
            return self._choose_best_boltz_structure_file(names), None

        ligand_mean_cache: dict[str, Optional[float]] = {}
        structure_for_confidence: dict[str, Optional[str]] = {}
        scored: list[tuple[int, float, int, float, int, float, int, float, int, int, str]] = []

        for name in confidence_candidates:
            payload = None
            try:
                parsed = json.loads(src_zip.read(name))
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = None

            matched_structure = self._resolve_boltz_structure_for_confidence(names, name)
            structure_for_confidence[name] = matched_structure

            confidence_score = self._to_finite_float(payload.get('confidence_score')) if payload else None
            complex_plddt = self._to_finite_float(payload.get('complex_plddt')) if payload else None
            iptm = self._to_finite_float(payload.get('iptm')) if payload else None
            ligand_mean_plddt = self._to_finite_float(payload.get('ligand_mean_plddt')) if payload else None
            if ligand_mean_plddt is not None:
                ligand_mean_plddt = self._normalize_plddt_value(ligand_mean_plddt)
            if ligand_mean_plddt is None and matched_structure:
                if matched_structure not in ligand_mean_cache:
                    try:
                        structure_bytes = src_zip.read(matched_structure)
                    except Exception:
                        ligand_mean_cache[matched_structure] = None
                    else:
                        ligand_mean_cache[matched_structure] = self._estimate_ligand_mean_plddt_from_structure_bytes(
                            matched_structure,
                            structure_bytes,
                        )
                ligand_mean_plddt = ligand_mean_cache.get(matched_structure)

            heuristic = self._boltz_confidence_heuristic_score(name)
            scored.append(
                (
                    1 if ligand_mean_plddt is not None else 0,
                    ligand_mean_plddt if ligand_mean_plddt is not None else float('-inf'),
                    1 if confidence_score is not None else 0,
                    confidence_score if confidence_score is not None else float('-inf'),
                    1 if complex_plddt is not None else 0,
                    complex_plddt if complex_plddt is not None else float('-inf'),
                    1 if iptm is not None else 0,
                    iptm if iptm is not None else float('-inf'),
                    -heuristic,
                    -len(name),
                    name,
                )
            )

        scored.sort(reverse=True)
        selected_confidence = scored[0][10] if scored else None
        matched_structure = structure_for_confidence.get(selected_confidence) if selected_confidence else None
        selected_structure = matched_structure or self._choose_best_boltz_structure_file(names)
        return selected_structure, selected_confidence

    @staticmethod
    def _choose_best_af3_structure_file(names: list[str]) -> Optional[str]:
        candidates: list[tuple[int, int, str]] = []
        for name in names:
            lower = name.lower()
            if not re.search(r'\.(cif|mmcif|pdb)$', lower):
                continue
            if 'af3/output/' not in lower:
                continue
            score = 100
            if lower.endswith('.cif'):
                score -= 5
            if os.path.basename(lower) == 'boltz_af3_model.cif':
                score -= 30
            if '/model.cif' in lower or lower.endswith('model.cif'):
                score -= 8
            if 'seed-' in lower:
                score += 8
            else:
                score -= 6
            if 'predicted' in lower:
                score -= 1
            if 'model' in lower:
                score -= 1
            candidates.append((score, len(name), name))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

    @staticmethod
    def _choose_best_protenix_files(src_zip: zipfile.ZipFile, names: list[str]) -> tuple[Optional[str], Optional[str]]:
        canonical_structures = [
            'protenix/output/protenix_model_0.cif',
            'protenix/output/protenix_model_0.mmcif',
            'protenix/output/protenix_model_0.pdb',
            'mol1/output/mol1_model_0.cif',
        ]
        canonical_confidence = 'protenix/output/confidence_protenix_model_0.json'
        structure = next((item for item in canonical_structures if item in names), None)
        if structure and canonical_confidence in names:
            return structure, canonical_confidence

        summary_candidates = [
            item
            for item in names
            if (
                item.lower().startswith('protenix/output/')
                or item.lower().startswith('mol1/output/')
            )
            and item.lower().endswith('.json')
            and _PROTENIX_SUMMARY_SAMPLE_RE.search(os.path.basename(item))
        ]
        if not summary_candidates:
            return None, None

        scored: list[tuple[float, int, str]] = []
        for item in summary_candidates:
            try:
                payload = json.loads(src_zip.read(item))
                score = float(payload.get('ranking_score'))
            except Exception:
                continue
            if not math.isfinite(score):
                continue
            scored.append((score, -len(item), item))
        if not scored:
            raise RuntimeError('Protenix summary confidence files are present but ranking_score is invalid.')

        scored.sort(reverse=True)
        selected_summary = scored[0][2]
        sample_match = _PROTENIX_SUMMARY_SAMPLE_RE.search(os.path.basename(selected_summary))
        if not sample_match:
            raise RuntimeError(f'Unable to parse Protenix summary sample index from: {selected_summary}')

        sample_index = sample_match.group(1)
        summary_dir = os.path.dirname(selected_summary)
        summary_base = os.path.basename(selected_summary)
        structure_base = re.sub(
            r'_summary_confidence_sample_\d+\.json$',
            f'_sample_{sample_index}',
            summary_base,
            flags=re.IGNORECASE,
        )
        if structure_base == summary_base:
            raise RuntimeError(f'Unable to derive Protenix sample structure name from: {selected_summary}')

        structure_candidates = [
            os.path.join(summary_dir, f'{structure_base}.cif'),
            os.path.join(summary_dir, f'{structure_base}.mmcif'),
            os.path.join(summary_dir, f'{structure_base}.pdb'),
        ]
        structure = next((item for item in structure_candidates if item in names), None)
        if not structure:
            raise RuntimeError(
                f"Unable to locate Protenix structure for summary '{selected_summary}' "
                f'(expected sample index {sample_index}).'
            )
        return structure, selected_summary

    def _build_view_archive_bytes(self, source_zip_path: str, preferred_structure_name: str | None = None) -> bytes:
        with zipfile.ZipFile(source_zip_path, 'r') as src_zip:
            names = [name for name in src_zip.namelist() if not name.endswith('/')]
            lower_names = [name.lower() for name in names]
            is_af3 = any('af3/output/' in name for name in lower_names)
            is_protenix = any(
                'protenix/output/' in name or 'mol1/output/' in name
                for name in lower_names
            )
            is_nesso = 'nesso/manifest.json' in lower_names
            preferred_structure_token = self._normalize_structure_name_token(preferred_structure_name or '')
            preferred_structure = self._find_structure_file_by_name(names, preferred_structure_name)
            if preferred_structure_token and not preferred_structure:
                raise RuntimeError(f"Requested structure file was not found in result archive: {preferred_structure_name}")

            include: list[str] = []
            if is_nesso:
                manifest = next(
                    (name for name in names if name.lower() == 'nesso/manifest.json'),
                    None,
                )
                if manifest:
                    include.append(manifest)
                screening = next(
                    (name for name in names if name.lower() == 'nesso/screening.json'),
                    None,
                )
                if screening:
                    include.append(screening)
                canonical_affinity = next(
                    (name for name in names if name.lower() == 'nesso/affinity.json'),
                    None,
                )
                if canonical_affinity:
                    include.append(canonical_affinity)
                else:
                    affinity_candidates = [
                        name for name in names
                        if name.lower().endswith('/affinity.json')
                    ]
                    if affinity_candidates:
                        include.append(sorted(affinity_candidates, key=lambda item: (len(item), item))[0])
            elif is_af3:
                # the viewer must show the sample the metrics were computed
                # from — derive the trio from the extractor's best sample,
                # with the legacy name heuristics as fallback.
                af3_samples = self._prediction_samples(src_zip, names)
                af3_best = self._best_sample(
                    [c for c in af3_samples if c.get('cif')]) if af3_samples else None
                structure = preferred_structure if preferred_structure_token else (
                    (af3_best or {}).get('cif')
                    or self._choose_best_af3_structure_file(names))
                if structure:
                    include.append(structure)
                summary = (af3_best or {}).get('summary_name') or self._choose_preferred_path([
                    name for name in names
                    if name.lower().endswith('.json')
                    and 'af3/output/' in name.lower()
                    and 'summary_confidences' in name.lower()
                ])
                confidences = (af3_best or {}).get('full_name') or self._choose_preferred_path([
                    name for name in names
                    if name.lower().endswith('.json')
                    and 'af3/output/' in name.lower()
                    and os.path.basename(name).lower() == 'confidences.json'
                ])
                if summary:
                    include.append(summary)
                if confidences:
                    include.append(confidences)
            elif any(name.lower().endswith('halo_results.json') for name in names):
                # HALO lead-optimization run artifacts: the structured
                # halo_results.json is the whole deliverable (structureless).
                halo_file = next(
                    (name for name in names if name.lower().endswith('halo_results.json')),
                    None,
                )
                if halo_file:
                    include.append(halo_file)
                for candidate_artifact in (
                    'candidates.csv',
                    'oracle_scores.csv',
                    'rounds.jsonl',
                    'config.json',
                ):
                    matches = [
                        name for name in names
                        if name.lower().endswith(candidate_artifact)
                    ]
                    if matches:
                        include.append(sorted(matches, key=len)[0])

            elif is_protenix:
                if preferred_structure:
                    structure = preferred_structure
                    confidence = self._resolve_protenix_confidence_for_structure(names, preferred_structure)
                else:
                    structure, confidence = self._choose_best_protenix_files(src_zip, names)
                if structure:
                    include.append(structure)
                if confidence:
                    include.append(confidence)
                affinity_candidates = [
                    name
                    for name in names
                    if name.lower().endswith('.json') and os.path.basename(name).lower() == 'affinity_data.json'
                ]
                protenix_affinity = self._choose_preferred_path(affinity_candidates)
                if protenix_affinity:
                    include.append(protenix_affinity)
            else:
                if preferred_structure:
                    structure = preferred_structure
                    confidence = self._resolve_boltz_confidence_for_structure(names, preferred_structure)
                else:
                    structure, confidence = self._choose_best_boltz_files(src_zip, names)
                if structure:
                    include.append(structure)
                if confidence:
                    include.append(confidence)
                peptide_summary_candidates = [
                    name
                    for name in names
                    if name.lower().endswith('.json')
                    and os.path.basename(name).lower() == 'results_summary.json'
                ]
                peptide_summary = self._choose_preferred_path(peptide_summary_candidates)
                if peptide_summary:
                    include.append(peptide_summary)
                    design_results = self._choose_peptide_design_results_file(names)
                    if design_results:
                        include.append(design_results)
                    if preferred_structure:
                        include.append(preferred_structure)
                    else:
                        include.extend(self._collect_peptide_design_structure_files(names))
                affinity_candidates = [name for name in names if name.lower().endswith('.json') and 'affinity' in name.lower()]
                if affinity_candidates:
                    include.append(sorted(affinity_candidates, key=lambda item: len(item))[0])

            ipsae_candidates = [
                name
                for name in names
                if name.lower().endswith('.json') and os.path.basename(name).lower() == 'best_ipsae.json'
            ]
            ipsae = self._choose_preferred_path(ipsae_candidates)
            if ipsae:
                include.append(ipsae)

            if not include:
                # Surface the engine's own error log (e.g. protenix2dock writes
                # ERR/*.txt when the runtime crashed internally) so the caller
                # sees the real failure instead of a generic 500.
                err_detail = ''
                for err_name in sorted(name for name in names if '/err/' in f"/{name}".lower()):
                    try:
                        err_detail += f"\n--- {err_name} ---\n{src_zip.read(err_name).decode('utf-8', 'replace')[-2000:]}"
                    except (KeyError, OSError):
                        continue
                raise RuntimeError(
                    'Unable to build view archive: no renderable files found.' + err_detail
                )

            include_unique: list[str] = []
            seen = set()
            for item in include:
                if item in seen:
                    continue
                seen.add(item)
                include_unique.append(item)

            out_buffer = io.BytesIO()
            with zipfile.ZipFile(out_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as out_zip:
                for member_name in include_unique:
                    try:
                        payload = src_zip.read(member_name)
                    except KeyError:
                        continue
                    if member_name.lower().endswith('.json'):
                        payload = _sanitize_json_bytes(payload)
                    out_zip.writestr(member_name, payload)
                # Result-view metrics (interface Sc, MSA depth, downsampled
                # PAE) the SPA cannot derive from the slim archive itself.
                try:
                    compact = self._extract_compact_prediction_metrics_from_zip(source_zip_path)
                except Exception:  # noqa: BLE001 — view archive must build even without metrics
                    compact = None
                if isinstance(compact, dict) and compact:
                    out_zip.writestr('compact_metrics.json',
                                     json.dumps(compact, ensure_ascii=False))
            out_buffer.seek(0)
            return out_buffer.getvalue()

    def build_or_get_view_archive(self, source_zip_path: str, preferred_structure_name: str | None = None) -> str:
        src_stat = os.stat(source_zip_path)
        cache_schema_version = 'view-v17-extractor-best'
        preferred_structure_token = self._normalize_structure_name_token(preferred_structure_name or '')
        cache_seed = f'{cache_schema_version}|{source_zip_path}|{int(src_stat.st_mtime_ns)}|{src_stat.st_size}|{preferred_structure_token}'
        cache_key = hashlib.sha256(cache_seed.encode('utf-8')).hexdigest()[:24]
        cache_dir = Path('/tmp/boltz_result_view_cache')
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f'{cache_key}.zip'
        if cache_path.exists():
            return str(cache_path)

        data = self._build_view_archive_bytes(source_zip_path, preferred_structure_name=preferred_structure_name)
        temp_path = cache_dir / f'{cache_key}.tmp'
        temp_path.write_bytes(data)
        os.replace(str(temp_path), str(cache_path))
        return str(cache_path)

    def get_tracker_status(self, task_id: str) -> tuple[Optional[Dict], Optional[str]]:
        try:
            redis_client = self.get_redis_client()
            status_raw = redis_client.get(f'task_status:{task_id}')
            heartbeat = redis_client.get(f'task_heartbeat:{task_id}')
            status_data = json.loads(status_raw) if status_raw else None
            return status_data, heartbeat
        except Exception as exc:
            self.logger.warning('Failed to fetch tracker status for task %s: %s', task_id, exc)
            return None, None

    # 历史别名：与 _normalize_plddt_value 同实现，保留调用点不动
    _normalize_01_or_100 = _normalize_plddt_value

    def _extract_off_diagonal_extreme(self, value: Any, *, pick: str) -> Optional[float]:
        best: Optional[float] = None

        def update(candidate: Optional[float]) -> None:
            nonlocal best
            if candidate is None:
                return
            if best is None:
                best = candidate
                return
            if pick == 'min':
                if candidate < best:
                    best = candidate
            elif candidate > best:
                best = candidate

        if isinstance(value, dict):
            for left_key, nested in value.items():
                left_token = str(left_key)
                if isinstance(nested, dict):
                    for right_key, raw in nested.items():
                        if left_token == str(right_key):
                            continue
                        update(self._to_finite_float(raw))
                    continue
                if isinstance(nested, list):
                    for right_idx, raw in enumerate(nested):
                        if left_token == str(right_idx):
                            continue
                        update(self._to_finite_float(raw))
                    continue
                update(self._to_finite_float(nested))
            return best

        if isinstance(value, list):
            contains_rows = any(isinstance(item, list) for item in value)
            if contains_rows:
                for row_idx, row in enumerate(value):
                    if not isinstance(row, list):
                        continue
                    for col_idx, raw in enumerate(row):
                        if row_idx == col_idx:
                            continue
                        update(self._to_finite_float(raw))
                return best
            for raw in value:
                update(self._to_finite_float(raw))
            return best

        return self._to_finite_float(value)

    def _collect_finite_floats(self, value: Any, *, limit: int = 20000) -> list[float]:
        if limit <= 0:
            return []
        out: list[float] = []
        stack = [value]
        while stack and len(out) < limit:
            current = stack.pop()
            if isinstance(current, dict):
                stack.extend(current.values())
                continue
            if isinstance(current, list):
                stack.extend(current)
                continue
            parsed = self._to_finite_float(current)
            if parsed is None:
                continue
            out.append(parsed)
        return out

    def _extract_ligand_atom_plddts(self, payload: Dict[str, Any]) -> list[float]:
        max_atoms = 512

        def normalize_values(raw_values: Any) -> list[float]:
            values = [self._normalize_01_or_100(item) for item in self._collect_finite_floats(raw_values, limit=max_atoms)]
            return values[:max_atoms]

        # Preferred explicit ligand-level arrays.
        for key in ('ligand_atom_plddts', 'ligand_atom_plddt'):
            values = normalize_values(payload.get(key))
            if values:
                return values

        # AF-style confidences usually expose global atom arrays + chain ids.
        atom_values_raw = payload.get('atom_plddts') or payload.get('atom_plddt')
        chain_values_raw = payload.get('atom_chain_ids')
        if not isinstance(atom_values_raw, list) or not isinstance(chain_values_raw, list):
            return []
        if len(atom_values_raw) == 0 or len(atom_values_raw) != len(chain_values_raw):
            return []

        per_chain: Dict[str, list[float]] = {}
        for chain_id, atom_plddt in zip(chain_values_raw, atom_values_raw):
            chain_token = str(chain_id or '').strip()
            if not chain_token:
                continue
            parsed = self._to_finite_float(atom_plddt)
            if parsed is None:
                continue
            per_chain.setdefault(chain_token, []).append(self._normalize_01_or_100(parsed))

        if len(per_chain) < 2:
            return []
        ranked = sorted(per_chain.items(), key=lambda item: (len(item[1]), item[0]))
        ligand_chain, ligand_values = ranked[0]
        total_atoms = sum(len(values) for values in per_chain.values())
        if not ligand_values or total_atoms <= 0:
            return []
        # Guard against selecting a full protein chain when chain typing is ambiguous.
        if len(ligand_values) > max_atoms and (len(ligand_values) / float(total_atoms)) > 0.45:
            self.logger.debug(
                'Skip ligand atom pLDDT extraction due to ambiguous chain split: %s (%d/%d)',
                ligand_chain,
                len(ligand_values),
                total_atoms,
            )
            return []
        return ligand_values[:max_atoms]

    @staticmethod
    def _extract_first_non_empty_string(payload: Dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str):
                trimmed = value.strip()
                if trimmed:
                    return trimmed
        return ""

    def _extract_ligand_display_smiles(self, payload: Dict[str, Any]) -> str:
        return self._extract_first_non_empty_string(
            payload,
            'ligand_display_smiles',
            'ligandDisplaySmiles',
        )

    def _extract_ligand_smiles(self, payload: Dict[str, Any]) -> str:
        return self._extract_first_non_empty_string(
            payload,
            'ligand_smiles',
            'ligandSmiles',
        )

    def _extract_ligand_display_atom_plddts(self, payload: Dict[str, Any]) -> list[float]:
        max_atoms = 512

        def normalize_values(raw_values: Any) -> list[float]:
            values = [self._normalize_01_or_100(item) for item in self._collect_finite_floats(raw_values, limit=max_atoms)]
            return values[:max_atoms]

        for key in ('ligand_display_atom_plddts', 'ligandDisplayAtomPlddts'):
            values = normalize_values(payload.get(key))
            if values:
                return values

        by_chain_raw = payload.get('ligand_display_atom_plddts_by_chain')
        if isinstance(by_chain_raw, dict):
            entries = [normalize_values(value) for value in by_chain_raw.values()]
            entries = [entry for entry in entries if entry]
            if entries:
                entries.sort(key=lambda item: len(item), reverse=True)
                return entries[0]
        return []

    def _extract_pair_iptm(self, payload: Dict[str, Any]) -> Optional[float]:
        direct = self._to_finite_float(
            payload.get('pair_iptm_target_binder')
            or payload.get('pair_iptm')
            or payload.get('iptm')
            or payload.get('confidence_score')
        )
        if direct is not None:
            return direct
        for key in ('pair_chains_iptm', 'chain_pair_iptm', 'pair_iptm_matrix'):
            parsed = self._extract_off_diagonal_extreme(payload.get(key), pick='max')
            if parsed is not None:
                return parsed
        return None

    def _extract_pair_pae(self, payload: Dict[str, Any]) -> Optional[float]:
        direct = self._to_finite_float(
            payload.get('complex_pde')
            or payload.get('complex_pae')
            or payload.get('pair_pae')
            or payload.get('pair_pae_min')
            or payload.get('pair_pde')
            or payload.get('pair_gpde')
            or payload.get('gpde')
            or payload.get('pae')
        )
        if direct is not None:
            return direct
        for key in (
            'pair_chains_pae',
            'pair_chains_pae_min',
            'pair_chains_pde',
            'pair_chains_gpde',
            'chain_pair_pae',
            'chain_pair_pae_min',
            'chain_pair_pae_minimum',
            'chain_pair_pde',
            'chain_pair_gpde',
        ):
            parsed = self._extract_off_diagonal_extreme(payload.get(key), pick='min')
            if parsed is not None:
                return parsed
        pae_matrix = payload.get('pae')
        if not isinstance(pae_matrix, list):
            return None
        total = 0.0
        count = 0
        for row in pae_matrix:
            if not isinstance(row, list):
                continue
            for value in row:
                parsed = self._to_finite_float(value)
                if parsed is None:
                    continue
                total += parsed
                count += 1
                if count >= 20000:
                    break
            if count >= 20000:
                break
        if count == 0:
            return None
        return total / float(count)

    def _extract_ligand_plddt(self, payload: Dict[str, Any], ligand_atom_plddts: Optional[list[float]] = None) -> Optional[float]:
        direct = self._to_finite_float(payload.get('ligand_mean_plddt') or payload.get('ligand_plddt'))
        if direct is not None:
            return self._normalize_01_or_100(direct)
        atom_values = ligand_atom_plddts if isinstance(ligand_atom_plddts, list) else self._extract_ligand_atom_plddts(payload)
        if atom_values:
            return sum(atom_values) / float(len(atom_values))
        fallback = self._to_finite_float(payload.get('complex_plddt') or payload.get('mean_plddt') or payload.get('plddt'))
        if fallback is not None:
            return self._normalize_01_or_100(fallback)
        return None

    @staticmethod
    def _chain_letter(index: int) -> str:
        """Spreadsheet-style chain label: A..Z, AA, AB, ..."""
        label = ""
        n = index
        while True:
            label = chr(65 + n % 26) + label
            n = n // 26 - 1
            if n < 0:
                return label

    @staticmethod
    def _prediction_samples(src_zip, names: list) -> list:
        """Backend-aware discovery of per-sample artifacts in a result archive.

        Every structure-prediction backend ships, per diffusion sample: a
        model cif, a PAE carrier (token-level full_data json, AF3 token-level
        confidences json, or a Boltz token-level pae npz), and a scalar
        confidence summary. Layouts differ; this normalizes the discovery into
        one shape. Summaries are parsed eagerly (small); PAE/full_data
        payloads are NOT — consumers fetch them for the chosen sample, so a
        multi-sample archive never pays for more than one PAE parse.
        """
        samples: list = []
        used = set()

        def find_cif(prefix: str) -> Optional[str]:
            for ext in ('.cif', '.mmcif'):
                for n in names:
                    if n.startswith(prefix) and n.endswith(ext) and n not in used:
                        return n
            return None

        # 1) mol1/esmfold2/protenix: <prefix>summary_confidence_sample_<i>.json
        for name in names:
            base = os.path.basename(name)
            if 'summary_confidence_sample_' not in base or not base.endswith('.json'):
                continue
            if 'full_data' in base:
                continue
            sample = base.split('summary_confidence_sample_')[1].split('.')[0]
            prefix = name.split('summary_confidence_sample_')[0]
            full = f"{prefix}full_data_sample_{sample}.json"
            cif = find_cif(prefix + f"sample_{sample}.")
            if full not in names or not cif or name in used:
                continue
            try:
                summary = json.loads(src_zip.read(name))
            except Exception:  # noqa: BLE001
                continue
            used.update({name, cif})
            samples.append({
                'sample': sample,
                'cif': cif, 'summary_name': name, 'summary': summary,
                'full_name': full, 'pae_name': full, 'pae_kind': 'json',
            })

        # 2) alphafold3: <dir>/summary_confidences.json + confidences.json + model.cif
        for name in names:
            base = os.path.basename(name)
            if base != 'summary_confidences.json':
                continue
            parent = name[:name.rfind('/') + 1]
            cif = parent + 'model.cif'
            conf = parent + 'confidences.json'
            if cif not in names or conf not in names or name in used:
                continue
            try:
                summary = json.loads(src_zip.read(name))
            except Exception:  # noqa: BLE001
                continue
            used.update({name, cif, conf})
            samples.append({
                'sample': os.path.basename(parent.rstrip('/')),
                'cif': cif, 'summary_name': name, 'summary': summary,
                'full_name': conf, 'pae_name': conf, 'pae_kind': 'json',
            })

        # 3) boltz: confidence_data_model_<i>.json + pae_data_model_<i>.npz
        #    + data_model_<i>.cif, paired within the conf's own directory.
        confs = [n for n in names
                 if os.path.basename(n).startswith('confidence_data_model_')
                 and n.endswith('.json') and n not in used]
        for conf in confs:
            parent = conf[:conf.rfind('/') + 1]
            model = os.path.basename(conf)[len('confidence_data_model_'):-len('.json')]
            pae = parent + f'pae_data_model_{model}.npz'
            cif = parent + f'data_model_{model}.cif'
            if pae not in names or cif not in names or conf in used:
                continue
            try:
                summary = json.loads(src_zip.read(conf))
            except Exception:  # noqa: BLE001
                continue
            used.update({conf, pae, cif})
            samples.append({
                'sample': model,
                'cif': cif, 'summary_name': conf, 'summary': summary,
                'pae_name': pae, 'pae_kind': 'npz',
            })
        ResultArchiveService._apply_csv_ranks(samples, src_zip, names)
        return samples

    @staticmethod
    def _sample_rank(sample: Dict[str, Any]) -> float:
        csv_rank = sample.get('csv_rank')
        if isinstance(csv_rank, (int, float)) and math.isfinite(csv_rank):
            return float(csv_rank)
        payload = sample.get('summary') or {}
        for key in ('ranking_score', 'confidence_score'):
            v = payload.get(key)
            if isinstance(v, (int, float)) and math.isfinite(v):
                return float(v)
            try:
                return float(str(v))
            except (TypeError, ValueError):
                continue
        return 0.0

    @staticmethod
    def _zip_ranking_scores(src_zip, names: list) -> Dict[str, float]:
        """Full-precision per-sample ranks from ranking_scores.csv — af3
        rounds the summary ranking_score to 2 decimals, which ties distinct
        samples and can pick a different one than the displayed structure."""
        csv_name = next((n for n in names
                         if os.path.basename(n).lower() == 'ranking_scores.csv'), None)
        if not csv_name:
            return {}
        try:
            # utf-8-sig strips a leading BOM that survives .strip() and
            # would otherwise poison the first header cell.
            text = src_zip.read(csv_name).decode('utf-8-sig', errors='replace')
            rows = list(csv.DictReader(io.StringIO(text)))
        except Exception:  # noqa: BLE001
            return {}
        if not rows:
            return {}
        columns = {str(k).strip().lstrip('\ufeff').strip().lower(): k
                   for k in rows[0] if k}
        score_key = columns.get('ranking_score')
        if score_key is None:
            return {}
        seed_key = columns.get('seed')
        sample_key = columns.get('sample')
        if sample_key is None:
            return {}
        has_seed = seed_key is not None
        ranks: Dict[str, float] = {}
        for row in rows:
            try:
                value = float(str(row.get(score_key) or '').strip())
            except (TypeError, ValueError):
                continue
            seed = str(row.get(seed_key) or '').strip() if has_seed else ''
            if seed.lower().startswith('seed-'):
                seed = seed[len('seed-'):]
            sample = str(row.get(sample_key) or '').strip()
            if sample:
                # Seedless CSVs key on the sample number alone; label
                # lookup falls back to that shape.
                ranks[f"{sample}" if not has_seed else f"{seed}-{sample}"] = value
        return ranks

    @staticmethod
    def _csv_rank_for_label(label: str, csv_ranks: Dict[str, float]) -> Optional[float]:
        if not csv_ranks or not label:
            return None
        if label in csv_ranks:
            return csv_ranks[label]
        parts = re.split(r'_sample-', label, maxsplit=1)
        if len(parts) == 2:
            seed = parts[0]
            if seed.startswith('seed-'):
                seed = seed[len('seed-'):]
            value = csv_ranks.get(f"{seed}-{parts[1]}")
            if value is None and not any('-' in key for key in csv_ranks):
                value = csv_ranks.get(parts[1])
            return value
        return None

    @classmethod
    def _apply_csv_ranks(cls, samples: list, src_zip, names: list) -> None:
        ranks = cls._zip_ranking_scores(src_zip, names)
        if not ranks:
            return
        for sample in samples:
            value = cls._csv_rank_for_label(str(sample.get('sample') or ''), ranks)
            if value is not None:
                sample['csv_rank'] = value

    @classmethod
    def _best_sample(cls, samples: list, key=None):
        """Deterministic best sample: highest rank, lowest index on ties —
        zip member order is arbitrary and must not pick the sample."""
        rank = key or cls._sample_rank
        return samples[min(range(len(samples)),
                           key=lambda i: (-rank(samples[i]), i))]

    @staticmethod
    def _cif_asym_tokens(structure_text: str) -> Optional[list]:
        """Per-token chain labels in cif file order (polymer residue = 1
        token, het residue = 1 token per atom), mirroring backend PAE
        tokenization; used when a backend ships no token asym payload."""
        try:
            import gemmi
            structure = gemmi.make_structure_from_block(
                gemmi.cif.read_string(structure_text).sole_block())
            structure.setup_entities()
        except Exception:  # noqa: BLE001
            return None
        if not len(structure):
            return None
        labels: list = []
        for ch in structure[0]:
            for res in ch:
                if res.name.strip() in _BOLTZ_WATER_RESNAMES:
                    continue  # PAE carriers carry no water tokens
                info = gemmi.find_tabulated_residue(res.name)
                polymer = (info is not None
                           and (info.is_amino_acid() or info.is_nucleic_acid())
                           and res.het_flag != 'H')
                if polymer:
                    labels.append(ch.name)
                else:
                    labels.extend([ch.name] * len(res))
        return labels or None

    @staticmethod
    def _sample_confidence(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalized scalar summary from any backend confidence payload."""
        def pick(*keys):
            for k in keys:
                v = payload.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
            return None
        out = {}
        iptm = pick('iptm', 'complex_iptm')
        ptm = pick('ptm', 'complex_ptm')
        ranking = pick('ranking_score', 'confidence_score')
        plddt = pick('complex_plddt', 'plddt')
        if iptm is not None:
            out['iptm'] = iptm
        if ptm is not None:
            out['ptm'] = ptm
        if ranking is not None:
            out['ranking_score'] = ranking
        if plddt is not None:
            # boltz stores 0-1; mol1 stores 0-100. Keep native scale; the
            # frontend normalizes.
            out['plddt'] = plddt
        for k in ('has_clash', 'fraction_disordered'):
            v = payload.get(k)
            if isinstance(v, (int, float)):
                out[k] = v
        pair = payload.get('chain_pair_iptm')
        if isinstance(pair, list) and pair:
            out['chain_pair_iptm'] = pair
        ligand_iptm = pick('ligand_iptm')
        if ligand_iptm is not None:
            out['ligand_iptm'] = ligand_iptm
        protein_iptm = pick('protein_iptm')
        if protein_iptm is not None:
            out['protein_iptm'] = protein_iptm
        return out

    @staticmethod
    def _extract_compact_pae_from_zip(src_zip) -> Optional[Dict[str, Any]]:
        """Compact PAE matrix (≤134²) + chain bounds for ANY backend, via the
        backend-aware samples abstraction. Downsamples in numpy before
        materializing Python floats."""
        import numpy as _np
        names = [n for n in src_zip.namelist() if not n.endswith('/')]
        samples = ResultArchiveService._prediction_samples(src_zip, names)
        candidates = [s for s in samples if s.get('pae_name')]
        if not candidates:
            return None
        best = ResultArchiveService._best_sample(candidates)
        try:
            payload = None
            arr = None
            asym = None
            if best['pae_kind'] == 'npz':
                with _np.load(io.BytesIO(src_zip.read(best['pae_name']))) as loaded:
                    key = 'pae' if 'pae' in loaded.files else next(iter(loaded.files))
                    arr = _np.asarray(loaded[key], dtype=float)
                if best.get('cif'):
                    asym = ResultArchiveService._cif_asym_tokens(
                        src_zip.read(best['cif']).decode('utf-8', errors='replace'))
            else:
                payload = json.loads(src_zip.read(best['pae_name']))
                pae = payload.get('token_pair_pae') or payload.get('pae')
                arr = _np.asarray(pae, dtype=float)
                asym = payload.get('token_asym_id')
                if asym is None:
                    raw = payload.get('token_chain_ids')
                    if isinstance(raw, list) and raw and len(raw) == len(pae):
                        uniq = []
                        for value in raw:
                            sval = str(value)
                            if sval not in uniq:
                                uniq.append(sval)
                        asym = [uniq.index(str(v)) for v in raw]
        except Exception:  # noqa: BLE001
            return None
        if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[0] != arr.shape[1]:
            return None
        if asym is not None and len(asym) != arr.shape[0]:
            asym = None
        n0 = arr.shape[0]
        down = max(1, -(-n0 // 134))
        # Numpy downsample before materializing Python floats.
        matrix = arr[::down, ::down].round(2).tolist()
        # bounds are in full-token space; the frontend rescales to the
        # downsampled matrix — do NOT pre-shrink here.
        bounds = []
        if asym:
            # Merge consecutive tokens of one chain on the raw label; the
            # stored chain value is the numeric id when possible, else the
            # ordinal chain index (string labels).
            prev = object()
            for idx, chain in enumerate(asym):
                if chain == prev and bounds:
                    bounds[-1]['end'] = idx
                else:
                    try:
                        numeric = int(chain)
                    except (TypeError, ValueError):
                        numeric = len(bounds)
                    bounds.append({'chain': numeric, 'start': idx, 'end': idx})
                prev = chain
        return {'pae_matrix': matrix, 'pae_chain_bounds': bounds}

    @staticmethod
    def _extract_interface_sc(src_zip) -> Optional[Dict[str, Any]]:
        """Lawrence-Colman shape complementarity of the best-ranked sample.

        Sides follow the classical definition: target is the largest amino-
        acid chain, the binder surface is every other chain's heavy atoms
        (binder polymers, covalently bound linkers, ligands and cofactors;
        waters excluded). Covers protein-protein, protein-peptide and
        protein-small-molecule interfaces.
        """
        import gemmi
        import numpy as np
        from shape_complementarity import compute_sc

        names = [n for n in src_zip.namelist() if not n.endswith('/')]
        samples = ResultArchiveService._prediction_samples(src_zip, names)
        candidates = [s for s in samples if s.get('cif')]
        if not candidates:
            return None
        best = ResultArchiveService._best_sample(candidates)
        cif_name = best['cif']
        try:
            structure_text = src_zip.read(cif_name).decode('utf-8', errors='replace')
            structure = gemmi.make_structure_from_block(
                gemmi.cif.read_string(structure_text).sole_block())
            structure.setup_entities()
        except Exception:  # noqa: BLE001 — malformed sample structure: no Sc
            return None
        model = structure[0] if len(structure) else None
        if model is None:
            return None

        water = _BOLTZ_WATER_RESNAMES

        def amino_count(chain):
            return sum(1 for res in chain
                       if res.het_flag != 'H'
                       and gemmi.find_tabulated_residue(res.name) is not None
                       and gemmi.find_tabulated_residue(res.name).is_amino_acid())

        def side(chains):
            coords, atoms, residues = [], [], []
            for chain in chains:
                for res in chain:
                    if res.name.strip() in water:
                        continue
                    info = gemmi.find_tabulated_residue(res.name)
                    is_amino = info is not None and info.is_amino_acid()
                    for atom in res:
                        if atom.element == 'H':
                            continue
                        coords.append((atom.pos.x, atom.pos.y, atom.pos.z))
                        if is_amino:
                            # Polymer atoms key directly into the sc-rs radius
                            # table.
                            atoms.append(atom.name)
                            residues.append(res.name.strip())
                        else:
                            # Ligand/cofactor atoms: translate to a standard
                            # residue key with the same vdW radius (Br/P ->
                            # MET SD 1.85, SE -> MET SE 2.0, C -> ALA CB 1.88,
                            # N -> ALA N 1.65, O/CL/F -> ALA O 1.46).
                            element = atom.element.name.upper()
                            res_key, atom_key = _SC_LIGAND_RADII.get(
                                element, ('ALA', 'CB'))
                            atoms.append(atom_key)
                            residues.append(res_key)
            return coords, atoms, residues

        polymer_chains = [ch for ch in model if amino_count(ch) > 0]
        if not polymer_chains:
            return None
        target_chain = max(polymer_chains, key=amino_count)
        binder_chains = [ch for ch in model if ch is not target_chain]
        if not binder_chains:
            return None
        target_side, binder_side = side([target_chain]), side(binder_chains)
        if not target_side[0] or not binder_side[0]:
            return None
        target_coords = np.asarray(target_side[0])
        binder_coords = np.asarray(binder_side[0])
        contacts = (np.linalg.norm(
            target_coords[:, None, :] - binder_coords[None, :, :], axis=-1) <= 8.0).any()
        if not contacts:
            return None
        result = compute_sc(target_side[0], target_side[1], target_side[2],
                            binder_side[0], binder_side[1], binder_side[2])
        sc_value = float(result.sc)
        if not math.isfinite(sc_value):
            return None
        return {
            'interface_sc': sc_value,
            'interface_sc_median': float(result.median_distance),
        }

    @staticmethod
    def _extract_pair_ipsae(src_zip) -> Optional[Dict[str, Any]]:
        """Directional per-chain-pair ipSAE via the official IPSAE2 CLI.

        The best-ranked sample (cif + PAE carrier) is staged and handed to
        `python -m ipsae`; the official reader natively understands every
        backend's carrier (mol1/esmfold2 and protenix token-level json,
        AF3 confidences json, Boltz pae npz). All scoring math lives in the
        official package, so values match the reference by construction.
        """
        import subprocess

        names = [n for n in src_zip.namelist() if not n.endswith('/')]
        samples = ResultArchiveService._prediction_samples(src_zip, names)
        candidates = [s for s in samples if s.get('cif') and s.get('pae_name')]
        if not candidates:
            return None

        best = ResultArchiveService._best_sample(candidates)
        workdir = Path(tempfile.mkdtemp(prefix='vbio-ipsae-'))
        try:
            cif_path = workdir / 'sample.cif'
            pae_path = workdir / f'pae.{best["pae_kind"]}'
            cif_path.write_bytes(src_zip.read(best['cif']))
            pae_path.write_bytes(src_zip.read(best['pae_name']))

            # Binder group = every chain except the largest (target); the
            # official --group scores the union straight from the full PAE
            # matrix (IPSAE2 virtual-molecule mode).
            import gemmi

            cif_residues: Counter = Counter()
            cif_atoms: Counter = Counter()
            try:
                structure_text = src_zip.read(best['cif']).decode('utf-8', errors='replace')
                structure = gemmi.make_structure_from_block(
                    gemmi.cif.read_string(structure_text).sole_block())
                structure.setup_entities()
            except Exception:  # noqa: BLE001 — malformed cif: no group, no letters
                structure = None
            if structure is not None:
                for ch in structure[0]:
                    for res in ch:
                        if res.name.strip() in _BOLTZ_WATER_RESNAMES:
                            continue
                        info = gemmi.find_tabulated_residue(res.name)
                        polymer = (info is not None
                                   and (info.is_amino_acid() or info.is_nucleic_acid())
                                   and res.het_flag != 'H')
                        if polymer:
                            cif_residues[ch.name] += 1  # PAE: 1 token per polymer residue
                        else:
                            cif_atoms[ch.name] += len(res)  # atom-token per het residue
            if not cif_residues and not cif_atoms:
                return None

            target_chain = max(cif_residues, key=cif_residues.get) if cif_residues else None
            binder_chains = [c for c in cif_residues if c != target_chain] + list(cif_atoms)

            # cif id -> token asym letter up front, so the --group string
            # speaks ipsae2's chain language (raw cif ids may not exist there).
            full = best.get('full_data')
            if full is None and best.get('full_name'):
                try:
                    full = json.loads(src_zip.read(best['full_name']))
                except Exception:  # noqa: BLE001
                    full = {}
            asym_counts = Counter(str(a) for a in ((full or {}).get('token_asym_id') or []))
            id_to_letter: dict = {}
            used: set = set()
            cif_chain_names = set(cif_residues) | set(cif_atoms)
            if cif_chain_names and cif_chain_names <= set(asym_counts):
                # cif chain ids already match token labels — map 1:1, no
                # unit-count guessing.
                id_to_letter = {name: name for name in cif_chain_names}
            else:
                for chain, count in cif_residues.items():
                    for asym, acount in asym_counts.items():
                        if acount == count and asym not in used:
                            id_to_letter[chain] = ResultArchiveService._chain_letter(
                                int(asym)) if asym.isdigit() else asym
                            used.add(asym)
                            break
                for chain, count in cif_atoms.items():
                    for asym, acount in asym_counts.items():
                        if acount == count and asym not in used:
                            id_to_letter[chain] = ResultArchiveService._chain_letter(
                                int(asym)) if asym.isdigit() else asym
                            used.add(asym)
                            break

            out_path = workdir / 'ipsae.tsv'
            command = [sys.executable, '-m', 'ipsae', str(cif_path), str(pae_path),
                       '--pae-cutoff', '10', '--output-file', str(out_path)]
            # IPSAE2 rejects a single-chain group (a group needs 2+ chains);
            # for one binder chain the pair rows already carry both directions.
            # Chains whose token mapping failed would only feed ipsae2 an
            # unknown id — drop them instead of failing the whole run.
            # Layout 3 (boltz npz) carries no token labels: ipsae2 reads the
            # boltz cif natively, so raw cif ids are its chain language —
            # keep them instead of silently dropping the group aggregate.
            binder_letters = sorted({id_to_letter.get(c, c) for c in binder_chains
                                     if c in id_to_letter or not asym_counts})
            if len(binder_letters) >= 2:
                command += ['--group', '+'.join(binder_letters)]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=300)
            if result.returncode != 0 or not out_path.exists():
                logging.getLogger(__name__).warning(
                    'ipsae2 failed on %s: %s', best['cif'],
                    (result.stderr or '')[-200:])
                return None

            def to_letter(chain_name: str) -> str:
                # group rows join chains with '+' ('B+L'); map each part.
                return '+'.join(id_to_letter.get(part, part)
                                for part in chain_name.split('+'))

            with open(out_path, newline='') as handle:
                rows_out = list(csv.DictReader(handle, delimiter='\t'))

            pair_rows = []
            group_values: dict = {}
            for row in rows_out:
                chn1, chn2 = row.get('chn1', ''), row.get('chn2', '')
                try:
                    ipsae12 = float(row['ipsae12'])
                    ipsae21 = float(row['ipsae21'])
                except (KeyError, ValueError):
                    continue
                if '+' in chn1 or '+' in chn2:
                    group_values[(chn1, chn2)] = (ipsae12, ipsae21)
                    continue
                pair_rows.append({
                    'a': to_letter(chn1), 'b': to_letter(chn2),
                    'a_to_b': ipsae12, 'b_to_a': ipsae21,
                })
            if not pair_rows:
                return None

            out: Dict[str, Any] = {'pair_ipsae': pair_rows}
            # Binder aggregate: prefer the official group row; for a single
            # binder chain (no group emitted) derive it from the target pair.
            target_letter = id_to_letter.get(target_chain)
            binder_group = next(((c1, c2) for (c1, c2) in group_values
                                 if target_chain and (target_chain in c2.split('+'))), None)
            if binder_group:
                i12, i21 = group_values[binder_group]
                # ipsae12: aligned on the binder group, scoring the target.
                out['ipsae_protein_to_ligand'] = i21
                out['ipsae_ligand_to_protein'] = i12
                out['ipsae_official_max'] = max(i21, i12)
            elif target_chain:
                target_letter = id_to_letter.get(target_chain, target_chain)
                p2l = [r['a_to_b'] for r in pair_rows
                       if r['a'] == target_letter and r['b'] != target_letter]
                l2p = [r['b_to_a'] for r in pair_rows
                       if r['a'] == target_letter and r['b'] != target_letter]
                if p2l:
                    out['ipsae_protein_to_ligand'] = max(p2l)
                if l2p:
                    out['ipsae_ligand_to_protein'] = max(l2p)
                if out.get('ipsae_protein_to_ligand') is not None or out.get('ipsae_ligand_to_protein') is not None:
                    out['ipsae_official_max'] = max(
                        out.get('ipsae_protein_to_ligand', 0.0),
                        out.get('ipsae_ligand_to_protein', 0.0))
            return out
        except Exception:  # noqa: BLE001 — malformed inputs: no pair ipsae
            logging.getLogger(__name__).exception('pair ipsae extraction failed')
            return None
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _extract_msa_stats_from_zip(src_zip) -> Optional[Dict[str, Any]]:
        """MSA stats from a3m files inside the archive. Per-chain files
        (protenix msa/<chain>.a3m, af3 <chain>_msa.a3m) each describe one
        chain's alignment: the deepest row count is the total depth and
        every chain carries depth + per-position coverage, matching the
        mol1 summary block so every backend fills the coverage chart.

        a3m lowercase characters are insertions against the query columns;
        they are stripped before column mapping, and rows that do not match
        the query width afterwards are skipped. Gap counts accumulate one
        row at a time, so memory scales with alignment width only."""
        import numpy as _np
        per_chain: list = []
        for name in src_zip.namelist():
            if not name.lower().endswith('.a3m'):
                continue
            try:
                seqs = [line for line in src_zip.read(name).decode('utf-8', errors='replace').splitlines()
                        if line and not line.startswith('>')]
            except Exception:  # noqa: BLE001
                continue
            if not seqs:
                continue
            stem = os.path.basename(name)[:-len('.a3m')]
            if stem.lower().endswith('_msa'):
                stem = stem[:-len('_msa')]
            chain = stem.strip() or 'unnamed'
            stripped = [''.join(ch for ch in seq if not ch.islower())
                        for seq in seqs]
            cols = len(stripped[0])
            if cols <= 0:
                continue
            gap_counts = _np.zeros(cols, dtype=_np.int64)
            kept = 0
            query_gaps = 0
            for seq in stripped:
                b = _np.frombuffer(seq.encode('ascii', errors='ignore'),
                                   dtype=_np.uint8)
                if len(b) != cols:
                    continue  # non-conforming row (non-ascii or ragged)
                kept += 1
                gaps = b == ord('-')
                gap_counts += gaps
                if kept == 1:
                    query_gaps = int(gaps.sum())
            if kept <= 0:
                continue
            coverage = (100 - gap_counts * 100 // kept).tolist()
            per_chain.append({
                'chain': chain, 'length': cols - query_gaps,
                'depth': kept, 'coverage': coverage,
            })
        if not per_chain:
            return None
        deepest = {}
        for entry in per_chain:
            prev = deepest.get(entry['chain'])
            if prev is None or entry['depth'] > prev['depth']:
                deepest[entry['chain']] = entry
        per_chain = sorted(deepest.values(), key=lambda entry: entry['chain'])
        return {'depth': max(entry['depth'] for entry in per_chain),
                'chains': per_chain}

    def _extract_compact_prediction_metrics_from_zip(self, source_zip_path: str) -> Optional[Dict[str, Any]]:
        try:
            with zipfile.ZipFile(source_zip_path, 'r') as src_zip:
                names = [name for name in src_zip.namelist() if not name.endswith('/')]
                json_candidates = [
                    name for name in names
                    if name.lower().endswith('.json')
                    and (
                        'confidence' in name.lower()
                        or os.path.basename(name).lower() == 'best_ipsae.json'
                        or os.path.basename(name).lower() in {'confidences.json', 'affinity_data.json'}
                    )
                ]
                # Fall back to peptide design results_summary.json when no confidence files exist.
                if not json_candidates:
                    summary_name = next(
                        (n for n in names if os.path.basename(n).lower() == 'results_summary.json'),
                        None
                    )
                    if summary_name:
                        json_candidates = [summary_name]
                # af3 archives duplicate the best sample at the top level;
                # per-seed sample dirs are authoritative — drop the copies so
                # this block and the samples-based supplement read one sample.
                if any('/seed-' in n for n in json_candidates):
                    json_candidates = [n for n in json_candidates if '/seed-' in n]
                csv_ranks = self._zip_ranking_scores(src_zip, names)
                parsed_entries: list[tuple[tuple, Dict[str, Any]]] = []
                best: Optional[Dict[str, Any]] = None
                best_score: tuple = ()
                for name in json_candidates:
                    try:
                        payload = json.loads(src_zip.read(name))
                    except Exception:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    # Handle peptide design results_summary.json: extract from best candidate row.
                    if os.path.basename(name).lower() == 'results_summary.json':
                        top_results = payload.get('top_results') or payload.get('best_sequences')
                        if isinstance(top_results, list) and top_results:
                            best_row: Optional[Dict[str, Any]] = None
                            for candidate in top_results:
                                if not isinstance(candidate, dict):
                                    continue
                                cs = self._to_finite_float(candidate.get('composite_score'))
                                if best_row is None or (cs is not None and (self._to_finite_float(best_row.get('composite_score')) is None or cs > self._to_finite_float(best_row.get('composite_score')))):
                                    best_row = candidate
                            if best_row:
                                payload = best_row
                        else:
                            continue
                    pair_iptm = self._extract_pair_iptm(payload)
                    pair_pae = self._extract_pair_pae(payload)
                    ligand_atom_plddts = self._extract_ligand_atom_plddts(payload)
                    ligand_smiles = self._extract_ligand_smiles(payload)
                    ligand_display_smiles = self._extract_ligand_display_smiles(payload)
                    ligand_display_atom_plddts = self._extract_ligand_display_atom_plddts(payload)
                    ligand_plddt = self._extract_ligand_plddt(payload, ligand_atom_plddts=ligand_atom_plddts)
                    ipsae_dom = self._to_finite_float(payload.get('ipsae_dom'))
                    ligand_ipsae_max = self._to_finite_float(payload.get('ligand_ipsae_max'))
                    protein_to_ligand_ipsae = self._to_finite_float(payload.get('protein_to_ligand_ipsae'))
                    ligand_to_protein_ipsae = self._to_finite_float(payload.get('ligand_to_protein_ipsae'))
                    chain_pair_iptm_global = self._to_finite_float(payload.get('chain_pair_iptm_global'))
                    has_clash = payload.get('has_clash')
                    coverage = (
                        int(pair_iptm is not None)
                        + int(pair_pae is not None)
                        + int(ligand_plddt is not None)
                        + int(ipsae_dom is not None)
                        + int(ligand_ipsae_max is not None)
                        + int(protein_to_ligand_ipsae is not None)
                        + int(ligand_to_protein_ipsae is not None)
                        + int(chain_pair_iptm_global is not None)
                    )
                    if coverage <= 0:
                        continue
                    parent = name[:name.rfind('/')] if '/' in name else ''
                    rank_value = self._csv_rank_for_label(
                        os.path.basename(parent), csv_ranks)
                    if rank_value is None:
                        rank_value = self._to_finite_float(payload.get('ranking_score'))
                    if rank_value is None:
                        rank_value = self._to_finite_float(payload.get('confidence_score'))
                    # Equal (coverage, rank): the token-level carrier wins
                    # over the rounded per-sample summary — zip member order
                    # must not decide which pair_pae ships.
                    carrier = 0 if os.path.basename(name).lower().startswith('summary') else 1
                    score = (coverage, rank_value if rank_value is not None else 0.0, carrier)
                    metrics_block = {
                        'pair_iptm': pair_iptm,
                        'pair_pae': pair_pae,
                        'ligand_plddt': ligand_plddt,
                        'ipsae_dom': ipsae_dom,
                        'ligand_ipsae_max': ligand_ipsae_max,
                        'protein_to_ligand_ipsae': protein_to_ligand_ipsae,
                        'ligand_to_protein_ipsae': ligand_to_protein_ipsae,
                        'chain_pair_iptm_global': chain_pair_iptm_global,
                        'ligand_atom_plddts': ligand_atom_plddts,
                        'ligand_smiles': ligand_smiles,
                        'ligand_display_smiles': ligand_display_smiles,
                        'ligand_display_atom_plddts': ligand_display_atom_plddts,
                    }
                    if has_clash is not None:
                        metrics_block['has_clash'] = coerce_bool(has_clash)
                    if score <= best_score:
                        parsed_entries.append((score, metrics_block))
                        continue
                    best_score = score
                    best = metrics_block
                    parsed_entries.append((score, dict(best)))
                if not best:
                    return None
                merged: Dict[str, Any] = {k: v for k, v in best.items() if v is not None}
                for metric_key in (
                    'pair_iptm',
                    'pair_pae',
                    'ligand_plddt',
                    'ipsae_dom',
                    'ligand_ipsae_max',
                    'protein_to_ligand_ipsae',
                    'ligand_to_protein_ipsae',
                    'chain_pair_iptm_global',
                    'ligand_atom_plddts',
                    'ligand_smiles',
                    'ligand_display_smiles',
                    'ligand_display_atom_plddts',
                ):
                    if metric_key in merged:
                        continue
                    best_metric_score: tuple = ()
                    best_metric_value: Optional[Any] = None
                    for metric_score, metrics in parsed_entries:
                        if metric_key in {'ligand_atom_plddts', 'ligand_display_atom_plddts'}:
                            values = metrics.get(metric_key)
                            if not isinstance(values, list) or len(values) == 0:
                                continue
                            value = values
                        elif metric_key == 'ligand_smiles':
                            value = str(metrics.get(metric_key) or '').strip()
                            if not value:
                                continue
                        elif metric_key == 'ligand_display_smiles':
                            value = str(metrics.get(metric_key) or '').strip()
                            if not value:
                                continue
                        else:
                            value = self._to_finite_float(metrics.get(metric_key))
                            if value is None:
                                continue
                        if metric_score <= best_metric_score:
                            continue
                        best_metric_score = metric_score
                        best_metric_value = value
                    if best_metric_value is not None:
                        merged[metric_key] = best_metric_value
                pair_pae = self._to_finite_float(merged.get('pair_pae'))
                if pair_pae is not None:
                    for alias in ('complex_pde', 'complex_pae', 'pae'):
                        if self._to_finite_float(merged.get(alias)) is None:
                            merged[alias] = pair_pae
                # Downsampled PAE matrix + MSA depth + interface Sc for the
                # results view; each is additive and must not sink the merge.
                try:
                    pae_block = self._extract_compact_pae_from_zip(src_zip)
                    if pae_block:
                        merged.setdefault('pae_matrix', pae_block['pae_matrix'])
                        merged.setdefault('pae_chain_bounds', pae_block['pae_chain_bounds'])
                except Exception as exc:  # noqa: BLE001 — pae is additive
                    self.logger.warning('pae extraction skipped: %s', exc)
                try:
                    sc_block = self._extract_interface_sc(src_zip)
                    if sc_block:
                        merged.update(sc_block)
                except Exception as exc:  # noqa: BLE001 — sc is additive
                    self.logger.warning('interface sc extraction skipped: %s', exc)
                try:
                    pair_block = self._extract_pair_ipsae(src_zip)
                    if pair_block:
                        merged.update(pair_block)
                        # The legacy ligand-ipsae keys were distance-filtered
                        # with the linker pooled into the protein side; the
                        # official pure-PAE values above supersede them.
                        merged['protein_to_ligand_ipsae'] = pair_block['ipsae_protein_to_ligand']
                        merged['ligand_to_protein_ipsae'] = pair_block['ipsae_ligand_to_protein']
                        merged['ligand_ipsae_max'] = pair_block['ipsae_official_max']
                except Exception:  # noqa: BLE001 — pair ipsae is additive
                    pass
                # Backend-uniform scalars: boltz/af3/protenix confidence
                # payloads use different scalar spellings; normalize from the
                # best sample so every backend fills the same metric cards.
                try:
                    names = [n for n in src_zip.namelist() if not n.endswith('/')]
                    samples = self._prediction_samples(src_zip, names)
                    if samples:
                        best = self._best_sample(samples)
                        normalized = self._sample_confidence(best['summary'])
                        # af3 rounds the summary ranking_score to 2 decimals;
                        # the csv row carries the full precision.
                        csv_rank = best.get('csv_rank')
                        if csv_rank is not None:
                            normalized['ranking_score'] = float(csv_rank)
                        for key, value in normalized.items():
                            merged.setdefault(key, value)
                except Exception:  # noqa: BLE001 — scalar supplement is additive
                    pass
                # MSA depth fallback: a3m rows in the archive when the backend
                # has no summary-level msa block.
                if 'msa' not in merged:
                    try:
                        msa_block = self._extract_msa_stats_from_zip(src_zip)
                        if msa_block:
                            merged['msa'] = msa_block
                    except Exception:  # noqa: BLE001 — msa is additive
                        pass
                try:
                    summary_name = next(n for n in src_zip.namelist()
                                        if n.lower().endswith('output/summary.json'))
                    root_summary = json.loads(src_zip.read(summary_name))
                    msa_stats = root_summary.get('msa')
                    if isinstance(msa_stats, dict) and msa_stats.get('depth'):
                        # a depth-only summary must not discard the richer
                        # per-chain block derived from a3m files
                        existing = merged.get('msa')
                        if (not msa_stats.get('chains')
                                and isinstance(existing, dict)
                                and existing.get('chains')):
                            merged['msa'] = {**existing, 'depth': msa_stats['depth']}
                        else:
                            merged['msa'] = msa_stats
                except Exception:  # noqa: BLE001 — zip 无 summary.json 时跳过
                    pass
                # The zipped summary predates per-chain MSA stats on older
                # tasks; the on-disk summary is refreshed by backfill/mainte-
                # nance and wins when it is the richer one.
                archive_stem = Path(source_zip_path).stem.removesuffix('_results')
                msa_path = Path(source_zip_path).parent / 'mol1' / archive_stem / 'output' / 'summary.json'
                try:
                    disk_msa = json.loads(msa_path.read_text()).get('msa')
                    if isinstance(disk_msa, dict) and disk_msa.get('depth'):
                        merged['msa'] = disk_msa
                except Exception:  # noqa: BLE001 — no on-disk summary: keep zip value
                    pass
                return merged or None
        except Exception as exc:
            self.logger.debug('Failed to extract compact prediction metrics from %s: %s', source_zip_path, exc)
            return None

    def get_compact_prediction_metrics(
        self,
        task_id: str,
        source_zip_path: Optional[str] = None,
        include_pae_matrix: bool = False,
    ) -> Optional[Dict[str, Any]]:
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return None
        if source_zip_path is None:
            try:
                _, source_zip_path = self.resolve_result_archive_path(normalized_task_id)
            except Exception:
                return None
        if not source_zip_path:
            return None

        try:
            stat = os.stat(source_zip_path)
        except Exception:
            return None
        cache_key = hashlib.sha256(
            f"{source_zip_path}|{int(stat.st_mtime_ns)}|{stat.st_size}".encode('utf-8')
        ).hexdigest()[:24]

        with self._compact_metrics_lock:
            cached = self._compact_metrics_cache.get(cache_key)
            if cached is not None:
                result = copy.deepcopy(cached)
                if not include_pae_matrix:
                    result.pop('pae_matrix', None)
                return result

        parsed = self._extract_compact_prediction_metrics_from_zip(source_zip_path)
        if not parsed:
            return None
        with self._compact_metrics_lock:
            if len(self._compact_metrics_cache) > 512:
                self._compact_metrics_cache.clear()
            self._compact_metrics_cache[cache_key] = copy.deepcopy(parsed)
        result = copy.deepcopy(parsed)
        if not include_pae_matrix:
            # ~100 KB; only the results view consumes it, via the view archive.
            result.pop('pae_matrix', None)
        return result
