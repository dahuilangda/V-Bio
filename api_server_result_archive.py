from __future__ import annotations

import glob
import hashlib
import io
import json
import math
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import gemmi
from celery.result import AsyncResult
from werkzeug.utils import secure_filename


_BOLTZ_WATER_RESNAMES = {'HOH', 'WAT', 'H2O'}
_BOLTZ_ION_RESNAMES = {
    'NA', 'CL', 'MG', 'CA', 'K', 'ZN', 'FE', 'MN', 'CU', 'CO', 'NI',
    'CD', 'HG', 'SR', 'BA', 'CS', 'LI', 'BR', 'I',
}
_PROTENIX_SUMMARY_SAMPLE_RE = re.compile(r'_summary_confidence_sample_(\d+)\.json$', re.IGNORECASE)


class ResultArchiveService:
    def __init__(self, *, app, celery_app, logger, get_redis_client_fn: Callable[[], Any]):
        self.app = app
        self.celery_app = celery_app
        self.logger = logger
        self.get_redis_client = get_redis_client_fn

    def find_result_archive(self, task_id: str) -> Optional[str]:
        base_dir = self.app.config.get('UPLOAD_FOLDER')
        if not base_dir or not os.path.isdir(base_dir):
            return None

        candidates = [
            f'{task_id}_results.zip',
            f'{task_id}_affinity_results.zip',
            f'{task_id}_virtual_screening_results.zip',
            f'{task_id}_lead_optimization_results.zip',
        ]
        for name in candidates:
            candidate_path = os.path.join(base_dir, name)
            if os.path.exists(candidate_path):
                return name

        try:
            matches = glob.glob(os.path.join(base_dir, f'{task_id}_*.zip'))
            if matches:
                newest = max(matches, key=os.path.getmtime)
                return os.path.basename(newest)
        except Exception as exc:
            self.logger.warning('Failed to scan results directory for task %s: %s', task_id, exc)

        return None

    def resolve_result_archive_path(self, task_id: str) -> tuple[str, str]:
        directory = self.app.config.get('UPLOAD_FOLDER')
        if not directory:
            raise FileNotFoundError('Result upload directory is not configured.')

        # Primary path: resolve by files on disk first, so result reads do not depend on Celery/Redis health.
        archive_name = self.find_result_archive(task_id)
        if archive_name:
            self.logger.info('Resolved result archive for task %s directly from disk: %s', task_id, archive_name)
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
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

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
        except Exception:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
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
        canonical_structure_candidates = [
            'protenix/output/protenix_model_0.cif',
            'protenix/output/protenix_model_0.mmcif',
            'protenix/output/protenix_model_0.pdb',
        ]
        canonical_confidence = 'protenix/output/confidence_protenix_model_0.json'
        structure = next((item for item in canonical_structure_candidates if item in names), None)
        if structure and canonical_confidence in names:
            return structure, canonical_confidence

        summary_candidates = [
            item
            for item in names
            if item.lower().startswith('protenix/output/')
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

    def _build_view_archive_bytes(self, source_zip_path: str) -> bytes:
        with zipfile.ZipFile(source_zip_path, 'r') as src_zip:
            names = [name for name in src_zip.namelist() if not name.endswith('/')]
            lower_names = [name.lower() for name in names]
            is_af3 = any('af3/output/' in name for name in lower_names)
            is_protenix = any('protenix/output/' in name for name in lower_names)

            include: list[str] = []
            if is_af3:
                structure = self._choose_best_af3_structure_file(names)
                if structure:
                    include.append(structure)
                summary_candidates = [
                    name for name in names
                    if name.lower().endswith('.json')
                    and 'af3/output/' in name.lower()
                    and 'summary_confidences' in name.lower()
                ]
                confidences_candidates = [
                    name for name in names
                    if name.lower().endswith('.json')
                    and 'af3/output/' in name.lower()
                    and os.path.basename(name).lower() == 'confidences.json'
                ]
                summary = self._choose_preferred_path(summary_candidates)
                confidences = self._choose_preferred_path(confidences_candidates)
                if summary:
                    include.append(summary)
                if confidences:
                    include.append(confidences)
            elif is_protenix:
                structure, confidence = self._choose_best_protenix_files(src_zip, names)
                if structure:
                    include.append(structure)
                if confidence:
                    include.append(confidence)
                protenix_metrics = next(
                    (name for name in names if name.lower() == 'protenix/output/protenix2score_metrics.json'),
                    None,
                )
                if protenix_metrics:
                    include.append(protenix_metrics)
                affinity_candidates = [
                    name
                    for name in names
                    if name.lower().endswith('.json') and os.path.basename(name).lower() == 'affinity_data.json'
                ]
                protenix_affinity = self._choose_preferred_path(affinity_candidates)
                if protenix_affinity:
                    include.append(protenix_affinity)
            else:
                structure, confidence = self._choose_best_boltz_files(src_zip, names)
                if structure:
                    include.append(structure)
                if confidence:
                    include.append(confidence)
                affinity_candidates = [name for name in names if name.lower().endswith('.json') and 'affinity' in name.lower()]
                if affinity_candidates:
                    include.append(sorted(affinity_candidates, key=lambda item: len(item))[0])

            if not include:
                raise RuntimeError('Unable to build view archive: no renderable files found.')

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
                    out_zip.writestr(member_name, payload)
            out_buffer.seek(0)
            return out_buffer.getvalue()

    def build_or_get_view_archive(self, source_zip_path: str) -> str:
        src_stat = os.stat(source_zip_path)
        cache_schema_version = 'view-v5-boltz-ligand-aware-ranking'
        cache_seed = f'{cache_schema_version}|{source_zip_path}|{int(src_stat.st_mtime_ns)}|{src_stat.st_size}'
        cache_key = hashlib.sha256(cache_seed.encode('utf-8')).hexdigest()[:24]
        cache_dir = Path('/tmp/boltz_result_view_cache')
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f'{cache_key}.zip'
        if cache_path.exists():
            return str(cache_path)

        data = self._build_view_archive_bytes(source_zip_path)
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
