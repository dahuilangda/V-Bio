from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
_METHOD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _normalize_batch_id(raw: str) -> str:
    token = str(raw or "").strip()
    if token and _BATCH_ID_RE.match(token):
        return token
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"batch_{stamp}_{uuid.uuid4().hex[:8]}"


def _normalize_method_id(raw: str) -> str:
    token = str(raw or "").strip()
    if token and _METHOD_ID_RE.match(token):
        return token
    return f"method_{uuid.uuid4().hex[:10]}"


def _slugify_token(raw: str, *, fallback: str = "item", max_len: int = 40) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
    if not token:
        token = fallback
    if max_len > 0:
        token = token[:max_len]
    return token or fallback


def _auto_method_id(output_property: str) -> str:
    key = str(output_property or "").strip().lower()
    digest = uuid.uuid5(uuid.NAMESPACE_DNS, f"mmp_method:{key}").hex[:12]
    slug = _slugify_token(key, fallback="prop", max_len=24)
    return f"method_auto_{slug}_{digest}"


def _auto_mapping_id(database_id: str, source_property: str) -> str:
    db_token = str(database_id or "").strip()
    src_token = str(source_property or "").strip().lower()
    digest = uuid.uuid5(uuid.NAMESPACE_DNS, f"mmp_mapping:{db_token}:{src_token}").hex[:12]
    slug = _slugify_token(src_token, fallback="prop", max_len=20)
    return f"map_auto_{slug}_{digest}"


