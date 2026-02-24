from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


class LeadOptMmpService:
    def __init__(
        self,
        *,
        get_redis_client_fn,
        logger,
        cache_ttl_seconds: int = 1200,
        max_cached_rows: int = 400,
        mmp_query_cache_dir: Optional[str] = None,
    ):
        self.get_redis_client = get_redis_client_fn
        self.logger = logger
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_cached_rows = max_cached_rows
        self.query_cache: Dict[str, Dict[str, Any]] = {}
        self.evidence_cache: Dict[str, Dict[str, Any]] = {}
        self.async_query_to_result: Dict[str, str] = {}
        self.transform_to_query: Dict[str, str] = {}
        self.query_prefix = 'lead_opt:mmp:query:'
        self.async_map_prefix = 'lead_opt:mmp:task_query:'
        self.transform_map_prefix = 'lead_opt:mmp:transform_query:'
        self.disk_cache_max_files = max(200, int(os.getenv('LEAD_OPT_MMP_QUERY_DISK_CACHE_MAX_FILES', '5000') or 5000))
        self.disk_cache_max_age_seconds = max(
            3600,
            int(os.getenv('LEAD_OPT_MMP_QUERY_DISK_CACHE_MAX_AGE_SECONDS', str(cache_ttl_seconds * 6)) or (cache_ttl_seconds * 6)),
        )
        self.disk_cache_cleanup_interval_seconds = max(
            30,
            int(os.getenv('LEAD_OPT_MMP_QUERY_DISK_CACHE_CLEANUP_INTERVAL_SECONDS', '120') or 120),
        )
        self._last_disk_cleanup_at = 0.0
        root_dir = str(mmp_query_cache_dir or '').strip()
        if root_dir:
            root = Path(root_dir).expanduser()
            self.disk_query_dir = root / 'queries'
            self.disk_task_dir = root / 'tasks'
            self.disk_transform_dir = root / 'transforms'
            self._ensure_disk_dirs()
        else:
            self.disk_query_dir = None
            self.disk_task_dir = None
            self.disk_transform_dir = None

    @staticmethod
    def safe_json_object(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _cleanup_cache(self) -> None:
        now = time.time()
        expired_query_ids = [
            query_id
            for query_id, payload in self.query_cache.items()
            if now - float(payload.get('created_at', 0.0)) > self.cache_ttl_seconds
        ]
        for query_id in expired_query_ids:
            self.query_cache.pop(query_id, None)

        expired_transform_ids = [
            transform_id
            for transform_id, payload in self.evidence_cache.items()
            if now - float(payload.get('created_at', 0.0)) > self.cache_ttl_seconds
        ]
        for transform_id in expired_transform_ids:
            self.evidence_cache.pop(transform_id, None)
        self._cleanup_disk_cache(now=now)

    def _cleanup_disk_cache(self, *, now: Optional[float] = None) -> None:
        if self.disk_query_dir is None and self.disk_task_dir is None and self.disk_transform_dir is None:
            return
        current = float(now if isinstance(now, (int, float)) else time.time())
        if (current - float(self._last_disk_cleanup_at or 0.0)) < float(self.disk_cache_cleanup_interval_seconds):
            return
        self._last_disk_cleanup_at = current
        cutoff = current - float(self.disk_cache_max_age_seconds)
        targets = [self.disk_query_dir, self.disk_task_dir, self.disk_transform_dir]
        for target in targets:
            if target is None:
                continue
            try:
                files = [path for path in target.glob('*.json') if path.is_file()]
            except Exception as exc:
                self.logger.warning('Failed to enumerate disk cache dir=%s: %s', target, exc)
                continue
            retained: List[Tuple[float, Path]] = []
            for path in files:
                try:
                    mtime = float(path.stat().st_mtime)
                except Exception:
                    mtime = current
                if mtime < cutoff:
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    continue
                retained.append((mtime, path))
            if len(retained) <= self.disk_cache_max_files:
                continue
            retained.sort(key=lambda item: item[0])
            overflow = len(retained) - self.disk_cache_max_files
            for _, path in retained[:overflow]:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _query_cache_key(self, query_id: str) -> str:
        return f'{self.query_prefix}{query_id}'

    def _async_map_key(self, task_id: str) -> str:
        return f'{self.async_map_prefix}{task_id}'

    def _transform_query_key(self, transform_id: str) -> str:
        return f'{self.transform_map_prefix}{transform_id}'

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha1(str(token or '').encode('utf-8')).hexdigest()

    def _ensure_disk_dirs(self) -> None:
        if self.disk_query_dir is not None:
            self.disk_query_dir.mkdir(parents=True, exist_ok=True)
        if self.disk_task_dir is not None:
            self.disk_task_dir.mkdir(parents=True, exist_ok=True)
        if self.disk_transform_dir is not None:
            self.disk_transform_dir.mkdir(parents=True, exist_ok=True)

    def _query_disk_path(self, query_id: str) -> Optional[Path]:
        if self.disk_query_dir is None:
            return None
        return self.disk_query_dir / f'{self._token_digest(query_id)}.json'

    def _task_disk_path(self, task_id: str) -> Optional[Path]:
        if self.disk_task_dir is None:
            return None
        return self.disk_task_dir / f'{self._token_digest(task_id)}.json'

    def _transform_disk_path(self, transform_id: str) -> Optional[Path]:
        if self.disk_transform_dir is None:
            return None
        return self.disk_transform_dir / f'{self._token_digest(transform_id)}.json'

    def _disk_write_json(self, path: Optional[Path], payload: Dict[str, Any]) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(f'{path.suffix}.tmp')
            temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            os.replace(temp_path, path)
        except Exception as exc:
            self.logger.warning('Failed to write disk cache file=%s: %s', path, exc)

    def _disk_read_json(self, path: Optional[Path]) -> Optional[Dict[str, Any]]:
        if path is None or not path.exists():
            return None
        try:
            parsed = json.loads(path.read_text(encoding='utf-8'))
            return parsed if isinstance(parsed, dict) else None
        except Exception as exc:
            self.logger.warning('Failed to read disk cache file=%s: %s', path, exc)
            return None

    def _redis_set_json(self, redis_key: str, payload: Dict[str, Any], ttl: Optional[int] = None) -> None:
        ttl_seconds = ttl if ttl is not None else self.cache_ttl_seconds
        try:
            redis_client = self.get_redis_client()
            redis_client.setex(redis_key, ttl_seconds, json.dumps(payload))
        except Exception as exc:
            self.logger.warning('Failed to write redis cache key=%s: %s', redis_key, exc)

    def _redis_get_json(self, redis_key: str) -> Optional[Dict[str, Any]]:
        try:
            redis_client = self.get_redis_client()
            raw = redis_client.get(redis_key)
            if not raw:
                return None
            text = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else str(raw)
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception as exc:
            self.logger.warning('Failed to read redis cache key=%s: %s', redis_key, exc)
            return None

    def get_cached_query_payload(self, query_id: str) -> Optional[Dict[str, Any]]:
        token = str(query_id or '').strip()
        if not token:
            return None
        payload = self.query_cache.get(token)
        if payload:
            return payload

        payload = self._redis_get_json(self._query_cache_key(token))
        if payload:
            self.query_cache[token] = payload
            return payload

        payload = self._disk_read_json(self._query_disk_path(token))
        if payload and str(payload.get('query_id') or '').strip() == token:
            self.query_cache[token] = payload
        return payload

    def get_cached_evidence_payload(self, transform_id: str) -> Optional[Dict[str, Any]]:
        token = str(transform_id or '').strip()
        if not token:
            return None

        payload = self.evidence_cache.get(token)
        if payload:
            return payload

        query_id = str(self.transform_to_query.get(token) or '').strip()
        if not query_id:
            map_payload = self._redis_get_json(self._transform_query_key(token))
            query_id = str(map_payload.get('query_id') or '').strip() if map_payload else ''
            if query_id:
                self.transform_to_query[token] = query_id
        if not query_id:
            map_payload = self._disk_read_json(self._transform_disk_path(token))
            query_id = str(map_payload.get('query_id') or '').strip() if map_payload else ''
            if query_id:
                self.transform_to_query[token] = query_id
        if not query_id:
            return None

        query_payload = self.get_cached_query_payload(query_id)
        if not query_payload:
            return None

        transform = {}
        for row in query_payload.get('transforms', []):
            if str((row or {}).get('transform_id') or '') == token:
                transform = row if isinstance(row, dict) else {}
                break

        rows = [
            row
            for row in (query_payload.get('rows', []) if isinstance(query_payload.get('rows'), list) else [])
            if str((row or {}).get('transform_id') or '') == token
        ]

        payload = {
            'created_at': time.time(),
            'transform': transform,
            'rows': rows,
        }
        self.evidence_cache[token] = payload
        return payload

    def get_cached_query_id_for_task(self, task_id: str) -> str:
        token = str(task_id or '').strip()
        if not token:
            return ''

        query_id = str(self.async_query_to_result.get(token) or '').strip()
        if query_id:
            return query_id

        payload = self._redis_get_json(self._async_map_key(token))
        query_id = str(payload.get('query_id') or '').strip() if payload else ''
        if query_id:
            self.async_query_to_result[token] = query_id
            return query_id

        payload = self._disk_read_json(self._task_disk_path(token))
        query_id = str(payload.get('query_id') or '').strip() if payload else ''
        if query_id:
            self.async_query_to_result[token] = query_id
        return query_id

    def materialize_query_result_cache(self, result_payload: Dict[str, Any], *, task_id: Optional[str] = None) -> Dict[str, Any]:
        self._cleanup_cache()

        query_id = str(result_payload.get('query_id') or '').strip()
        if not query_id:
            query_id = uuid.uuid4().hex
        created_at = time.time()

        rows = result_payload.get('rows')
        rows = rows if isinstance(rows, list) else []
        cached_rows = rows[: self.max_cached_rows]

        global_rows = result_payload.get('global_rows')
        global_rows = global_rows if isinstance(global_rows, list) else rows

        transforms = result_payload.get('transforms')
        transforms = transforms if isinstance(transforms, list) else []

        global_transforms = result_payload.get('global_transforms')
        global_transforms = global_transforms if isinstance(global_transforms, list) else transforms

        clusters = result_payload.get('clusters')
        clusters = clusters if isinstance(clusters, list) else []

        query_mode = str(result_payload.get('query_mode') or 'one-to-many').strip().lower()
        aggregation_type = str(result_payload.get('aggregation_type') or '').strip().lower()
        if aggregation_type not in {'individual_transforms', 'group_by_fragment'}:
            aggregation_type = 'group_by_fragment' if query_mode == 'many-to-many' else 'individual_transforms'
        grouped_by_environment_raw = result_payload.get('grouped_by_environment')
        if isinstance(grouped_by_environment_raw, bool):
            grouped_by_environment = grouped_by_environment_raw
        else:
            token = str(grouped_by_environment_raw or '').strip().lower()
            if token in {'1', 'true', 'yes', 'on'}:
                grouped_by_environment = True
            elif token in {'0', 'false', 'no', 'off'}:
                grouped_by_environment = False
            else:
                grouped_by_environment = False
        variable_spec = self.safe_json_object(result_payload.get('variable_spec'))
        constant_spec = self.safe_json_object(result_payload.get('constant_spec'))
        property_targets = self.safe_json_object(result_payload.get('property_targets'))
        direction = str(result_payload.get('direction') or property_targets.get('direction') or 'increase').strip().lower()
        rule_env_radius = result_payload.get('rule_env_radius')
        min_pairs = max(1, int(result_payload.get('min_pairs') or 1))

        stats = self.safe_json_object(result_payload.get('stats'))
        if not stats:
            stats = {
                'global_rows': len(global_rows),
                'environment_rows': len(rows),
            }

        query_cache_payload = {
            'created_at': created_at,
            'query_id': query_id,
            'query_mol': str(result_payload.get('query_mol') or ''),
            'query_mode': query_mode,
            'aggregation_type': aggregation_type,
            'grouped_by_environment': grouped_by_environment,
            'mmp_database_id': str(result_payload.get('mmp_database_id') or ''),
            'mmp_database_label': str(result_payload.get('mmp_database_label') or ''),
            'mmp_database_schema': str(result_payload.get('mmp_database_schema') or ''),
            'property_targets': property_targets,
            'direction': direction,
            'variable_spec': variable_spec,
            'constant_spec': constant_spec,
            'rows': cached_rows,
            'transforms': transforms,
            'clusters': clusters,
            'global_transforms': global_transforms,
            'global_rows_count': len(global_rows),
            'environment_rows_count': len(rows),
            'rule_env_radius': rule_env_radius,
            'min_pairs': min_pairs,
        }
        self.query_cache[query_id] = query_cache_payload
        self._redis_set_json(self._query_cache_key(query_id), query_cache_payload)
        self._disk_write_json(self._query_disk_path(query_id), query_cache_payload)

        for transform in transforms:
            transform_id = str(transform.get('transform_id') or '')
            if not transform_id:
                continue
            self.transform_to_query[transform_id] = query_id
            transform_map_payload = {'query_id': query_id}
            self._redis_set_json(self._transform_query_key(transform_id), transform_map_payload)
            self._disk_write_json(self._transform_disk_path(transform_id), transform_map_payload)
            evidence_rows = [row for row in cached_rows if str(row.get('transform_id') or '') == transform_id]
            self.evidence_cache[transform_id] = {
                'created_at': created_at,
                'transform': transform,
                'rows': evidence_rows,
            }

        if task_id:
            self.async_query_to_result[str(task_id)] = query_id
            task_map_payload = {'query_id': query_id}
            self._redis_set_json(self._async_map_key(str(task_id)), task_map_payload)
            self._disk_write_json(self._task_disk_path(str(task_id)), task_map_payload)

        return {
            'query_id': query_id,
            'query_mode': query_mode,
            'aggregation_type': aggregation_type,
            'grouped_by_environment': grouped_by_environment,
            'mmp_database_id': str(result_payload.get('mmp_database_id') or ''),
            'mmp_database_label': str(result_payload.get('mmp_database_label') or ''),
            'mmp_database_schema': str(result_payload.get('mmp_database_schema') or ''),
            'variable_spec': variable_spec,
            'constant_spec': constant_spec,
            'rule_env_radius': rule_env_radius,
            'transforms': transforms,
            'global_transforms': global_transforms,
            'clusters': clusters,
            'count': len(transforms),
            'global_count': len(global_transforms),
            'min_pairs': min_pairs,
            'stats': stats,
        }

    @staticmethod
    def attachment_fragment_smiles_from_atom_indices(parent_mol: Any, atom_indices: List[int]) -> str:
        try:
            from rdkit import Chem
        except Exception:
            return ''

        atom_set = {int(idx) for idx in atom_indices if isinstance(idx, (int, float))}
        if not atom_set:
            return ''
        try:
            from lead_optimization.mmp_query_service import derive_attachment_query_from_atom_indices
        except Exception:
            return ''
        query = derive_attachment_query_from_atom_indices(parent_mol, sorted(atom_set), expand_rings=False)
        if not query or '*' not in query:
            return ''

        if Chem.MolFromSmiles(query) is not None:
            return query

        # Keep attachment-aware output only when we can normalize it to stable SMILES.
        query_mol = Chem.MolFromSmarts(query)
        if query_mol is None:
            return ''
        try:
            normalized = Chem.MolToSmiles(query_mol, canonical=True)
        except Exception:
            return ''
        if not normalized or '*' not in normalized:
            return ''
        if Chem.MolFromSmiles(normalized) is None:
            return ''
        return normalized

    @staticmethod
    def decode_smiles_atom_index_from_name(atom_name: str) -> Optional[int]:
        token = str(atom_name or '').strip().upper()
        if len(token) != 4 or not token.startswith('Q'):
            return None
        alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        value = 0
        for ch in token[1:]:
            idx = alphabet.find(ch)
            if idx < 0:
                return None
            value = value * 36 + idx
        return value

    @staticmethod
    def compute_smiles_properties(smiles: str) -> Dict[str, float]:
        try:
            from rdkit import Chem
            from rdkit.Chem import Descriptors

            mol = Chem.MolFromSmiles(smiles)
            if not mol:
                return {}
            return {
                'molecular_weight': float(Descriptors.MolWt(mol)),
                'logp': float(Descriptors.MolLogP(mol)),
                'tpsa': float(Descriptors.TPSA(mol)),
            }
        except Exception:
            return {}

    @staticmethod
    def _percentile(values: List[float], p: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        idx = (len(ordered) - 1) * p
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return ordered[lo]
        weight = idx - lo
        return ordered[lo] * (1.0 - weight) + ordered[hi] * weight

    def build_mmp_clusters(
        self,
        transforms: List[Dict[str, Any]],
        group_by: str = 'to',
        min_pairs: int = 1,
        direction: str = 'increase',
    ) -> List[Dict[str, Any]]:
        key_field = 'to_smiles' if group_by == 'to' else 'from_smiles' if group_by == 'from' else 'rule_env_radius'
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in transforms:
            if int(item.get('n_pairs', 0) or 0) < min_pairs:
                continue
            key = str(item.get(key_field, ''))
            grouped.setdefault(key, []).append(item)

        clusters: List[Dict[str, Any]] = []
        for key, items in grouped.items():
            cluster_size = len(items)
            deltas = [float(i.get('median_delta', 0.0) or 0.0) for i in items]
            improved = sum(1 for value in deltas if (value < 0 if direction == 'decrease' else value > 0))
            clusters.append({
                'cluster_id': hashlib.sha1(f'{group_by}:{key}'.encode('utf-8')).hexdigest()[:16],
                'group_by': group_by,
                'group_key': key,
                'cluster_size': cluster_size,
                'n_pairs': int(sum(int(i.get('n_pairs', 0) or 0) for i in items)),
                '%median_improved': (100.0 * improved / cluster_size) if cluster_size > 0 else 0.0,
                'median_delta': self._percentile(deltas, 0.5),
                'dispersion': self._percentile(deltas, 0.75) - self._percentile(deltas, 0.25),
                'evidence_strength': float(
                    sum(float(i.get('evidence_strength', 0.0) or 0.0) for i in items) / max(1, cluster_size)
                ),
                'transform_ids': [str(i.get('transform_id')) for i in items],
            })

        clusters.sort(key=lambda item: (item.get('evidence_strength', 0.0), item.get('cluster_size', 0)), reverse=True)
        return clusters

    @staticmethod
    def passes_property_constraints_simple(properties: Dict[str, float], constraints: Dict[str, Any]) -> bool:
        if not constraints:
            return True
        aliases = {
            'mw': 'molecular_weight',
            'logp': 'logp',
            'tpsa': 'tpsa',
        }
        for short_key, prop_key in aliases.items():
            value = float(properties.get(prop_key, 0.0) or 0.0)
            min_key = f'{short_key}_min'
            max_key = f'{short_key}_max'
            min_value = constraints.get(min_key)
            max_value = constraints.get(max_key)
            if isinstance(min_value, (int, float)) and value < float(min_value):
                return False
            if isinstance(max_value, (int, float)) and value > float(max_value):
                return False
        return True

    @staticmethod
    def build_lead_opt_prediction_yaml(
        protein_sequence: str,
        candidate_smiles: str,
        target_chain: str,
        ligand_chain: str,
        backend: str,
        pocket_residues: List[Dict[str, Any]],
    ) -> str:
        target_chain = str(target_chain or 'A').strip() or 'A'
        ligand_chain = str(ligand_chain or 'L').strip() or 'L'
        backend = str(backend or 'boltz').strip().lower()
        if target_chain == ligand_chain:
            raise ValueError(
                f"target_chain ({target_chain}) and ligand_chain ({ligand_chain}) must be different."
            )

        payload: Dict[str, Any] = {
            'version': 1,
            'sequences': [
                {
                    'protein': {
                        'id': target_chain,
                        'sequence': str(protein_sequence or '').replace('\n', '').replace(' ', ''),
                    }
                },
                {
                    'ligand': {
                        'id': ligand_chain,
                        'smiles': str(candidate_smiles or '').strip(),
                    }
                },
            ],
            'properties': [
                {
                    'affinity': {
                        'target': target_chain,
                        'binder': ligand_chain,
                    }
                }
            ],
        }

        protein_seq_len = len(str(protein_sequence or "").replace("\n", "").replace(" ", ""))
        if pocket_residues:
            ranked = sorted(
                [row for row in pocket_residues if str(row.get('chain_id') or '').strip() == target_chain],
                key=lambda row: float(row.get('min_distance', 999.0) or 999.0),
            )
            if not ranked:
                ranked = sorted(pocket_residues, key=lambda row: float(row.get('min_distance', 999.0) or 999.0))
            ranked = ranked[:16]
            contacts = []
            invalid_rows: List[str] = []
            for row in ranked:
                chain_id = str(row.get('chain_id') or target_chain).strip() or target_chain
                residue_number = int(row.get('residue_number') or 0)
                if chain_id != target_chain:
                    invalid_rows.append(f"{chain_id}:{residue_number}")
                    continue
                if residue_number <= 0 or (protein_seq_len > 0 and residue_number > protein_seq_len):
                    invalid_rows.append(f"{chain_id}:{residue_number}")
                    continue
                contacts.append([chain_id, residue_number])
            if invalid_rows:
                preview = ", ".join(invalid_rows[:6])
                raise ValueError(
                    "Pocket residues are inconsistent with target chain/sequence: "
                    f"target_chain={target_chain}, sequence_len={protein_seq_len}, invalid={preview}"
                )

            if contacts and backend in {'boltz', 'protenix'}:
                payload['constraints'] = [{
                    'pocket': {
                        'binder': ligand_chain,
                        'contacts': contacts,
                        'max_distance': 6.0,
                        'force': True,
                    }
                }]
            # AlphaFold3 does not support pocket/contact constraints for SMILES ligands.
            # Keep payload unconstrained for AF3 to improve robustness.

        return yaml.dump(payload, default_flow_style=False, allow_unicode=True, sort_keys=False)
