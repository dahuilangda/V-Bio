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


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    token = str(value or "").strip().lower()
    if not token:
        return bool(default)
    return token in {"1", "true", "yes", "on", "y", "t"}


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


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    raw = str(os.getenv(name, "")).strip()
    try:
        value = int(raw) if raw else int(default)
    except Exception:
        value = int(default)
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


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
        self._status_history_limit = _env_int(
            "LEAD_OPT_MMP_LIFECYCLE_STATUS_HISTORY_LIMIT",
            120,
            minimum=10,
            maximum=20_000,
        )
        self._apply_history_limit = _env_int(
            "LEAD_OPT_MMP_LIFECYCLE_APPLY_HISTORY_LIMIT",
            80,
            minimum=10,
            maximum=20_000,
        )
        self._rollback_history_limit = _env_int(
            "LEAD_OPT_MMP_LIFECYCLE_ROLLBACK_HISTORY_LIMIT",
            80,
            minimum=10,
            maximum=20_000,
        )
        self._pending_sync_keep_applied = _env_int(
            "LEAD_OPT_MMP_LIFECYCLE_PENDING_SYNC_KEEP_APPLIED",
            200,
            minimum=0,
            maximum=100_000,
        )
        self._pending_sync_keep_failed = _env_int(
            "LEAD_OPT_MMP_LIFECYCLE_PENDING_SYNC_KEEP_FAILED",
            100,
            minimum=0,
            maximum=100_000,
        )
        self._pending_sync_keep_other = _env_int(
            "LEAD_OPT_MMP_LIFECYCLE_PENDING_SYNC_KEEP_OTHER",
            100,
            minimum=0,
            maximum=100_000,
        )
        self._error_text_max_len = _env_int(
            "LEAD_OPT_MMP_LIFECYCLE_ERROR_TEXT_MAX_LEN",
            2000,
            minimum=256,
            maximum=200_000,
        )
        self._sync_result_max_chars = _env_int(
            "LEAD_OPT_MMP_LIFECYCLE_SYNC_RESULT_MAX_CHARS",
            8000,
            minimum=512,
            maximum=500_000,
        )
        self._db_lock_history_limit = _env_int(
            "LEAD_OPT_MMP_LIFECYCLE_DB_LOCK_HISTORY_LIMIT",
            200,
            minimum=20,
            maximum=100_000,
        )
        self._db_lock_max_age_seconds = _env_int(
            "LEAD_OPT_MMP_LIFECYCLE_DB_LOCK_MAX_AGE_SECONDS",
            12 * 3600,
            minimum=60,
            maximum=7 * 24 * 3600,
        )
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
            "database_operation_locks": [],
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
            if not isinstance(base.get("database_operation_locks"), list):
                base["database_operation_locks"] = []
            if not isinstance(base.get("batches"), list):
                base["batches"] = []
            normalized_methods, method_aliases = self._normalize_method_collection_with_aliases(base.get("methods"))
            base["methods"] = normalized_methods
            self._apply_method_aliases_to_state(base, method_aliases)
            self._sanitize_state_inplace(base)
            return base
        except Exception:
            return self._empty_state()

    def _write_state(self, state: Dict[str, Any]) -> None:
        self._sanitize_state_inplace(state)
        state["updated_at"] = _utc_now_iso()
        _atomic_json_write(self.state_file, state)

    @staticmethod
    def _tail_list(value: Any, *, limit: int) -> List[Any]:
        if not isinstance(value, list):
            return []
        rows = list(value)
        if limit <= 0:
            return []
        if len(rows) <= limit:
            return rows
        return rows[-limit:]

    def _truncate_text(self, value: Any, *, limit: int = 0) -> str:
        text = str(value or "")
        max_len = int(limit or self._error_text_max_len)
        if len(text) <= max_len:
            return text
        return text[:max_len]

    def _normalize_sync_result_payload(self, result: Any) -> Dict[str, Any]:
        payload = dict(result or {}) if isinstance(result, dict) else {}
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception:
            return {}
        if len(encoded) <= self._sync_result_max_chars:
            return payload
        return {
            "truncated": True,
            "size": len(encoded),
            "preview": encoded[: self._sync_result_max_chars],
        }

    def _sanitize_pending_database_sync_rows(self, rows: Any) -> List[Dict[str, Any]]:
        pending: List[Dict[str, Any]] = []
        applied: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        other: List[Dict[str, Any]] = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["error"] = self._truncate_text(row.get("error"))
            row["result"] = self._normalize_sync_result_payload(row.get("result"))
            status = str(row.get("status") or "pending").strip().lower() or "pending"
            row["status"] = status
            if status == "pending":
                pending.append(row)
            elif status == "applied":
                applied.append(row)
            elif status == "failed":
                failed.append(row)
            else:
                other.append(row)

        def _sort_desc(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            entries.sort(
                key=lambda item: (
                    str(item.get("updated_at") or item.get("created_at") or ""),
                    str(item.get("id") or ""),
                ),
                reverse=True,
            )
            return entries

        applied = _sort_desc(applied)[: self._pending_sync_keep_applied]
        failed = _sort_desc(failed)[: self._pending_sync_keep_failed]
        other = _sort_desc(other)[: self._pending_sync_keep_other]

        merged = pending + applied + failed + other
        merged.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("id") or ""),
            )
        )
        return merged

    @staticmethod
    def _parse_utc_iso(value: Any) -> Optional[datetime]:
        token = str(value or "").strip()
        if not token:
            return None
        try:
            return datetime.strptime(token, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _sanitize_database_operation_locks(self, rows: Any) -> List[Dict[str, Any]]:
        now_dt = datetime.now(timezone.utc)
        active_rows: List[Dict[str, Any]] = []
        historical_rows: List[Dict[str, Any]] = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row_id = str(row.get("id") or "").strip()
            db_id = str(row.get("database_id") or "").strip()
            operation = str(row.get("operation") or "").strip().lower()
            if not row_id or not db_id or not operation:
                continue
            status = str(row.get("status") or "active").strip().lower() or "active"
            created_at = str(row.get("created_at") or "")
            updated_at = str(row.get("updated_at") or created_at)
            released_at = str(row.get("released_at") or "")
            entry = {
                "id": row_id,
                "database_id": db_id,
                "operation": operation,
                "batch_id": str(row.get("batch_id") or "").strip(),
                "task_id": str(row.get("task_id") or "").strip(),
                "note": self._truncate_text(row.get("note"), limit=512),
                "status": status,
                "created_at": created_at,
                "updated_at": updated_at,
                "released_at": released_at,
                "error": self._truncate_text(row.get("error")),
            }
            is_active = status == "active" and not released_at
            if is_active:
                created_dt = self._parse_utc_iso(created_at)
                if created_dt is not None and (now_dt - created_dt).total_seconds() > float(self._db_lock_max_age_seconds):
                    entry["status"] = "expired"
                    entry["released_at"] = _utc_now_iso()
                    entry["updated_at"] = entry["released_at"]
                    is_active = False
            if is_active:
                active_rows.append(entry)
            else:
                historical_rows.append(entry)
        active_rows.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
        historical_rows.sort(
            key=lambda item: (str(item.get("updated_at") or item.get("created_at") or ""), str(item.get("id") or "")),
            reverse=True,
        )
        historical_rows = historical_rows[: self._db_lock_history_limit]
        merged = active_rows + list(reversed(historical_rows))
        return merged

    def _sanitize_state_inplace(self, state: Dict[str, Any]) -> None:
        state["pending_database_sync"] = self._sanitize_pending_database_sync_rows(state.get("pending_database_sync"))
        state["database_operation_locks"] = self._sanitize_database_operation_locks(state.get("database_operation_locks"))
        next_batches: List[Dict[str, Any]] = []
        for item in state.get("batches", []) if isinstance(state.get("batches"), list) else []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["status_history"] = self._tail_list(row.get("status_history"), limit=self._status_history_limit)
            row["apply_history"] = self._tail_list(row.get("apply_history"), limit=self._apply_history_limit)
            row["rollback_history"] = self._tail_list(row.get("rollback_history"), limit=self._rollback_history_limit)
            row["last_error"] = self._truncate_text(row.get("last_error"))
            runtime = dict(row.get("apply_runtime") or {}) if isinstance(row.get("apply_runtime"), dict) else None
            if runtime is not None:
                runtime["message"] = self._truncate_text(runtime.get("message"))
                runtime["error"] = self._truncate_text(runtime.get("error"))
            row["apply_runtime"] = runtime
            next_batches.append(row)
        state["batches"] = next_batches

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
        entry = {
            "at": _utc_now_iso(),
            "status": str(status or "").strip().lower(),
            "event": str(event or "").strip().lower(),
            "note": str(note or "").strip(),
        }
        if history and isinstance(history[-1], dict):
            last = history[-1]
            if (
                str(last.get("status") or "").strip().lower() == entry["status"]
                and str(last.get("event") or "").strip().lower() == entry["event"]
                and str(last.get("note") or "").strip() == entry["note"]
            ):
                last["at"] = entry["at"]
                history[-1] = last
                row["status_history"] = history
                return row
        history.append(entry)
        row["status_history"] = history
        return row

    def _batch_summary_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(row or {})
        status_history = list(payload.get("status_history") or []) if isinstance(payload.get("status_history"), list) else []
        apply_history = list(payload.get("apply_history") or []) if isinstance(payload.get("apply_history"), list) else []
        rollback_history = (
            list(payload.get("rollback_history") or []) if isinstance(payload.get("rollback_history"), list) else []
        )
        payload["status_history_count"] = len(status_history)
        payload["apply_history_count"] = len(apply_history)
        payload["rollback_history_count"] = len(rollback_history)
        payload["status_history"] = []
        payload["apply_history"] = self._tail_list(apply_history, limit=1)
        payload["rollback_history"] = self._tail_list(rollback_history, limit=1)
        return payload

    def list_batches(self, *, summary: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            state = self._read_state()
            if summary:
                rows = [self._batch_summary_row(item) for item in state.get("batches", []) if isinstance(item, dict)]
            else:
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

    def _resolve_saved_batch_file_path(self, batch_id: str, file_meta: Dict[str, Any]) -> Optional[Path]:
        meta = dict(file_meta or {}) if isinstance(file_meta, dict) else {}
        direct_path = str(meta.get("path") or "").strip()
        if direct_path:
            path_obj = Path(direct_path)
            if path_obj.exists():
                return path_obj
        rel_path = str(meta.get("path_rel") or "").strip()
        if rel_path:
            path_obj = self.root_dir / rel_path
            if path_obj.exists():
                return path_obj
        stored_name = str(meta.get("stored_name") or "").strip()
        if batch_id and stored_name:
            path_obj = self.batch_upload_dir / batch_id / stored_name
            if path_obj.exists():
                return path_obj
        return None

    def _clone_uploaded_files_from_batch(
        self,
        *,
        source_batch: Dict[str, Any],
        source_batch_id: str,
        target_batch_id: str,
    ) -> Dict[str, Any]:
        files = dict(source_batch.get("files") or {}) if isinstance(source_batch.get("files"), dict) else {}
        target_dir = self.batch_upload_dir / target_batch_id
        target_dir.mkdir(parents=True, exist_ok=True)
        cloned_files: Dict[str, Any] = {
            "compounds": None,
            "experiments": None,
            "generated_property_import": None,
        }
        now = _utc_now_iso()
        for kind in ("compounds", "experiments"):
            source_meta = dict(files.get(kind) or {}) if isinstance(files.get(kind), dict) else {}
            if not source_meta:
                continue
            source_path = self._resolve_saved_batch_file_path(source_batch_id, source_meta)
            if source_path is None:
                raise ValueError(
                    f"Cannot clone batch '{source_batch_id}': missing saved {kind} file. Please re-upload and retry."
                )
            suffix = source_path.suffix or Path(str(source_meta.get("stored_name") or f"{kind}.tsv")).suffix or ".tsv"
            stored_name = f"{kind}{suffix}"
            target_path = target_dir / stored_name
            shutil.copy2(source_path, target_path)
            try:
                path_rel = str(target_path.relative_to(self.root_dir))
            except Exception:
                path_rel = ""
            cloned_files[kind] = {
                "batch_id": target_batch_id,
                "stored_name": stored_name,
                "original_name": str(source_meta.get("original_name") or source_meta.get("stored_name") or stored_name),
                "size": int(target_path.stat().st_size),
                "uploaded_at": now,
                "path": str(target_path),
                "path_rel": path_rel,
                "column_config": dict(source_meta.get("column_config") or {}),
            }
        return cloned_files

    def create_batch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payload if isinstance(payload, dict) else {}
        patch = self._normalize_batch_payload(raw, for_update=False)
        name = str(patch.get("name") or "").strip()
        if not name:
            raise ValueError("batch name is required")
        batch_id = _normalize_batch_id(str(raw.get("id") or "").strip())
        clone_from_batch_id = str(raw.get("clone_from_batch_id") or "").strip()
        clone_uploaded_files = _to_bool(raw.get("clone_uploaded_files"), default=True)
        now = _utc_now_iso()
        entry = {
            "id": batch_id,
            "name": name,
            "description": str(patch.get("description") or ""),
            "notes": str(patch.get("notes") or ""),
            "status": "draft",
            "selected_database_id": str(patch.get("selected_database_id") or ""),
            "source_batch_id": clone_from_batch_id,
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
            "apply_runtime": None,
            "last_error": "",
            "apply_history": [],
            "rollback_history": [],
            "status_history": [
                {
                    "at": now,
                    "status": "draft",
                    "event": "created",
                    "note": (f"forked from {clone_from_batch_id}" if clone_from_batch_id else ""),
                }
            ],
        }
        with self._lock:
            state = self._read_state()
            for item in state.get("batches", []):
                if str((item or {}).get("id") or "").strip() == batch_id:
                    raise ValueError(f"Batch '{batch_id}' already exists")
            if clone_from_batch_id and clone_uploaded_files:
                source_batch = next(
                    (
                        dict(item)
                        for item in state.get("batches", [])
                        if isinstance(item, dict) and str(item.get("id") or "").strip() == clone_from_batch_id
                    ),
                    None,
                )
                if source_batch is None:
                    raise ValueError(f"Source batch '{clone_from_batch_id}' not found for clone")
                entry["files"] = self._clone_uploaded_files_from_batch(
                    source_batch=source_batch,
                    source_batch_id=clone_from_batch_id,
                    target_batch_id=batch_id,
                )
            state["batches"].append(entry)
            self._write_state(state)
        if not (clone_from_batch_id and clone_uploaded_files):
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
                for key, value in patch.items():
                    row[key] = value
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
                row["apply_runtime"] = None
                row["last_error"] = ""
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
        resolved_path = Path(path).resolve()
        size_value = int(meta.get("size") or 0)
        if size_value <= 0:
            try:
                if resolved_path.exists():
                    size_value = int(resolved_path.stat().st_size)
            except Exception:
                size_value = 0
        path_rel = ""
        try:
            path_rel = str(resolved_path.relative_to(self.root_dir.resolve()))
        except Exception:
            path_rel = ""
        payload = {
            "path": str(resolved_path),
            "path_rel": path_rel,
            "stored_name": resolved_path.name,
            "size": size_value,
            "generated_at": _utc_now_iso(),
            "row_count": int(meta.get("row_count") or 0),
            "property_count": int(meta.get("property_count") or 0),
            "database_id": str(meta.get("database_id") or "").strip(),
            "source_signature": str(meta.get("source_signature") or "").strip(),
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
                row["apply_runtime"] = None
                row["last_error"] = ""
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
                row["apply_runtime"] = None
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
                row["apply_runtime"] = None
                row["last_error"] = ""
                row = self._append_status_history(row, status="applied", event="applied", note=str(entry.get("apply_id") or ""))
                row["updated_at"] = _utc_now_iso()
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def mark_apply_queued(
        self,
        batch_id: str,
        *,
        task_id: str,
        database_id: str = "",
        import_batch_id: str = "",
        import_compounds: bool = True,
        import_experiments: bool = True,
    ) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        task_token = str(task_id or "").strip()
        if not token or not task_token:
            raise ValueError("batch_id and task_id are required")
        now = _utc_now_iso()
        with self._lock:
            state = self._read_state()
            db_token = str(database_id or "").strip()
            if db_token:
                for item in state.get("batches", []):
                    row_existing = dict(item or {})
                    existing_batch_id = str(row_existing.get("id") or "").strip()
                    if not existing_batch_id or existing_batch_id == token:
                        continue
                    runtime_existing = dict(row_existing.get("apply_runtime") or {})
                    phase = str(runtime_existing.get("phase") or "").strip().lower()
                    runtime_db = str(runtime_existing.get("database_id") or row_existing.get("selected_database_id") or "").strip()
                    if runtime_db == db_token and phase in {"queued", "running"}:
                        raise ValueError(
                            f"Database '{db_token}' is already applying in batch '{existing_batch_id}'."
                        )
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                row["status"] = "queued"
                row["apply_runtime"] = {
                    "task_id": task_token,
                    "phase": "queued",
                    "stage": "queued",
                    "message": "Queued",
                    "database_id": str(database_id or "").strip(),
                    "import_batch_id": str(import_batch_id or "").strip(),
                    "import_compounds": bool(import_compounds),
                    "import_experiments": bool(import_experiments),
                    "queued_at": now,
                    "started_at": "",
                    "finished_at": "",
                    "updated_at": now,
                    "error": "",
                    "timings_s": {},
                }
                row["last_error"] = ""
                row = self._append_status_history(row, status="queued", event="apply_queued", note=task_token)
                row["updated_at"] = now
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def mark_apply_running(self, batch_id: str, *, task_id: str) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        task_token = str(task_id or "").strip()
        if not token or not task_token:
            raise ValueError("batch_id and task_id are required")
        now = _utc_now_iso()
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                runtime = dict(row.get("apply_runtime") or {})
                current_task = str(runtime.get("task_id") or "").strip()
                if current_task and current_task != task_token:
                    raise ValueError(f"Batch '{token}' has another active apply task")
                runtime["task_id"] = task_token
                runtime["phase"] = "running"
                runtime["stage"] = "starting"
                runtime["message"] = "Running"
                if not str(runtime.get("queued_at") or "").strip():
                    runtime["queued_at"] = now
                runtime["started_at"] = now
                runtime["finished_at"] = ""
                runtime["updated_at"] = now
                runtime["error"] = ""
                row["status"] = "running"
                row["apply_runtime"] = runtime
                row["last_error"] = ""
                row = self._append_status_history(row, status="running", event="apply_running", note=task_token)
                row["updated_at"] = now
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def mark_apply_progress(
        self,
        batch_id: str,
        *,
        task_id: str,
        stage: str = "",
        message: str = "",
        timings_s: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        task_token = str(task_id or "").strip()
        if not token or not task_token:
            raise ValueError("batch_id and task_id are required")
        now = _utc_now_iso()
        stage_token = str(stage or "").strip()
        message_token = str(message or "").strip()
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                runtime = dict(row.get("apply_runtime") or {})
                current_task = str(runtime.get("task_id") or "").strip()
                if current_task and current_task != task_token:
                    raise ValueError(f"Batch '{token}' has another active apply task")
                runtime["task_id"] = task_token
                runtime["phase"] = "running"
                if not str(runtime.get("queued_at") or "").strip():
                    runtime["queued_at"] = now
                if not str(runtime.get("started_at") or "").strip():
                    runtime["started_at"] = now
                runtime["finished_at"] = ""
                runtime["updated_at"] = now
                runtime["error"] = ""
                if stage_token:
                    runtime["stage"] = stage_token
                if message_token:
                    runtime["message"] = message_token
                if isinstance(timings_s, dict):
                    merged_timings = dict(runtime.get("timings_s") or {})
                    for key, value in timings_s.items():
                        name = str(key or "").strip()
                        if not name:
                            continue
                        try:
                            merged_timings[name] = float(value)
                        except Exception:
                            continue
                    runtime["timings_s"] = merged_timings
                row["status"] = "running"
                row["apply_runtime"] = runtime
                row["last_error"] = ""
                row["updated_at"] = now
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def mark_apply_failed(self, batch_id: str, *, task_id: str, error: str) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        task_token = str(task_id or "").strip()
        error_text = self._truncate_text(error)
        if not token or not task_token:
            raise ValueError("batch_id and task_id are required")
        now = _utc_now_iso()
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                runtime = dict(row.get("apply_runtime") or {})
                current_task = str(runtime.get("task_id") or "").strip()
                if current_task and current_task != task_token:
                    raise ValueError(f"Batch '{token}' has another active apply task")
                runtime["task_id"] = task_token
                runtime["phase"] = "failed"
                runtime["stage"] = "failed"
                runtime["message"] = error_text
                if not str(runtime.get("queued_at") or "").strip():
                    runtime["queued_at"] = now
                if not str(runtime.get("started_at") or "").strip():
                    runtime["started_at"] = now
                runtime["finished_at"] = now
                runtime["updated_at"] = now
                runtime["error"] = error_text
                row["status"] = "failed"
                row["apply_runtime"] = runtime
                row["last_error"] = error_text
                row = self._append_status_history(row, status="failed", event="apply_failed", note=error_text[:240])
                row["updated_at"] = now
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def mark_delete_queued(
        self,
        batch_id: str,
        *,
        task_id: str,
        database_id: str = "",
        import_batch_id: str = "",
        rollback_compounds: bool = False,
        rollback_experiments: bool = False,
    ) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        task_token = str(task_id or "").strip()
        if not token or not task_token:
            raise ValueError("batch_id and task_id are required")
        now = _utc_now_iso()
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                row["status"] = "deleting"
                row["apply_runtime"] = {
                    "task_id": task_token,
                    "operation": "delete",
                    "phase": "deleting",
                    "stage": "queued",
                    "message": "Delete queued",
                    "database_id": str(database_id or "").strip(),
                    "import_batch_id": str(import_batch_id or "").strip(),
                    "rollback_compounds": bool(rollback_compounds),
                    "rollback_experiments": bool(rollback_experiments),
                    "queued_at": now,
                    "started_at": "",
                    "finished_at": "",
                    "updated_at": now,
                    "error": "",
                }
                row["last_error"] = ""
                row = self._append_status_history(row, status="deleting", event="delete_queued", note=task_token)
                row["updated_at"] = now
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def mark_delete_running(self, batch_id: str, *, task_id: str, stage: str = "", message: str = "") -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        task_token = str(task_id or "").strip()
        if not token or not task_token:
            raise ValueError("batch_id and task_id are required")
        now = _utc_now_iso()
        stage_token = str(stage or "").strip() or "running"
        message_token = str(message or "").strip() or "Deleting"
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                runtime = dict(row.get("apply_runtime") or {})
                current_task = str(runtime.get("task_id") or "").strip()
                if current_task and current_task != task_token:
                    raise ValueError(f"Batch '{token}' has another active task")
                runtime["task_id"] = task_token
                runtime["operation"] = "delete"
                runtime["phase"] = "deleting"
                runtime["stage"] = stage_token
                runtime["message"] = message_token
                if not str(runtime.get("queued_at") or "").strip():
                    runtime["queued_at"] = now
                if not str(runtime.get("started_at") or "").strip():
                    runtime["started_at"] = now
                runtime["finished_at"] = ""
                runtime["updated_at"] = now
                runtime["error"] = ""
                row["status"] = "deleting"
                row["apply_runtime"] = runtime
                row["last_error"] = ""
                row["updated_at"] = now
                state["batches"][index] = row
                self._write_state(state)
                return row
        raise ValueError(f"Batch '{token}' not found")

    def mark_delete_failed(self, batch_id: str, *, task_id: str, error: str) -> Dict[str, Any]:
        token = str(batch_id or "").strip()
        task_token = str(task_id or "").strip()
        error_text = self._truncate_text(error)
        if not token or not task_token:
            raise ValueError("batch_id and task_id are required")
        now = _utc_now_iso()
        with self._lock:
            state = self._read_state()
            for index, item in enumerate(state.get("batches", [])):
                if str((item or {}).get("id") or "").strip() != token:
                    continue
                row = dict(item)
                runtime = dict(row.get("apply_runtime") or {})
                current_task = str(runtime.get("task_id") or "").strip()
                if current_task and current_task != task_token:
                    raise ValueError(f"Batch '{token}' has another active task")
                runtime["task_id"] = task_token
                runtime["operation"] = "delete"
                runtime["phase"] = "failed"
                runtime["stage"] = "delete_failed"
                runtime["message"] = error_text
                if not str(runtime.get("queued_at") or "").strip():
                    runtime["queued_at"] = now
                if not str(runtime.get("started_at") or "").strip():
                    runtime["started_at"] = now
                runtime["finished_at"] = now
                runtime["updated_at"] = now
                runtime["error"] = error_text
                row["status"] = "failed"
                row["apply_runtime"] = runtime
                row["last_error"] = error_text
                row = self._append_status_history(row, status="failed", event="delete_failed", note=error_text[:240])
                row["updated_at"] = now
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
                    row["apply_runtime"] = None
                    row["last_error"] = ""
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

    def list_database_operation_locks(
        self,
        *,
        database_id: str = "",
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        db_token = str(database_id or "").strip()
        with self._lock:
            state = self._read_state()
            rows = [dict(item) for item in state.get("database_operation_locks", []) if isinstance(item, dict)]
        filtered: List[Dict[str, Any]] = []
        for item in rows:
            row = dict(item)
            if db_token and str(row.get("database_id") or "").strip() != db_token:
                continue
            status = str(row.get("status") or "").strip().lower()
            if active_only and status != "active":
                continue
            filtered.append(row)
        filtered.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("id") or ""),
            )
        )
        return filtered

    def acquire_database_operation_lock(
        self,
        *,
        database_id: str,
        operation: str,
        batch_id: str = "",
        task_id: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        db_token = str(database_id or "").strip()
        op_token = str(operation or "").strip().lower() or "update"
        if not db_token:
            raise ValueError("database_id is required")
        now = _utc_now_iso()
        with self._lock:
            state = self._read_state()
            locks = [dict(item) for item in state.get("database_operation_locks", []) if isinstance(item, dict)]
            for row in locks:
                if str(row.get("status") or "").strip().lower() != "active":
                    continue
                if str(row.get("database_id") or "").strip() != db_token:
                    continue
                holder_op = str(row.get("operation") or "").strip().lower() or "update"
                holder_batch = str(row.get("batch_id") or "").strip()
                holder_task = str(row.get("task_id") or "").strip()
                holder_token = holder_task or holder_batch or str(row.get("id") or "").strip()
                raise ValueError(
                    f"Database '{db_token}' is busy with {holder_op} ({holder_token})."
                )
            entry = {
                "id": f"dblock_{uuid.uuid4().hex[:12]}",
                "database_id": db_token,
                "operation": op_token,
                "batch_id": str(batch_id or "").strip(),
                "task_id": str(task_id or "").strip(),
                "note": self._truncate_text(note, limit=512),
                "status": "active",
                "created_at": now,
                "updated_at": now,
                "released_at": "",
                "error": "",
            }
            locks.append(entry)
            state["database_operation_locks"] = locks
            self._write_state(state)
            return entry

    def release_database_operation_lock(
        self,
        lock_id: str,
        *,
        status: str = "released",
        error: str = "",
    ) -> Dict[str, Any]:
        token = str(lock_id or "").strip()
        if not token:
            raise ValueError("lock_id is required")
        status_token = str(status or "released").strip().lower()
        if status_token not in {"released", "failed", "expired", "canceled"}:
            status_token = "released"
        now = _utc_now_iso()
        err = self._truncate_text(error)
        with self._lock:
            state = self._read_state()
            locks = [dict(item) for item in state.get("database_operation_locks", []) if isinstance(item, dict)]
            for index, item in enumerate(locks):
                if str(item.get("id") or "").strip() != token:
                    continue
                row = dict(item)
                current_status = str(row.get("status") or "").strip().lower()
                if current_status == "active":
                    row["status"] = status_token
                    row["updated_at"] = now
                    row["released_at"] = now
                    if err:
                        row["error"] = err
                elif err and not str(row.get("error") or "").strip():
                    row["error"] = err
                locks[index] = row
                state["database_operation_locks"] = locks
                self._write_state(state)
                return row
        raise ValueError(f"Database operation lock '{token}' not found")

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
                row["result"] = self._normalize_sync_result_payload(result)
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
        err = self._truncate_text(error)
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

        discovered_preferred_property_by_family: Dict[str, str] = {}
        for prop in normalized_properties:
            family = _canonical_property_family(prop)
            if not family:
                continue
            current = discovered_preferred_property_by_family.get(family)
            if not current:
                discovered_preferred_property_by_family[family] = prop
                continue
            if _property_preference_rank(prop, family) < _property_preference_rank(current, family):
                discovered_preferred_property_by_family[family] = prop

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
            property_name_by_lower: Dict[str, str] = {str(item or "").strip().lower(): str(item or "").strip() for item in normalized_properties}
            preferred_property_by_family: Dict[str, str] = dict(discovered_preferred_property_by_family)

            # Preserve existing method-edited output property per family for this database.
            # This prevents sync from reverting back to legacy tokens when both names coexist.
            methods_for_db = [
                dict(item)
                for item in methods
                if str(item.get("id") or "").strip()
                and str(item.get("reference") or "").strip() in {"", db_token}
            ]
            methods_for_db.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
            methods_for_db.sort(
                key=lambda row: 0 if not str(row.get("id") or "").strip().startswith("method_auto_") else 1
            )
            for item in methods_for_db:
                output_prop = str(item.get("output_property") or "").strip()
                if not output_prop:
                    continue
                canonical_output = property_name_by_lower.get(output_prop.lower(), "")
                if not canonical_output:
                    continue
                family = _canonical_property_family(canonical_output)
                if not family:
                    continue
                preferred_property_by_family[family] = canonical_output

            selected_property_keys = {value.lower() for value in preferred_property_by_family.values() if value}
            selected_properties = [prop for prop in normalized_properties if prop.lower() in selected_property_keys]

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
                mapped_prop = str(row.get("mmp_property") or "").strip()
                family = _canonical_property_family(mapped_prop or source)
                preferred = str(preferred_property_by_family.get(family) or "").strip()
                mapping_id = str(row.get("id") or "").strip()
                mapping_notes = str(row.get("notes") or "").strip().lower()
                auto_generated = mapping_id.startswith("map_auto_") or mapping_notes in {
                    "method-bound mapping.",
                    "method bound mapping.",
                    "optional",
                }
                compare_token = (mapped_prop or source).lower()
                if preferred and compare_token != preferred.lower() and auto_generated:
                    removed_mappings += 1
                    changed = True
                    continue
                filtered_mappings.append(row)
            mappings = filtered_mappings

            method_id_by_output: Dict[str, str] = {}
            method_by_id: Dict[str, Dict[str, Any]] = {}
            for item in methods:
                output = str(item.get("output_property") or "").strip()
                method_id = str(item.get("id") or "").strip()
                if method_id:
                    method_by_id[method_id] = dict(item)
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
            db_mapping_indexes_by_method: Dict[str, int] = {}
            for index, item in enumerate(mappings):
                if str(item.get("database_id") or "").strip() != db_token:
                    continue
                source = str(item.get("source_property") or "").strip()
                if source and source.lower() not in db_mapping_indexes_by_source:
                    db_mapping_indexes_by_source[source.lower()] = index
                method_ref = str(item.get("method_id") or "").strip()
                if method_ref and method_ref not in db_mapping_indexes_by_method:
                    db_mapping_indexes_by_method[method_ref] = index

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
                    method_by_id[method_id] = dict(methods[-1])
                    created_methods += 1
                    changed = True

                method_row = dict(method_by_id.get(method_id) or {})
                source_for_mapping = str(method_row.get("key") or "").strip() or prop
                source_for_mapping_key = source_for_mapping.lower()

                existing_idx = db_mapping_indexes_by_method.get(method_id)
                if existing_idx is None:
                    existing_idx = db_mapping_indexes_by_source.get(source_for_mapping_key)
                if existing_idx is None:
                    existing_idx = db_mapping_indexes_by_source.get(prop_key)
                if existing_idx is None:
                    mapping_transform = str(method_display_transform_by_id.get(method_id) or "none").strip().lower() or "none"
                    mappings.append(
                        {
                            "id": _auto_mapping_id(db_token, source_for_mapping),
                            "database_id": db_token,
                            "source_property": source_for_mapping,
                            "mmp_property": prop,
                            "method_id": method_id,
                            "value_transform": mapping_transform,
                            "notes": "Method-bound mapping.",
                            "updated_at": now,
                        }
                    )
                    db_mapping_indexes_by_source[source_for_mapping_key] = len(mappings) - 1
                    db_mapping_indexes_by_source[prop_key] = len(mappings) - 1
                    db_mapping_indexes_by_method[method_id] = len(mappings) - 1
                    created_mappings += 1
                    changed = True
                    continue

                current = dict(mappings[existing_idx])
                current_source = str(current.get("source_property") or "").strip()
                current_prop = str(current.get("mmp_property") or "").strip()
                current_method = str(current.get("method_id") or "").strip()
                current_value_transform = str(current.get("value_transform") or "").strip().lower()
                current_id = str(current.get("id") or "").strip()
                current_notes = str(current.get("notes") or "").strip().lower()
                current_auto_generated = current_id.startswith("map_auto_") or current_notes in {
                    "method-bound mapping.",
                    "method bound mapping.",
                    "optional",
                }
                row_changed = False
                if current_auto_generated and current_source.lower() != source_for_mapping_key:
                    current["source_property"] = source_for_mapping
                    current_source = source_for_mapping
                    row_changed = True
                if not current_prop or (current_auto_generated and current_prop.lower() != prop_key):
                    current["mmp_property"] = prop
                    current_prop = prop
                    row_changed = True
                if not current_method and current_prop.lower() == prop_key:
                    current["method_id"] = method_id
                    current_method = method_id
                    row_changed = True
                elif current_auto_generated and current_method != method_id:
                    current["method_id"] = method_id
                    current_method = method_id
                    row_changed = True
                if not current_value_transform:
                    current["value_transform"] = "none"
                    row_changed = True
                if row_changed:
                    current["updated_at"] = now
                    mappings[existing_idx] = current
                    db_mapping_indexes_by_source[source_for_mapping_key] = existing_idx
                    if current_source:
                        db_mapping_indexes_by_source[current_source.lower()] = existing_idx
                    db_mapping_indexes_by_method[method_id] = existing_idx
                    updated_mappings += 1
                    changed = True

            deduped_mappings: List[Dict[str, Any]] = []
            seen_source_by_db: set[str] = set()
            for item in mappings:
                row = dict(item)
                row_db = str(row.get("database_id") or "").strip()
                source = str(row.get("source_property") or "").strip()
                if row_db != db_token or not source:
                    deduped_mappings.append(row)
                    continue
                source_key = source.lower()
                if source_key in seen_source_by_db:
                    removed_mappings += 1
                    changed = True
                    continue
                seen_source_by_db.add(source_key)
                deduped_mappings.append(row)
            mappings = deduped_mappings

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