def _normalize_property_token(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").strip().lower())


def _canonical_property_family(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return ""
    compact = token.replace(" ", "").replace("-", "_")
    matcher = re.match(
        r"^(?:p?ic50|ic50_(?:nm|um)|ki|kd|ec50|ac50|log10|neglog10|neg_log10)\((.+)\)$",
        compact,
    )
    if matcher:
        compact = matcher.group(1)
    compact = re.sub(
        r"^(?:p?ic50|ic50_(?:nm|um)|ki|kd|ec50|ac50|log10|neglog10|neg_log10)[_]+",
        "",
        compact,
    )
    compact = re.sub(
        r"[_]+(?:p?ic50|ic50_(?:nm|um)|ki|kd|ec50|ac50|log10|neglog10|neg_log10)$",
        "",
        compact,
    )
    # Normalize common unit wrappers/suffixes so "CYP3A4" and "CYP3A4 (uM)" share one family.
    compact = re.sub(r"\((?:um|nm|mm|pm|fm)\)$", "", compact)
    compact = re.sub(r"[_]+(?:um|nm|mm|pm|fm)$", "", compact)
    compact = re.sub(r"^(?:um|nm|mm|pm|fm)[_]+", "", compact)
    normalized = _normalize_property_token(compact)
    if normalized:
        return normalized
    return _normalize_property_token(token)


def _property_preference_rank(prop: str, family: str) -> tuple[int, int, int, str]:
    normalized = _normalize_property_token(prop)
    token = str(prop or "")
    is_exact_family = 0 if normalized == family else 1
    looks_derived = 1 if re.search(r"(?:p?ic50|ic50|ki|kd|ec50|ac50|log10|neglog)", token, flags=re.IGNORECASE) else 0
    return (is_exact_family, looks_derived, len(token), token.lower())


def _read_text_field(payload: Dict[str, Any], *keys: str, fallback: str = "") -> str:
    for key in keys:
        if key in payload:
            return str(payload.get(key) or "").strip()
    return str(fallback or "").strip()


def _score_method_row(row: Dict[str, Any]) -> int:
    fields = (
        "key",
        "name",
        "output_property",
        "input_unit",
        "output_unit",
        "display_unit",
        "import_transform",
        "display_transform",
        "category",
        "description",
        "reference",
    )
    return sum(1 for key in fields if str((row or {}).get(key) or "").strip())


def _method_identity_key(row: Dict[str, Any]) -> str:
    output_token = _normalize_property_token((row or {}).get("output_property"))
    if output_token:
        return f"output:{output_token}"
    key_token = _normalize_property_token((row or {}).get("key"))
    name_token = _normalize_property_token((row or {}).get("name"))
    if key_token and name_token:
        return f"key_name:{key_token}:{name_token}"
    if key_token:
        return f"key:{key_token}"
    if name_token:
        return f"name:{name_token}"
    method_id = str((row or {}).get("id") or "").strip()
    return f"id:{method_id}"


def _is_preferred_method_row(candidate: Dict[str, Any], current: Dict[str, Any]) -> bool:
    candidate_updated = str((candidate or {}).get("updated_at") or "")
    current_updated = str((current or {}).get("updated_at") or "")
    if candidate_updated > current_updated:
        return True
    if candidate_updated < current_updated:
        return False
    candidate_score = _score_method_row(candidate)
    current_score = _score_method_row(current)
    if candidate_score > current_score:
        return True
    if candidate_score < current_score:
        return False
    candidate_id = str((candidate or {}).get("id") or "").strip()
    current_id = str((current or {}).get("id") or "").strip()
    return candidate_id < current_id


class MmpLifecycleAdminStore:
    """File-backed state for MMP lifecycle admin workflows.

    State is persisted under a dedicated root so service restarts do not lose
    batch/method/mapping metadata.
    """

    def __init__(self, root_dir: str = "") -> None:
        default_root = Path(__file__).resolve().parents[1] / "data" / "mmp_lifecycle_admin"
        resolved_root = Path(str(root_dir or os.getenv("LEAD_OPT_MMP_LIFECYCLE_ADMIN_DIR", "")).strip() or default_root)
        self.root_dir = resolved_root
        self.state_file = self.root_dir / "state.json"
        self.batch_upload_dir = self.root_dir / "uploads"
        self._lock = threading.Lock()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.batch_upload_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_file.exists():
            _atomic_json_write(self.state_file, self._empty_state())

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {
            "version": 1,
            "updated_at": _utc_now_iso(),
            "methods": [],
            "property_mappings": [],
            "pending_database_sync": [],
            "batches": [],
        }

    def _read_state(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return self._empty_state()
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                return self._empty_state()
            base = self._empty_state()
            base.update(payload)
            if not isinstance(base.get("methods"), list):
                base["methods"] = []
            if not isinstance(base.get("property_mappings"), list):
                base["property_mappings"] = []
            if not isinstance(base.get("pending_database_sync"), list):
                base["pending_database_sync"] = []
            if not isinstance(base.get("batches"), list):
                base["batches"] = []
            original_methods = json.dumps(base.get("methods", []), sort_keys=True, ensure_ascii=False)
            original_mappings = json.dumps(base.get("property_mappings", []), sort_keys=True, ensure_ascii=False)
            original_batches = json.dumps(base.get("batches", []), sort_keys=True, ensure_ascii=False)
            normalized_methods, method_aliases = self._normalize_method_collection_with_aliases(base.get("methods"))
            base["methods"] = normalized_methods
            self._apply_method_aliases_to_state(base, method_aliases)
            normalized_methods_dump = json.dumps(base.get("methods", []), sort_keys=True, ensure_ascii=False)
            normalized_mappings_dump = json.dumps(base.get("property_mappings", []), sort_keys=True, ensure_ascii=False)
            normalized_batches_dump = json.dumps(base.get("batches", []), sort_keys=True, ensure_ascii=False)
            if (
                normalized_methods_dump != original_methods
                or normalized_mappings_dump != original_mappings
                or normalized_batches_dump != original_batches
            ):
                base["updated_at"] = _utc_now_iso()
                _atomic_json_write(self.state_file, base)
            return base
        except Exception:
            return self._empty_state()

    def _write_state(self, state: Dict[str, Any]) -> None:
        state["updated_at"] = _utc_now_iso()
        _atomic_json_write(self.state_file, state)

    @staticmethod
    def _normalize_method_row(raw: Dict[str, Any], *, default_now: str = "") -> Dict[str, Any]:
        row = dict(raw or {})
        now = str(default_now or _utc_now_iso())
        token = str(row.get("id") or "").strip()
        row["id"] = _normalize_method_id(token)
        row["key"] = str(row.get("key") or "").strip()
        row["name"] = str(row.get("name") or "").strip()
        row["output_property"] = str(row.get("output_property") or row.get("outputProperty") or "").strip()
        row["category"] = str(row.get("category") or "").strip()
        row["description"] = str(row.get("description") or "").strip()
        row["reference"] = str(row.get("reference") or "").strip()
        row["input_unit"] = str(row.get("input_unit") or row.get("inputUnit") or "").strip()
        row["output_unit"] = str(row.get("output_unit") or row.get("outputUnit") or "").strip()
        row["display_unit"] = str(row.get("display_unit") or row.get("displayUnit") or "").strip()
        row["import_transform"] = str(row.get("import_transform") or row.get("importTransform") or "none").strip() or "none"
        row["display_transform"] = str(row.get("display_transform") or row.get("displayTransform") or "none").strip() or "none"
        row["created_at"] = str(row.get("created_at") or now)
        row["updated_at"] = str(row.get("updated_at") or now)
        return row

    @classmethod
    def _merge_method_rows(
        cls,
        primary: Dict[str, Any],
        secondary: Dict[str, Any],
        *,
        default_now: str,
    ) -> Dict[str, Any]:
        merged = dict(secondary or {})
        merged.update(primary or {})
        for field in ("input_unit", "output_unit", "display_unit", "import_transform", "display_transform", "description"):
            if str(merged.get(field) or "").strip():
                continue
            fallback_value = str((secondary or {}).get(field) or "").strip()
            if fallback_value:
                merged[field] = fallback_value
        return cls._normalize_method_row(merged, default_now=default_now)

    @staticmethod
    def _collapse_method_aliases(aliases: Dict[str, str]) -> Dict[str, str]:
        collapsed: Dict[str, str] = {}
        for source, target in aliases.items():
            src = str(source or "").strip()
            dst = str(target or "").strip()
            if not src or not dst or src == dst:
                continue
            seen = {src}
            while dst in aliases and aliases[dst] not in seen:
                seen.add(dst)
                dst = str(aliases[dst] or "").strip()
                if not dst:
                    break
            if dst and dst != src:
                collapsed[src] = dst
        return collapsed

    @classmethod
    def _dedupe_methods(cls, methods: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
        now = _utc_now_iso()
        # Phase 1: strict dedupe by method id.
        by_id: Dict[str, Dict[str, Any]] = {}
        by_id_order: List[str] = []
        for item in methods if isinstance(methods, list) else []:
            if not isinstance(item, dict):
                continue
            row = cls._normalize_method_row(item, default_now=now)
            method_id = str(row.get("id") or "").strip()
            if not method_id:
                continue
            current = by_id.get(method_id)
            if current is None:
                by_id[method_id] = row
                by_id_order.append(method_id)
                continue
            primary = row if _is_preferred_method_row(row, current) else current
            secondary = current if primary is row else row
            by_id[method_id] = cls._merge_method_rows(primary, secondary, default_now=now)

        # Phase 2: semantic dedupe by normalized method identity.
        aliases: Dict[str, str] = {}
        by_identity: Dict[str, Dict[str, Any]] = {}
        identity_order: List[str] = []
        for method_id in by_id_order:
            row = dict(by_id.get(method_id) or {})
            current_id = str(row.get("id") or "").strip()
            if not current_id:
                continue
            identity = _method_identity_key(row)
            existing = by_identity.get(identity)
            if existing is None:
                by_identity[identity] = row
                identity_order.append(identity)
                continue
            existing_id = str(existing.get("id") or "").strip()
            primary = row if _is_preferred_method_row(row, existing) else existing
            secondary = existing if primary is row else row
            merged = cls._merge_method_rows(primary, secondary, default_now=now)
            winner_id = str(merged.get("id") or "").strip()
            loser_id = existing_id if winner_id == current_id else current_id
            if loser_id and winner_id and loser_id != winner_id:
                aliases[loser_id] = winner_id
            by_identity[identity] = merged

        collapsed_aliases = cls._collapse_method_aliases(aliases)
        normalized = [by_identity[key] for key in identity_order if key in by_identity]
        return normalized, collapsed_aliases

    @classmethod
    def _normalize_method_collection_with_aliases(cls, methods: Any) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
        rows = [dict(item) for item in methods if isinstance(item, dict)] if isinstance(methods, list) else []
        return cls._dedupe_methods(rows)

    @classmethod
    def _normalize_method_collection(cls, methods: Any) -> List[Dict[str, Any]]:
        normalized, _ = cls._normalize_method_collection_with_aliases(methods)
        return normalized

    @staticmethod
    def _apply_method_aliases_to_state(state: Dict[str, Any], aliases: Dict[str, str]) -> bool:
        if not aliases:
            return False
        changed = False

        mappings = state.get("property_mappings")
        if isinstance(mappings, list):
            next_mappings: List[Dict[str, Any]] = []
            for item in mappings:
                if not isinstance(item, dict):
                    next_mappings.append(item)
                    continue
                row = dict(item)
                method_id = str(row.get("method_id") or "").strip()
                alias = str(aliases.get(method_id) or "").strip()
                if alias and alias != method_id:
                    row["method_id"] = alias
                    changed = True
                next_mappings.append(row)
            state["property_mappings"] = next_mappings

        batches = state.get("batches")
        if isinstance(batches, list):
            next_batches: List[Dict[str, Any]] = []
            for item in batches:
                if not isinstance(item, dict):
                    next_batches.append(item)
                    continue
                row = dict(item)
                files = dict(row.get("files") or {}) if isinstance(row.get("files"), dict) else {}
                experiments = dict(files.get("experiments") or {}) if isinstance(files.get("experiments"), dict) else {}
                cfg = dict(experiments.get("column_config") or {}) if isinstance(experiments.get("column_config"), dict) else {}
                raw_map = cfg.get("activity_method_map")
                if isinstance(raw_map, dict):
                    next_map: Dict[str, str] = {}
                    map_changed = False
                    for key, value in raw_map.items():
                        method_id = str(value or "").strip()
                        alias = str(aliases.get(method_id) or "").strip()
                        next_value = alias or method_id
                        if next_value != method_id:
                            map_changed = True
                        next_map[str(key)] = next_value
                    if map_changed:
                        cfg["activity_method_map"] = next_map
                        changed = True
                legacy_method = str(cfg.get("assay_method_id") or "").strip()
                legacy_alias = str(aliases.get(legacy_method) or "").strip()
                if legacy_alias and legacy_alias != legacy_method:
                    cfg["assay_method_id"] = legacy_alias
                    changed = True
                if cfg:
                    experiments["column_config"] = cfg
                if experiments:
                    files["experiments"] = experiments
                if files:
                    row["files"] = files
                next_batches.append(row)
            state["batches"] = next_batches

        return changed

    @staticmethod
    def _normalize_batch_payload(raw: Dict[str, Any], *, for_update: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if (not for_update) or ("name" in raw):
            payload["name"] = str(raw.get("name") or "").strip()
        if (not for_update) or ("description" in raw):
            payload["description"] = str(raw.get("description") or "").strip()
        if (not for_update) or ("notes" in raw):
            payload["notes"] = str(raw.get("notes") or "").strip()
        if (not for_update) or ("selected_database_id" in raw):
            payload["selected_database_id"] = str(raw.get("selected_database_id") or "").strip()
        return payload

    @staticmethod
    def _append_status_history(row: Dict[str, Any], *, status: str, event: str, note: str = "") -> Dict[str, Any]:
        history = list(row.get("status_history") or [])
        history.append(
            {
                "at": _utc_now_iso(),
                "status": str(status or "").strip().lower(),
                "event": str(event or "").strip().lower(),
                "note": str(note or "").strip(),
            }
        )
        row["status_history"] = history
        return row

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            state = self._read_state()
            rows = [dict(item) for item in state.get("batches", []) if isinstance(item, dict)]
        rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return rows

    def get_batch(self, batch_id: str) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        if not token:
            raise ValueError("batch_id is required")
        with self._lock:
            state = self._read_state()
            for item in state.get("batches", []):
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or "").strip() == token:
                    return dict(item)
        raise ValueError(f"Batch '{token}' not found")

    def create_batch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payload if isinstance(payload, dict) else {}
        patch = self._normalize_batch_payload(raw, for_update=False)
        name = str(patch.get("name") or "").strip()
        if not name:
            raise ValueError("batch name is required")
        batch_id = _normalize_batch_id(str(raw.get("id") or "").strip())
        now = _utc_now_iso()
        entry = {
            "id": batch_id,
            "name": name,
            "description": str(patch.get("description") or ""),
            "notes": str(patch.get("notes") or ""),
            "status": "draft",
            "selected_database_id": str(patch.get("selected_database_id") or ""),
            "created_at": now,
            "updated_at": now,
            "files": {
                "compounds": None,
                "experiments": None,
                "generated_property_import": None,
            },
            "last_check": None,
            "review": None,
            "approval": None,
            "rejection": None,
            "apply_history": [],
            "rollback_history": [],
            "status_history": [
                {
                    "at": now,
                    "status": "draft",
                    "event": "created",
                    "note": "",
                }
            ],
        }
        with self._lock:
            state = self._read_state()
            for item in state.get("batches", []):
                if str((item or {}).get("id") or "").strip() == batch_id:
                    raise ValueError(f"Batch '{batch_id}' already exists")
            state["batches"].append(entry)
            self._write_state(state)
        (self.batch_upload_dir / batch_id).mkdir(parents=True, exist_ok=True)
        return entry

    def update_batch(self, batch_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        if not token:
            raise ValueError("batch_id is required")
        patch = self._normalize_batch_payload(payload if isinstance(payload, dict) else {}, for_update=True)
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                before_database_id = str(row.get("selected_database_id") or "").strip()
                for key, value in patch.items():
                    row[key] = value
                if "selected_database_id" in patch:
                    next_database_id = str(patch.get("selected_database_id") or "").strip()
                    if next_database_id != before_database_id:
                        row["status"] = "draft"
                        row["last_check"] = None
                        row["review"] = None
                        row["approval"] = None
                        row["rejection"] = None
                        row = self._append_status_history(
                            row,
                            status="draft",
                            event="database_changed",
                            note=f"{before_database_id} -> {next_database_id}",
                        )
                row["updated_at"] = _utc_now_iso()
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def delete_batch(self, batch_id: str) -> None:
        token = str(batch_id or "").strip()
        if not token:
            raise ValueError("batch_id is required")
        with self._lock:
            state = self._read_state()
            before = len(state.get("batches", []))
            state["batches"] = [
                item
                for item in state.get("batches", [])
                if str((item or {}).get("id") or "").strip() != token
            ]
            if len(state["batches"]) == before:
                raise ValueError(f"Batch '{token}' not found")
            self._write_state(state)
        upload_root = self.batch_upload_dir / token
        shutil.rmtree(upload_root, ignore_errors=True)

    def attach_batch_file(
        self,
        batch_id: str,
        *,
        file_kind: str,
        source_filename: str,
        body: bytes,
        column_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        kind = str(file_kind or "").strip().lower()
        if kind not in {"compounds", "experiments"}:
            raise ValueError("file_kind must be compounds or experiments")
        if not token:
            raise ValueError("batch_id is required")
        safe_name = Path(str(source_filename or "").strip() or f"{kind}.tsv").name
        ext = Path(safe_name).suffix or ".tsv"
        write_name = f"{kind}{ext}"
        target_dir = self.batch_upload_dir / token
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / write_name
        with open(target_path, "wb") as handle:
            handle.write(body)

        metadata = {
            "batch_id": token,
            "stored_name": write_name,
            "original_name": safe_name,
            "size": int(len(body)),
            "uploaded_at": _utc_now_iso(),
            "path": str(target_path),
            "path_rel": str(target_path.relative_to(self.root_dir)),
            "column_config": column_config or {},
        }

        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                files = dict(row.get("files") or {})
                files[kind] = metadata
                row["files"] = files
                row["status"] = "draft"
                row["last_check"] = None
                row["review"] = None
                row["approval"] = None
                row["rejection"] = None
                row = self._append_status_history(
                    row,
                    status="draft",
                    event=f"upload_{kind}",
                    note=str(metadata.get("original_name") or ""),
                )
                row["updated_at"] = _utc_now_iso()
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def set_generated_property_import_file(self, batch_id: str, file_path: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        path = str(file_path or "").strip()
        if not token or not path:
            raise ValueError("batch_id and file_path are required")
        payload = {
            "path": path,
            "stored_name": Path(path).name,
            "size": int(meta.get("size") or 0),
            "generated_at": _utc_now_iso(),
            "row_count": int(meta.get("row_count") or 0),
            "property_count": int(meta.get("property_count") or 0),
            "database_id": str(meta.get("database_id") or "").strip(),
        }
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                files = dict(row.get("files") or {})
                files["generated_property_import"] = payload
                row["files"] = files
                row["updated_at"] = _utc_now_iso()
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def clear_batch_file(self, batch_id: str, *, file_kind: str) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        kind = str(file_kind or "").strip().lower()
        if not token:
            raise ValueError("batch_id is required")
        if kind not in {"compounds", "experiments", "generated_property_import"}:
            raise ValueError("file_kind must be compounds, experiments, or generated_property_import")

        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                files = dict(row.get("files") or {})
                current = files.get(kind)
                current_meta = dict(current) if isinstance(current, dict) else {}
                file_path = str(current_meta.get("path") or "").strip()

                files[kind] = None
                if kind == "experiments":
                    files["generated_property_import"] = None
                row["files"] = files
                row["status"] = "draft"
                row["last_check"] = None
                row["review"] = None
                row["approval"] = None
                row["rejection"] = None
                row = self._append_status_history(
                    row,
                    status="draft",
                    event=f"clear_{kind}",
                    note="",
                )
                row["updated_at"] = _utc_now_iso()
                state["batches"][index] = row
                self._write_state(state)

                if file_path:
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                    except Exception:
                        pass
                return row
        raise ValueError(f"Batch '{token}' not found")

    def mark_last_check(self, batch_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        if not token:
            raise ValueError("batch_id is required")
        stamped = dict(payload or {})
        stamped["checked_at"] = _utc_now_iso()
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                row["last_check"] = stamped
                row["status"] = "checked"
                row["review"] = None
                row["approval"] = None
                row["rejection"] = None
                row = self._append_status_history(row, status="checked", event="checked")
                row["updated_at"] = _utc_now_iso()
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def append_apply_history(self, batch_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        if not token:
            raise ValueError("batch_id is required")
        entry = dict(payload or {})
        entry.setdefault("applied_at", _utc_now_iso())
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                history = list(row.get("apply_history") or [])
                history.append(entry)
                row["apply_history"] = history
                row["status"] = "applied"
                row = self._append_status_history(row, status="applied", event="applied", note=str(entry.get("apply_id") or ""))
                row["updated_at"] = _utc_now_iso()
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def append_rollback_history(self, batch_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        if not token:
            raise ValueError("batch_id is required")
        entry = dict(payload or {})
        entry.setdefault("rolled_back_at", _utc_now_iso())
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                history = list(row.get("rollback_history") or [])
                history.append(entry)
                row["rollback_history"] = history
                row["status"] = "rolled_back"
                row = self._append_status_history(
                    row,
                    status="rolled_back",
                    event="rolled_back",
                    note=str(entry.get("apply_id") or ""),
                )
                row["updated_at"] = _utc_now_iso()
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def transition_batch_status(self, batch_id: str, *, action: str, note: str = "") -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        action_token = str(action or "").strip().lower()
        note_token = str(note or "").strip()
        if not token:
            raise ValueError("batch_id is required")
        if action_token not in {"review", "approve", "reject", "reopen"}:
            raise ValueError("action must be one of review, approve, reject, reopen")

        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue

                row = dict(item)
                status = str(row.get("status") or "draft").strip().lower() or "draft"
                last_check = row.get("last_check")
                has_check = isinstance(last_check, dict) and bool(str(last_check.get("checked_at") or "").strip())
                now = _utc_now_iso()

                if action_token == "review":
                    if not has_check:
                        raise ValueError("Cannot mark reviewed before a successful check.")
                    if status not in {"checked", "reviewed", "approved"}:
                        raise ValueError(f"Cannot review batch from status '{status}'.")
                    row["review"] = {
                        "reviewed_at": now,
                        "note": note_token,
                    }
                    if status != "approved":
                        row["status"] = "reviewed"
                    row = self._append_status_history(
                        row,
                        status=str(row.get("status") or "reviewed"),
                        event="reviewed",
                        note=note_token,
                    )
                elif action_token == "approve":
                    if not has_check:
                        raise ValueError("Cannot approve before a successful check.")
                    if status not in {"checked", "reviewed", "approved"}:
                        raise ValueError(f"Cannot approve batch from status '{status}'.")
                    row["approval"] = {
                        "approved_at": now,
                        "note": note_token,
                    }
                    row["status"] = "approved"
                    row = self._append_status_history(row, status="approved", event="approved", note=note_token)
                elif action_token == "reject":
                    if not has_check:
                        raise ValueError("Cannot reject before a successful check.")
                    if status not in {"checked", "reviewed", "approved", "rejected"}:
                        raise ValueError(f"Cannot reject batch from status '{status}'.")
                    row["rejection"] = {
                        "rejected_at": now,
                        "note": note_token,
                    }
                    row["status"] = "rejected"
                    row = self._append_status_history(row, status="rejected", event="rejected", note=note_token)
                elif action_token == "reopen":
                    row["status"] = "draft"
                    row["last_check"] = None
                    row["review"] = None
                    row["approval"] = None
                    row["rejection"] = None
                    row = self._append_status_history(row, status="draft", event="reopened", note=note_token)

                row["updated_at"] = now
                state["batches"][index] = row
                self._write_state(state)
                return row

        raise ValueError(f"Batch '{token}' not found")

    def list_methods(self) -> List[Dict[str, Any]]:
        with self._lock:
            state = self._read_state()
            rows = [dict(item) for item in state.get("methods", []) if isinstance(item, dict)]
        rows.sort(key=lambda item: str(item.get("name") or item.get("key") or "").lower())
        return rows

    def upsert_method(self, payload: Dict[str, Any], *, method_id: str = "") -> Dict[str, Any]:
        raw = payload if isinstance(payload, dict) else {}
        token = str(method_id or raw.get("id") or "").strip()
        key = str(raw.get("key") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not key:
            raise ValueError("method key is required")
        if not name:
            raise ValueError("method name is required")
        explicit_output_property = _read_text_field(raw, "output_property", "outputProperty")
        explicit_reference = _read_text_field(raw, "reference")
        normalized_output = _normalize_property_token(explicit_output_property)
        normalized_key = _normalize_property_token(key)
        normalized_name = _normalize_property_token(name)
        now = _utc_now_iso()
        with self._lock:
            state = self._read_state()
            methods = self._normalize_method_collection(state.get("methods"))
            next_id = _normalize_method_id(token)
            existing_index = -1
            if token:
                for index, item in enumerate(methods):
                    if str((item or {}).get("id") or "").strip() == next_id:
                        existing_index = index
                        break
            else:
                def _same_reference(row: Dict[str, Any]) -> bool:
                    if not explicit_reference:
                        return True
                    row_ref = str((row or {}).get("reference") or "").strip()
                    return (not row_ref) or row_ref == explicit_reference

                if normalized_output:
                    for index, item in enumerate(methods):
                        row = dict(item or {})
                        if not _same_reference(row):
                            continue
                        row_output = _normalize_property_token(row.get("output_property"))
                        if row_output and row_output == normalized_output:
                            existing_index = index
                            next_id = str(row.get("id") or "").strip() or next_id
                            break
                if existing_index < 0 and normalized_key and normalized_name:
                    for index, item in enumerate(methods):
                        row = dict(item or {})
                        if not _same_reference(row):
                            continue
                        row_key = _normalize_property_token(row.get("key"))
                        row_name = _normalize_property_token(row.get("name"))
                        if row_key == normalized_key and row_name == normalized_name:
                            existing_index = index
                            next_id = str(row.get("id") or "").strip() or next_id
                            break
            if existing_index >= 0:
                row = dict(methods[existing_index])
                created_at = str(row.get("created_at") or now)
            else:
                row = {}
                created_at = now
            output_property = _read_text_field(
                raw,
                "output_property",
                "outputProperty",
                fallback=str(row.get("output_property") or "").strip(),
            )
            if not output_property:
                raise ValueError("method output_property is required")

            category = _read_text_field(raw, "category", fallback=str(row.get("category") or "").strip())
            description = _read_text_field(raw, "description", fallback=str(row.get("description") or "").strip())
            reference = _read_text_field(raw, "reference", fallback=str(row.get("reference") or "").strip())
            input_unit = _read_text_field(raw, "input_unit", "inputUnit", fallback=str(row.get("input_unit") or "").strip())
            output_unit = _read_text_field(raw, "output_unit", "outputUnit", fallback=str(row.get("output_unit") or "").strip())
            display_unit = _read_text_field(raw, "display_unit", "displayUnit", fallback=str(row.get("display_unit") or "").strip())
            import_transform = _read_text_field(
                raw,
                "import_transform",
                "importTransform",
                fallback=str(row.get("import_transform") or "none"),
            ) or "none"
            display_transform = _read_text_field(
                raw,
                "display_transform",
                "displayTransform",
                fallback=str(row.get("display_transform") or "none"),
            ) or "none"

            row.update(
                {
                    "id": next_id,
                    "key": key,
                    "name": name,
                    "output_property": output_property,
                    "category": category,
                    "description": description,
                    "reference": reference,
                    "input_unit": input_unit,
                    "output_unit": output_unit,
                    "display_unit": display_unit,
                    "import_transform": import_transform,
                    "display_transform": display_transform,
                    "created_at": created_at,
                    "updated_at": now,
                }
            )
            row = self._normalize_method_row(row, default_now=now)
            if existing_index >= 0:
                methods[existing_index] = row
            else:
                methods.append(row)
            state["methods"] = self._normalize_method_collection(methods)
            self._write_state(state)
            return row

    def delete_method(self, method_id: str) -> None:
        token = str(method_id or "").strip()
        if not token:
            raise ValueError("method_id is required")
        with self._lock:
            state = self._read_state()
            methods = self._normalize_method_collection(state.get("methods"))
            filtered = [item for item in methods if str((item or {}).get("id") or "").strip() != token]
            if len(filtered) == len(methods):
                raise ValueError(f"Method '{token}' not found")
            state["methods"] = filtered
            mappings = []
            for item in state.get("property_mappings", []):
                row = dict(item or {})
                if str(row.get("method_id") or "").strip() == token:
                    row["method_id"] = ""
                mappings.append(row)
            state["property_mappings"] = mappings
            self._write_state(state)

    def list_property_mappings(self, *, database_id: str = "") -> List[Dict[str, Any]]:
        token = str(database_id or "").strip()
        with self._lock:
            state = self._read_state()
            rows = [dict(item) for item in state.get("property_mappings", []) if isinstance(item, dict)]
        if token:
            rows = [row for row in rows if str(row.get("database_id") or "").strip() == token]
        rows.sort(key=lambda item: (str(item.get("database_id") or ""), str(item.get("source_property") or "").lower()))
        return rows

    def list_pending_database_sync(self, *, database_id: str = "", pending_only: bool = True) -> List[Dict[str, Any]]:
        db_token = str(database_id or "").strip()
        with self._lock:
            state = self._read_state()
            rows = [dict(item) for item in state.get("pending_database_sync", []) if isinstance(item, dict)]
        filtered: List[Dict[str, Any]] = []
        for item in rows:
            row = dict(item)
            row_db = str(row.get("database_id") or "").strip()
            if db_token and row_db != db_token:
                continue
            status = str(row.get("status") or "pending").strip().lower() or "pending"
            if pending_only and status != "pending":
                continue
            filtered.append(row)
        filtered.sort(key=lambda item: str(item.get("created_at") or ""))
        return filtered

    def enqueue_database_sync(
        self,
        *,
        database_id: str,
        operation: str,
        payload: Dict[str, Any],
        dedupe_key: str = "",
    ) -> Dict[str, Any]:
        db_token = str(database_id or "").strip()
        op_token = str(operation or "").strip().lower()
        if not db_token:
            raise ValueError("database_id is required")
        if op_token not in {"ensure_property", "rename_property", "purge_property"}:
            raise ValueError("operation must be ensure_property, rename_property, or purge_property")
        now = _utc_now_iso()
        dedupe_token = str(dedupe_key or "").strip()
        normalized_payload = dict(payload or {})
        with self._lock:
            state = self._read_state()
            queue = [dict(item) for item in state.get("pending_database_sync", []) if isinstance(item, dict)]
            if dedupe_token:
                for index, item in enumerate(queue):
                    if str(item.get("database_id") or "").strip() != db_token:
                        continue
                    if str(item.get("operation") or "").strip().lower() != op_token:
                        continue
                    if str(item.get("dedupe_key") or "").strip() != dedupe_token:
                        continue
                    status = str(item.get("status") or "pending").strip().lower() or "pending"
                    if status != "pending":
                        continue
                    next_row = dict(item)
                    next_row["payload"] = normalized_payload
                    next_row["updated_at"] = now
                    next_row["error"] = ""
                    queue[index] = next_row
                    state["pending_database_sync"] = queue
                    self._write_state(state)
                    return next_row

            entry = {
                "id": f"sync_{uuid.uuid4().hex[:12]}",
                "database_id": db_token,
                "operation": op_token,
                "payload": normalized_payload,
                "dedupe_key": dedupe_token,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "applied_at": "",
                "result": {},
                "error": "",
            }
            queue.append(entry)
            state["pending_database_sync"] = queue
            self._write_state(state)
            return entry

    def mark_database_sync_applied(self, entry_id: str, *, result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        token = str(entry_id or "").strip()
        if not token:
            raise ValueError("entry_id is required")
        now = _utc_now_iso()
        with self._lock:
            state = self._read_state()
            queue = [dict(item) for item in state.get("pending_database_sync", []) if isinstance(item, dict)]
            for index, item in enumerate(queue):
                if str(item.get("id") or "").strip() != token:
                    continue
                row = dict(item)
                row["status"] = "applied"
                row["updated_at"] = now
                row["applied_at"] = now
                row["result"] = dict(result or {})
                row["error"] = ""
                queue[index] = row
                state["pending_database_sync"] = queue
                self._write_state(state)
                return row
        raise ValueError(f"Pending database sync entry '{token}' not found")

    def mark_database_sync_failed(self, entry_id: str, *, error: str) -> Dict[str, Any]:
        token = str(entry_id or "").strip()
        if not token:
            raise ValueError("entry_id is required")
        now = _utc_now_iso()
        err = str(error or "").strip()
        with self._lock:
            state = self._read_state()
            queue = [dict(item) for item in state.get("pending_database_sync", []) if isinstance(item, dict)]
            for index, item in enumerate(queue):
                if str(item.get("id") or "").strip() != token:
                    continue
                row = dict(item)
                row["status"] = "failed"
                row["updated_at"] = now
                row["error"] = err
                queue[index] = row
                state["pending_database_sync"] = queue
                self._write_state(state)
                return row
        raise ValueError(f"Pending database sync entry '{token}' not found")

    def list_method_usage_rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            state = self._read_state()
            methods = self._normalize_method_collection(state.get("methods"))
            mappings = [dict(item) for item in state.get("property_mappings", []) if isinstance(item, dict)]

        method_ids = {str(item.get("id") or "").strip() for item in methods if str(item.get("id") or "").strip()}
        aggregate: Dict[tuple[str, str], Dict[str, Any]] = {}

        for row in mappings:
            method_id = str(row.get("method_id") or "").strip()
            database_id = str(row.get("database_id") or "").strip()
            source_property = str(row.get("source_property") or "").strip()
            if not method_id or not database_id:
                continue
            key = (method_id, database_id)
            bucket = aggregate.get(key)
            if bucket is None:
                bucket = {
                    "method_id": method_id,
                    "database_id": database_id,
                    "mapping_count": 0,
                    "source_properties": set(),
                }
                aggregate[key] = bucket
            bucket["mapping_count"] = int(bucket.get("mapping_count") or 0) + 1
            if source_property:
                props = bucket.get("source_properties")
                if isinstance(props, set):
                    props.add(source_property)

        rows: List[Dict[str, Any]] = []
        for item in aggregate.values():
            props_set = item.get("source_properties")
            props = sorted(props_set) if isinstance(props_set, set) else []
            rows.append(
                {
                    "method_id": str(item.get("method_id") or "").strip(),
                    "database_id": str(item.get("database_id") or "").strip(),
                    "mapping_count": int(item.get("mapping_count") or 0),
                    "source_property_count": len(props),
                    "source_properties": props,
                }
            )

        # Ensure methods with no usage are still visible to the caller.
        known_pairs = {(str(item.get("method_id") or "").strip(), str(item.get("database_id") or "").strip()) for item in rows}
        for method_id in sorted(method_ids):
            if any(pair[0] == method_id for pair in known_pairs):
                continue
            rows.append(
                {
                    "method_id": method_id,
                    "database_id": "",
                    "mapping_count": 0,
                    "source_property_count": 0,
                    "source_properties": [],
                }
            )

        rows.sort(
            key=lambda item: (
                str(item.get("method_id") or ""),
                str(item.get("database_id") or ""),
            )
        )
        return rows

    def replace_property_mappings(self, database_id: str, mappings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        db_token = str(database_id or "").strip()
        if not db_token:
            raise ValueError("database_id is required")

        normalized_rows: List[Dict[str, Any]] = []
        seen_sources: set[str] = set()
        now = _utc_now_iso()
        for row in mappings if isinstance(mappings, list) else []:
            if not isinstance(row, dict):
                continue
            source_property = str(row.get("source_property") or "").strip()
            mmp_property = str(row.get("mmp_property") or "").strip()
            if not source_property or not mmp_property:
                continue
            key = source_property.lower()
            if key in seen_sources:
                continue
            seen_sources.add(key)
            normalized_rows.append(
                {
                    "id": str(row.get("id") or f"map_{uuid.uuid4().hex[:10]}"),
                    "database_id": db_token,
                    "source_property": source_property,
                    "mmp_property": mmp_property,
                    "method_id": str(row.get("method_id") or "").strip(),
                    "value_transform": str(row.get("value_transform") or row.get("valueTransform") or "none").strip() or "none",
                    "notes": str(row.get("notes") or "").strip(),
                    "updated_at": now,
                }
            )

        with self._lock:
            state = self._read_state()
            existing = [
                dict(item)
                for item in state.get("property_mappings", [])
                if str((item or {}).get("database_id") or "").strip() != db_token
            ]
            state["property_mappings"] = existing + normalized_rows
            self._write_state(state)

        return normalized_rows

    def sync_database_methods_and_mappings(
        self,
        *,
        database_id: str,
        property_names: List[str],
    ) -> Dict[str, Any]:
        db_token = str(database_id or "").strip()
        if not db_token:
            raise ValueError("database_id is required")

        normalized_properties: List[str] = []
        seen_props: set[str] = set()
        for item in property_names if isinstance(property_names, list) else []:
            prop = str(item or "").strip()
            if not prop:
                continue
            key = prop.lower()
            if key in seen_props:
                continue
            seen_props.add(key)
            normalized_properties.append(prop)

        preferred_property_by_family: Dict[str, str] = {}
        for prop in normalized_properties:
            family = _canonical_property_family(prop)
            if not family:
                continue
            current = preferred_property_by_family.get(family)
            if not current:
                preferred_property_by_family[family] = prop
                continue
            if _property_preference_rank(prop, family) < _property_preference_rank(current, family):
                preferred_property_by_family[family] = prop

        selected_property_keys = {value.lower() for value in preferred_property_by_family.values() if value}
        selected_properties = [prop for prop in normalized_properties if prop.lower() in selected_property_keys]

        now = _utc_now_iso()
        created_methods = 0
        created_mappings = 0
        updated_mappings = 0
        removed_mappings = 0
        removed_methods = 0
        changed = False

        with self._lock:
            state = self._read_state()
            methods = [dict(item) for item in state.get("methods", []) if isinstance(item, dict)]
            mappings = [dict(item) for item in state.get("property_mappings", []) if isinstance(item, dict)]

            filtered_mappings: List[Dict[str, Any]] = []
            for item in mappings:
                row = dict(item)
                if str(row.get("database_id") or "").strip() != db_token:
                    filtered_mappings.append(row)
                    continue
                source = str(row.get("source_property") or "").strip()
                if not source:
                    filtered_mappings.append(row)
                    continue
                family = _canonical_property_family(source)
                preferred = str(preferred_property_by_family.get(family) or "").strip()
                mapping_id = str(row.get("id") or "").strip()
                mapping_notes = str(row.get("notes") or "").strip().lower()
                auto_generated = mapping_id.startswith("map_auto_") or mapping_notes in {
                    "method-bound mapping.",
                    "method bound mapping.",
                    "optional",
                }
                if preferred and source.lower() != preferred.lower() and auto_generated:
                    removed_mappings += 1
                    changed = True
                    continue
                filtered_mappings.append(row)
            mappings = filtered_mappings

            method_id_by_output: Dict[str, str] = {}
            for item in methods:
                output = str(item.get("output_property") or "").strip()
                method_id = str(item.get("id") or "").strip()
                if output and method_id and output.lower() not in method_id_by_output:
                    method_id_by_output[output.lower()] = method_id
            method_display_transform_by_id: Dict[str, str] = {}
            for item in methods:
                method_id = str(item.get("id") or "").strip()
                if not method_id:
                    continue
                display_transform = str(item.get("display_transform") or "none").strip() or "none"
                method_display_transform_by_id[method_id] = display_transform
            auto_method_id_by_family: Dict[str, str] = {}
            for item in methods:
                method_id = str(item.get("id") or "").strip()
                if not method_id.startswith("method_auto_"):
                    continue
                reference = str(item.get("reference") or "").strip()
                if reference and reference != db_token:
                    continue
                output = str(item.get("output_property") or "").strip()
                family = _canonical_property_family(output)
                if family and family not in auto_method_id_by_family:
                    auto_method_id_by_family[family] = method_id

            db_mapping_indexes_by_source: Dict[str, int] = {}
            for index, item in enumerate(mappings):
                if str(item.get("database_id") or "").strip() != db_token:
                    continue
                source = str(item.get("source_property") or "").strip()
                if source and source.lower() not in db_mapping_indexes_by_source:
                    db_mapping_indexes_by_source[source.lower()] = index

            for prop in selected_properties:
                prop_key = prop.lower()
                method_id = method_id_by_output.get(prop_key, "")
                family = _canonical_property_family(prop)
                if not method_id and family:
                    family_method_id = auto_method_id_by_family.get(family, "")
                    if family_method_id:
                        for method_index, method_item in enumerate(methods):
                            row = dict(method_item)
                            row_id = str(row.get("id") or "").strip()
                            if row_id != family_method_id:
                                continue
                            previous_output = str(row.get("output_property") or "").strip()
                            previous_key = str(row.get("key") or "").strip()
                            previous_name = str(row.get("name") or "").strip()
                            row_changed = False
                            if previous_output.lower() != prop_key:
                                row["output_property"] = prop
                                row_changed = True
                            if (not previous_key) or previous_key.lower() == previous_output.lower():
                                if previous_key != prop:
                                    row["key"] = prop
                                    row_changed = True
                            if (not previous_name) or previous_name.lower() == previous_output.lower():
                                if previous_name != prop:
                                    row["name"] = prop
                                    row_changed = True
                            if row_changed:
                                row["updated_at"] = now
                                methods[method_index] = row
                                changed = True
                            if previous_output:
                                previous_key_lower = previous_output.lower()
                                if method_id_by_output.get(previous_key_lower) == family_method_id:
                                    method_id_by_output.pop(previous_key_lower, None)
                            method_id_by_output[prop_key] = family_method_id
                            method_id = family_method_id
                            break
                if not method_id:
                    method_id = _auto_method_id(prop)
                    methods.append(
                        {
                            "id": method_id,
                            "key": prop,
                            "name": prop,
                            "output_property": prop,
                            "category": "Auto",
                            "description": "Auto-synced from MMP database property catalog.",
                            "reference": db_token,
                            "input_unit": "",
                            "output_unit": "",
                            "display_unit": "",
                            "import_transform": "none",
                            "display_transform": "none",
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    method_id_by_output[prop_key] = method_id
                    created_methods += 1
                    changed = True

                existing_idx = db_mapping_indexes_by_source.get(prop_key)
                if existing_idx is None:
                    mapping_transform = str(method_display_transform_by_id.get(method_id) or "none").strip().lower() or "none"
                    mappings.append(
                        {
                            "id": _auto_mapping_id(db_token, prop),
                            "database_id": db_token,
                            "source_property": prop,
                            "mmp_property": prop,
                            "method_id": method_id,
                            "value_transform": mapping_transform,
                            "notes": "Method-bound mapping.",
                            "updated_at": now,
                        }
                    )
                    db_mapping_indexes_by_source[prop_key] = len(mappings) - 1
                    created_mappings += 1
                    changed = True
                    continue

                current = dict(mappings[existing_idx])
                current_prop = str(current.get("mmp_property") or "").strip()
                current_method = str(current.get("method_id") or "").strip()
                current_value_transform = str(current.get("value_transform") or "").strip().lower()
                row_changed = False
                if not current_prop:
                    current["mmp_property"] = prop
                    current_prop = prop
                    row_changed = True
                if not current_method and current_prop.lower() == prop_key:
                    current["method_id"] = method_id
                    row_changed = True
                if not current_value_transform:
                    current["value_transform"] = "none"
                    row_changed = True
                if row_changed:
                    current["updated_at"] = now
                    mappings[existing_idx] = current
                    updated_mappings += 1
                    changed = True

            referenced_method_ids = {
                str(item.get("method_id") or "").strip()
                for item in mappings
                if str(item.get("method_id") or "").strip()
            }
            filtered_methods: List[Dict[str, Any]] = []
            for item in methods:
                row = dict(item)
                method_id = str(row.get("id") or "").strip()
                output_prop = str(row.get("output_property") or "").strip()
                family = _canonical_property_family(output_prop)
                preferred = str(preferred_property_by_family.get(family) or "").strip()
                if (
                    method_id.startswith("method_auto_")
                    and method_id not in referenced_method_ids
                    and preferred
                    and output_prop
                    and output_prop.lower() != preferred.lower()
                ):
                    removed_methods += 1
                    changed = True
                    continue
                filtered_methods.append(row)
            methods = filtered_methods

            normalized_methods = self._normalize_method_collection(methods)
            if len(normalized_methods) != len(methods):
                changed = True
            methods = normalized_methods

            if changed:
                state["methods"] = methods
                state["property_mappings"] = mappings
                self._write_state(state)

        return {
            "database_id": db_token,
            "properties_seen": len(normalized_properties),
            "properties_selected": len(selected_properties),
            "created_methods": created_methods,
            "created_mappings": created_mappings,
            "updated_mappings": updated_mappings,
            "removed_mappings": removed_mappings,
            "removed_methods": removed_methods,
            "changed": changed,
        }


__all__ = ["MmpLifecycleAdminStore"]
