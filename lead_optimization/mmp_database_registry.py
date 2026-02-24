from __future__ import annotations

import json
import os
import re
import time
import threading
import uuid
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import psycopg
except Exception:
    psycopg = None


_PG_DSN_PREFIXES = ("postgres://", "postgresql://")
_PG_SCHEMA_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_MMP_TABLES = (
    "dataset",
    "compound",
    "rule_smiles",
    "constant_smiles",
    "rule",
    "rule_environment",
    "pair",
)
_METADATA_COLUMNS = ("base", "unit", "display_name", "display_base", "display_unit", "change_displayed")
_CATALOG_CACHE_TTL_S = max(0.0, float(str(os.getenv("LEAD_OPT_MMP_CATALOG_CACHE_TTL_S", "8") or "8")))
_CATALOG_LIVE_COUNTS = str(os.getenv("LEAD_OPT_MMP_CATALOG_LIVE_COUNTS", "") or "").strip().lower() in {"1", "true", "yes", "on"}
_CATALOG_CACHE_LOCK = threading.Lock()
_CATALOG_CACHE: Dict[Tuple[bool, bool, bool], Tuple[float, Dict[str, Any]]] = {}
_STATUS_VALUES = {"ready", "building", "failed"}


def _is_postgres_dsn(candidate: str) -> bool:
    token = str(candidate or "").strip().lower()
    return token.startswith(_PG_DSN_PREFIXES)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_status(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    return token if token in _STATUS_VALUES else ""


def _clone_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Keep cached payload immutable for callers.
    return json.loads(json.dumps(payload, ensure_ascii=False))


def _read_catalog_cache(
    include_hidden: bool,
    include_stats: bool,
    include_properties: bool,
) -> Optional[Dict[str, Any]]:
    if _CATALOG_CACHE_TTL_S <= 0:
        return None
    key = (bool(include_hidden), bool(include_stats), bool(include_properties))
    now = time.time()
    with _CATALOG_CACHE_LOCK:
        hit = _CATALOG_CACHE.get(key)
        if not hit:
            return None
        created_at, payload = hit
        if now - created_at > _CATALOG_CACHE_TTL_S:
            _CATALOG_CACHE.pop(key, None)
            return None
        return _clone_payload(payload)


def _write_catalog_cache(
    include_hidden: bool,
    include_stats: bool,
    include_properties: bool,
    payload: Dict[str, Any],
) -> None:
    if _CATALOG_CACHE_TTL_S <= 0:
        return
    key = (bool(include_hidden), bool(include_stats), bool(include_properties))
    with _CATALOG_CACHE_LOCK:
        _CATALOG_CACHE[key] = (time.time(), _clone_payload(payload))


def _clear_catalog_cache() -> None:
    with _CATALOG_CACHE_LOCK:
        _CATALOG_CACHE.clear()


def _registry_path() -> Path:
    env_path = str(os.getenv("LEAD_OPT_MMP_DB_REGISTRY", "") or "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path(__file__).resolve().parent / "data" / "mmp_db_registry.json"


def _resolve_default_database() -> Tuple[str, str]:
    db_url = str(os.getenv("LEAD_OPT_MMP_DB_URL", "") or "").strip()
    if db_url and _is_postgres_dsn(db_url):
        return db_url, "postgres"
    try:
        config_path = Path(__file__).resolve().parent / "config.py"
        spec = importlib.util.spec_from_file_location("lead_optimization.config", config_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cfg = module.load_config()
            config_db_url = str(getattr(cfg.mmp_database, "database_url", "") or "").strip()
            if config_db_url and _is_postgres_dsn(config_db_url):
                return config_db_url, "postgres"
    except Exception:
        pass
    return "", ""


def _parse_schema_from_dsn(dsn: str) -> str:
    try:
        parsed = urlparse(str(dsn or "").strip())
        query = dict(parse_qsl(parsed.query or "", keep_blank_values=True))
        for key in ("schema", "search_path", "currentSchema"):
            value = str(query.get(key) or "").strip()
            if not value:
                continue
            schema = value.split(",")[0].strip()
            if _PG_SCHEMA_TOKEN_RE.match(schema):
                return schema
    except Exception:
        pass
    env_schema = str(os.getenv("LEAD_OPT_MMP_DB_SCHEMA", "") or "").strip()
    if env_schema and _PG_SCHEMA_TOKEN_RE.match(env_schema):
        return env_schema
    return "public"


def _dsn_with_schema(dsn: str, schema: str) -> str:
    parsed = urlparse(str(dsn or "").strip())
    query = dict(parse_qsl(parsed.query or "", keep_blank_values=True))
    # psycopg/libpq doesn't accept JDBC-style schema URI params like currentSchema.
    # Keep runtime DSN clean; schema is applied via SET search_path after connect.
    query.pop("currentSchema", None)
    query.pop("schema", None)
    query.pop("search_path", None)
    new_query = urlencode(query)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def _redact_postgres_dsn(dsn: str) -> str:
    token = str(dsn or "").strip()
    if not token:
        return token
    try:
        parsed = urlparse(token)
    except Exception:
        return "***"
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    database = parsed.path.lstrip("/") or "<db>"
    return f"{parsed.scheme}://***@{host}:{port}/{database}"


def _make_postgres_entry_id(dsn: str, schema: str) -> str:
    parsed = urlparse(str(dsn or "").strip())
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    database = parsed.path.lstrip("/") or "postgres"
    safe_schema = schema if _PG_SCHEMA_TOKEN_RE.match(schema) else "public"
    return f"pg:{host}:{port}:{database}:{safe_schema}"


def _read_registry_file() -> List[Dict[str, Any]]:
    path = _registry_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = payload.get("entries") if isinstance(payload, dict) else None
    return items if isinstance(items, list) else []


def _normalize_manual_entry(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    database_url = str(raw.get("database_url") or "").strip()
    schema = str(raw.get("schema") or "").strip()
    backend = "postgres" if _is_postgres_dsn(database_url) else ""
    if backend != "postgres":
        return None
    if not schema:
        schema = _parse_schema_from_dsn(database_url)
    if not _PG_SCHEMA_TOKEN_RE.match(schema):
        schema = _parse_schema_from_dsn(database_url)
    entry_id = _make_postgres_entry_id(database_url, schema)
    if not entry_id:
        return None
    label = str(raw.get("label") or "").strip()
    if not label:
        label = schema
    status = _normalize_status(raw.get("status"))
    status_message = str(raw.get("status_message") or "").strip()
    status_updated_at = str(raw.get("status_updated_at") or "").strip()
    status_token = str(raw.get("status_token") or "").strip()
    return {
        "id": entry_id,
        "label": label,
        "description": str(raw.get("description") or "").strip(),
        "visible": bool(raw.get("visible", True)),
        "is_default": bool(raw.get("is_default", False)),
        "database_url": database_url,
        "schema": schema,
        "backend": backend,
        "source": "manual",
        "status": status,
        "status_message": status_message,
        "status_updated_at": status_updated_at,
        "status_token": status_token,
    }


def _get_pg_property_catalog(conn: Any, schema: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = 'property_name'
            """,
            [schema],
        )
        columns = {str(row[0] or "") for row in cursor.fetchall()}
    if "name" not in columns:
        return []

    selected_columns = ["name"] + [col for col in _METADATA_COLUMNS if col in columns]
    select_sql = ", ".join(f'"{col}"' for col in selected_columns)
    with conn.cursor() as cursor:
        cursor.execute(f'SELECT {select_sql} FROM "{schema}".property_name ORDER BY name')
        rows = cursor.fetchall()

    result: List[Dict[str, Any]] = []
    for row in rows:
        item = {"name": str(row[0] or "").strip()}
        if not item["name"]:
            continue
        for idx, col in enumerate(selected_columns[1:], start=1):
            item[col] = str(row[idx] or "").strip()
        display = str(item.get("display_name") or "").strip()
        item["label"] = display or item["name"]
        result.append(item)
    return result


def _empty_mmp_stats() -> Dict[str, Optional[int]]:
    return {
        "compounds": None,
        "rules": None,
        "pairs": None,
    }


def _quote_pg_identifier(token: str) -> str:
    value = str(token or "").strip()
    if not _PG_SCHEMA_TOKEN_RE.match(value):
        raise ValueError(f"Invalid PostgreSQL identifier: {token}")
    return f'"{value}"'


def _to_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _get_pg_dataset_counts(conn: Any, schema: str) -> Dict[str, Optional[int]]:
    stats = _empty_mmp_stats()
    quoted_schema = ""
    try:
        quoted_schema = _quote_pg_identifier(schema)
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT num_compounds, num_rules, num_pairs FROM {quoted_schema}.dataset ORDER BY id LIMIT 1"
            )
            row = cursor.fetchone()
    except Exception:
        return stats
    if not row:
        return stats

    compounds = _to_int_or_none(row[0] if len(row) > 0 else None)
    rules = _to_int_or_none(row[1] if len(row) > 1 else None)
    pairs = _to_int_or_none(row[2] if len(row) > 2 else None)
    if compounds is not None and compounds >= 0:
        stats["compounds"] = compounds
    if rules is not None and rules >= 0:
        stats["rules"] = rules
    if pairs is not None and pairs >= 0:
        stats["pairs"] = pairs

    # Fast adaptive path: use pg_class row estimates to recover from stale/null
    # dataset counters without paying full COUNT(*) cost on every request.
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT relname, reltuples::bigint
                FROM pg_class c
                INNER JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s
                  AND c.relkind IN ('r', 'p')
                  AND c.relname IN ('compound', 'rule', 'pair')
                """,
                [schema],
            )
            estimate_rows = {str(row[0] or "").strip(): _to_int_or_none(row[1]) for row in cursor.fetchall()}
    except Exception:
        estimate_rows = {}

    adaptive_candidates = (
        ("compounds", estimate_rows.get("compound")),
        ("rules", estimate_rows.get("rule")),
        ("pairs", estimate_rows.get("pair")),
    )
    for key, estimate in adaptive_candidates:
        current = stats.get(key)
        if estimate is None or estimate < 0:
            continue
        if current is None or current < 0:
            stats[key] = estimate
            continue
        diff = abs(int(estimate) - int(current))
        if diff >= 100 and (diff / max(int(current), 1)) >= 0.02:
            stats[key] = int(estimate)

    # Optional slower path: live counts from core tables.
    if _CATALOG_LIVE_COUNTS and quoted_schema:
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM {quoted_schema}.compound")
                live_compounds = _to_int_or_none((cursor.fetchone() or [None])[0])
                cursor.execute(f"SELECT COUNT(*) FROM {quoted_schema}.rule")
                live_rules = _to_int_or_none((cursor.fetchone() or [None])[0])
                cursor.execute(f"SELECT COUNT(*) FROM {quoted_schema}.pair")
                live_pairs = _to_int_or_none((cursor.fetchone() or [None])[0])
            if live_compounds is not None and live_compounds >= 0:
                stats["compounds"] = live_compounds
            if live_rules is not None and live_rules >= 0:
                stats["rules"] = live_rules
            if live_pairs is not None and live_pairs >= 0:
                stats["pairs"] = live_pairs
        except Exception:
            # Keep dataset-based fallback when live counting is unavailable.
            pass
    return stats


def _discover_postgres_schemas(
    default_dsn: str,
    *,
    include_stats: bool = False,
    include_properties: bool = True,
) -> List[Dict[str, Any]]:
    if psycopg is None:
        return []
    try:
        conn = psycopg.connect(default_dsn, autocommit=True)
    except Exception:
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with conn.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(_REQUIRED_MMP_TABLES))
            cursor.execute(
                f"""
                SELECT table_schema
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                  AND table_name IN ({placeholders})
                GROUP BY table_schema
                HAVING COUNT(DISTINCT table_name) = %s
                ORDER BY CASE WHEN table_schema = 'public' THEN 0 ELSE 1 END, table_schema
                """,
                [*_REQUIRED_MMP_TABLES, len(_REQUIRED_MMP_TABLES)],
            )
            schemas = [str(row[0] or "").strip() for row in cursor.fetchall()]
        for schema in schemas:
            if not schema:
                continue
            entry_id = _make_postgres_entry_id(default_dsn, schema)
            entry_stats = _get_pg_dataset_counts(conn, schema) if include_stats else _empty_mmp_stats()
            entries.append(
                {
                    "id": entry_id,
                    "label": schema,
                    "description": "",
                    "visible": True,
                    "is_default": schema == "public",
                    "database_url": default_dsn,
                    "schema": schema,
                    "backend": "postgres",
                    "source": "discovered",
                    "properties": _get_pg_property_catalog(conn, schema) if include_properties else [],
                    "stats": entry_stats,
                    # Status is authoritative from lifecycle/engine updates, not inferred from counts.
                    "status": "",
                    "status_message": "",
                    "status_updated_at": "",
                    "status_token": "",
                }
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return entries


def _build_discovered_entries(*, include_stats: bool = False, include_properties: bool = True) -> List[Dict[str, Any]]:
    database, backend = _resolve_default_database()
    if backend == "postgres" and _is_postgres_dsn(database):
        discovered = _discover_postgres_schemas(
            database,
            include_stats=include_stats,
            include_properties=include_properties,
        )
        if discovered:
            return discovered
        # Fallback: keep the configured runtime DB visible in catalog even when
        # schema discovery cannot run (missing psycopg, transient DB outage, etc).
        schema = _parse_schema_from_dsn(database)
        return [
            {
                "id": _make_postgres_entry_id(database, schema),
                "label": schema,
                "description": "Configured runtime database (schema discovery unavailable).",
                "visible": True,
                "is_default": True,
                "database_url": database,
                "schema": schema,
                "backend": "postgres",
                "source": "configured",
                "properties": [],
                "stats": _empty_mmp_stats(),
                "status": "building",
                "status_message": "Configured runtime database (schema discovery unavailable).",
                "status_updated_at": "",
                "status_token": "",
            }
        ]
    # Lead Optimization runtime is PostgreSQL-only now.
    return []


def _read_manual_entries() -> List[Dict[str, Any]]:
    manual_entries_raw = _read_registry_file()
    normalized_entries: List[Dict[str, Any]] = []
    changed = False
    dedup: Dict[str, Dict[str, Any]] = {}

    for raw in manual_entries_raw:
        item_raw = raw if isinstance(raw, dict) else {}
        normalized = _normalize_manual_entry(item_raw)
        if not normalized:
            changed = True
            continue
        key = str(normalized.get("id") or "").strip()
        if not key:
            changed = True
            continue
        if key in dedup:
            changed = True
        dedup[key] = normalized
        normalized_entries.append(normalized)

        raw_id = str(item_raw.get("id") or "").strip()
        raw_schema = str(item_raw.get("schema") or "").strip()
        raw_label = str(item_raw.get("label") or "").strip()
        if raw_id != key or raw_schema != str(normalized.get("schema") or "") or raw_label != str(normalized.get("label") or ""):
            changed = True

    result = list(dedup.values())
    if changed:
        try:
            _write_manual_entries(result)
        except Exception:
            pass
    return result


def _write_manual_entries(entries: List[Dict[str, Any]]) -> None:
    normalized_entries: List[Dict[str, Any]] = []
    default_assigned = False
    for raw in entries if isinstance(entries, list) else []:
        item = _normalize_manual_entry(raw if isinstance(raw, dict) else {})
        if not item:
            continue
        is_default = bool(item.get("is_default", False))
        if is_default and default_assigned:
            is_default = False
        if is_default:
            default_assigned = True
        normalized_entries.append(
                {
                    "id": str(item.get("id") or "").strip(),
                    "label": str(item.get("label") or "").strip(),
                    "description": str(item.get("description") or "").strip(),
                    "visible": bool(item.get("visible", True)),
                    "is_default": is_default,
                    "database_url": str(item.get("database_url") or "").strip(),
                    "schema": str(item.get("schema") or "").strip(),
                    "status": _normalize_status(item.get("status")),
                    "status_message": str(item.get("status_message") or "").strip(),
                    "status_updated_at": str(item.get("status_updated_at") or "").strip(),
                    "status_token": str(item.get("status_token") or "").strip(),
                }
            )
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "entries": normalized_entries}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _clear_catalog_cache()


def get_mmp_database_catalog(
    include_hidden: bool = False,
    *,
    include_stats: bool = False,
    include_properties: bool = True,
) -> Dict[str, Any]:
    cached = _read_catalog_cache(include_hidden, include_stats, include_properties)
    if cached is not None:
        return cached
    discovered_entries = _build_discovered_entries(
        include_stats=include_stats,
        include_properties=include_properties,
    )
    manual_entries = _read_manual_entries()

    merged: Dict[str, Dict[str, Any]] = {}
    for item in discovered_entries:
        merged[str(item["id"])] = dict(item)

    for override in manual_entries:
        key = str(override["id"])
        if key in merged:
            current = merged[key]
            for field in (
                "label",
                "description",
                "visible",
                "is_default",
                "status",
                "status_message",
                "status_updated_at",
                "status_token",
            ):
                if field in override:
                    current[field] = override[field]
            if override.get("database_url"):
                current["database_url"] = override["database_url"]
            if override.get("schema"):
                current["schema"] = override["schema"]
        else:
            item = dict(override)
            if item.get("backend") == "postgres":
                dsn = str(item.get("database_url") or "").strip()
                schema = str(item.get("schema") or "").strip() or _parse_schema_from_dsn(dsn)
                item["schema"] = schema
                item["properties"] = []
                item["stats"] = _empty_mmp_stats()
                item["status"] = _normalize_status(item.get("status")) or ""
                item["status_message"] = str(item.get("status_message") or "").strip()
                item["status_updated_at"] = str(item.get("status_updated_at") or "").strip()
                item["status_token"] = str(item.get("status_token") or "").strip()
                if _is_postgres_dsn(dsn) and psycopg is not None:
                    try:
                        conn = psycopg.connect(dsn, autocommit=True)
                        try:
                            if include_properties:
                                item["properties"] = _get_pg_property_catalog(conn, schema)
                            if include_stats:
                                item["stats"] = _get_pg_dataset_counts(conn, schema)
                        finally:
                            conn.close()
                    except Exception:
                        item["properties"] = []
                        item["stats"] = _empty_mmp_stats()
            merged[key] = item

    entries = list(merged.values())
    for item in entries:
        if not isinstance(item, dict):
            continue
        status = _normalize_status(item.get("status"))
        item["status"] = status or "building"
        item["status_message"] = str(item.get("status_message") or "").strip()
        item["status_updated_at"] = str(item.get("status_updated_at") or "").strip()
        item["status_token"] = str(item.get("status_token") or "").strip()
    entries.sort(key=lambda item: (0 if bool(item.get("is_default")) else 1, str(item.get("label") or item.get("id") or "")))

    default_id = ""
    for item in entries:
        if bool(item.get("is_default")) and bool(item.get("visible", True)):
            default_id = str(item.get("id") or "")
            break
    if not default_id:
        for item in entries:
            if bool(item.get("visible", True)):
                default_id = str(item.get("id") or "")
                break

    visible_entries = [item for item in entries if bool(item.get("visible", True))]
    result_entries = entries if include_hidden else visible_entries

    payload = {
        "default_database_id": default_id,
        "databases": result_entries,
        "total": len(result_entries),
        "total_visible": len(visible_entries),
        "total_all": len(entries),
    }
    _write_catalog_cache(include_hidden, include_stats, include_properties, payload)
    return payload


def resolve_mmp_database(database_id: str = "", *, include_hidden: bool = False) -> Dict[str, Any]:
    catalog = get_mmp_database_catalog(include_hidden=True, include_stats=False)
    entries = catalog.get("databases") if isinstance(catalog, dict) else []
    by_id = {str(item.get("id") or ""): item for item in entries if isinstance(item, dict)}
    selected_id = str(database_id or "").strip() or str(catalog.get("default_database_id") or "").strip()
    if not selected_id:
        raise ValueError("No MMP database is configured. Build/import a database first.")
    selected = by_id.get(selected_id)
    if not selected:
        raise ValueError(f"MMP database '{selected_id}' is not found.")
    if (not include_hidden) and (not bool(selected.get("visible", True))):
        raise ValueError(f"MMP database '{selected_id}' is hidden by admin policy.")

    backend = str(selected.get("backend") or "").strip().lower()
    database_url = str(selected.get("database_url") or "").strip()
    schema = str(selected.get("schema") or "").strip()
    runtime_database = ""
    if backend == "postgres":
        if not _is_postgres_dsn(database_url):
            raise ValueError(f"MMP database '{selected_id}' has invalid PostgreSQL DSN.")
        safe_schema = schema if _PG_SCHEMA_TOKEN_RE.match(schema) else _parse_schema_from_dsn(database_url)
        runtime_database = _dsn_with_schema(database_url, safe_schema)
        selected["schema"] = safe_schema
    else:
        raise ValueError(f"MMP database '{selected_id}' has unsupported backend '{backend}'.")

    item = dict(selected)
    item["runtime_database"] = runtime_database
    item["database_url_redacted"] = _redact_postgres_dsn(database_url)
    return item


def _resolve_entry_for_status_update(
    *,
    database_id: str = "",
    database_url: str = "",
    schema: str = "",
) -> Dict[str, str]:
    requested_id = str(database_id or "").strip()
    dsn = str(database_url or "").strip()
    safe_schema = str(schema or "").strip()
    if not requested_id and dsn:
        if not safe_schema:
            safe_schema = _parse_schema_from_dsn(dsn)
        requested_id = _make_postgres_entry_id(dsn, safe_schema)
    catalog = get_mmp_database_catalog(include_hidden=True, include_stats=False, include_properties=False)
    for item in catalog.get("databases", []) if isinstance(catalog, dict) else []:
        row = item if isinstance(item, dict) else {}
        row_id = str(row.get("id") or "").strip()
        row_url = str(row.get("database_url") or "").strip()
        row_schema = str(row.get("schema") or "").strip()
        if requested_id and row_id == requested_id:
            return {"id": row_id, "database_url": row_url, "schema": row_schema}
        if dsn and safe_schema and row_url == dsn and row_schema == safe_schema:
            return {"id": row_id, "database_url": row_url, "schema": row_schema}
    if requested_id and dsn and safe_schema:
        return {"id": requested_id, "database_url": dsn, "schema": safe_schema}
    if requested_id:
        raise ValueError(f"MMP database '{requested_id}' not found.")
    if dsn and safe_schema:
        return {"id": _make_postgres_entry_id(dsn, safe_schema), "database_url": dsn, "schema": safe_schema}
    raise ValueError("database_id or database_url+schema is required.")


def begin_mmp_database_operation(
    *,
    database_id: str = "",
    database_url: str = "",
    schema: str = "",
    message: str = "",
) -> str:
    resolved = _resolve_entry_for_status_update(
        database_id=database_id,
        database_url=database_url,
        schema=schema,
    )
    token = f"op_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    set_mmp_database_status(
        database_id=resolved["id"],
        database_url=resolved["database_url"],
        schema=resolved["schema"],
        status="building",
        message=message,
        token=token,
    )
    return token


def finish_mmp_database_operation(
    *,
    database_id: str = "",
    database_url: str = "",
    schema: str = "",
    token: str = "",
    success: bool = True,
    message: str = "",
) -> Dict[str, Any]:
    return set_mmp_database_status(
        database_id=database_id,
        database_url=database_url,
        schema=schema,
        status="ready" if bool(success) else "failed",
        message=message,
        token=token,
        enforce_token=True,
    )


def set_mmp_database_status(
    *,
    database_id: str = "",
    database_url: str = "",
    schema: str = "",
    status: str,
    message: str = "",
    token: str = "",
    enforce_token: bool = False,
) -> Dict[str, Any]:
    status_token = _normalize_status(status)
    if not status_token:
        raise ValueError(f"Unsupported status: {status}")
    resolved = _resolve_entry_for_status_update(
        database_id=database_id,
        database_url=database_url,
        schema=schema,
    )
    target_id = resolved["id"]
    target_url = resolved["database_url"]
    target_schema = resolved["schema"]

    manual_entries = _read_manual_entries()
    by_id = {str(item.get("id") or ""): dict(item) for item in manual_entries}
    current = by_id.get(target_id)
    if not current:
        source_catalog = get_mmp_database_catalog(include_hidden=True, include_stats=False, include_properties=False)
        source = None
        for item in source_catalog.get("databases", []) if isinstance(source_catalog, dict) else []:
            if str((item or {}).get("id") or "") == target_id:
                source = item if isinstance(item, dict) else {}
                break
        current = {
            "id": target_id,
            "label": str((source or {}).get("label") or target_schema or target_id).strip() or target_id,
            "description": str((source or {}).get("description") or "").strip(),
            "visible": bool((source or {}).get("visible", True)),
            "is_default": bool((source or {}).get("is_default", False)),
            "database_url": target_url,
            "schema": target_schema,
            "backend": "postgres",
            "source": "manual",
        }
    current_token = str(current.get("status_token") or "").strip()
    input_token = str(token or "").strip()
    if enforce_token and input_token and current_token and current_token != input_token:
        return get_mmp_database_catalog(include_hidden=True, include_stats=True)

    now = _utc_now_iso()
    current["status"] = status_token
    current["status_message"] = str(message or "").strip()
    current["status_updated_at"] = now
    if status_token == "building":
        current["status_token"] = input_token or current_token or f"op_{uuid.uuid4().hex[:8]}"
    else:
        current["status_token"] = ""
    by_id[target_id] = current
    _write_manual_entries(list(by_id.values()))
    return get_mmp_database_catalog(include_hidden=True, include_stats=True)


def patch_mmp_database(
    database_id: str,
    *,
    visible: Optional[bool] = None,
    label: Optional[str] = None,
    description: Optional[str] = None,
    is_default: Optional[bool] = None,
    include_stats: bool = False,
) -> Dict[str, Any]:
    token = str(database_id or "").strip()
    if not token:
        raise ValueError("database_id is required.")
    catalog = get_mmp_database_catalog(include_hidden=True, include_stats=False)
    databases = catalog.get("databases") if isinstance(catalog, dict) else []
    source = None
    for item in databases if isinstance(databases, list) else []:
        if str(item.get("id") or "") == token:
            source = item
            break
    if not isinstance(source, dict):
        raise ValueError(f"MMP database '{token}' not found.")

    manual_entries = _read_manual_entries()
    by_id = {str(item.get("id") or ""): dict(item) for item in manual_entries}
    target = by_id.get(token) or {
        "id": token,
        "label": str(source.get("label") or token),
        "description": str(source.get("description") or ""),
        "visible": bool(source.get("visible", True)),
        "is_default": bool(source.get("is_default", False)),
        "database_url": str(source.get("database_url") or ""),
        "schema": str(source.get("schema") or ""),
        "status": _normalize_status(source.get("status")) or "building",
        "status_message": str(source.get("status_message") or "").strip(),
        "status_updated_at": str(source.get("status_updated_at") or "").strip(),
        "status_token": str(source.get("status_token") or "").strip(),
    }
    if visible is not None:
        target["visible"] = bool(visible)
    if label is not None:
        target["label"] = str(label).strip()
    if description is not None:
        target["description"] = str(description).strip()
    if is_default is not None:
        target["is_default"] = bool(is_default)
    by_id[token] = target

    entries = list(by_id.values())
    if is_default:
        for item in entries:
            item["is_default"] = str(item.get("id") or "") == token
    _write_manual_entries(entries)
    return get_mmp_database_catalog(include_hidden=True, include_stats=include_stats)


def delete_mmp_database(database_id: str, *, drop_data: bool = True, include_stats: bool = False) -> Dict[str, Any]:
    token = str(database_id or "").strip()
    if not token:
        raise ValueError("database_id is required.")
    selected = resolve_mmp_database(token, include_hidden=True)
    backend = str(selected.get("backend") or "").strip().lower()
    if backend != "postgres":
        raise ValueError("Only PostgreSQL MMP databases are supported.")
    schema = str(selected.get("schema") or "").strip()
    database_url = str(selected.get("database_url") or "").strip()
    if not schema or not _PG_SCHEMA_TOKEN_RE.match(schema):
        raise ValueError(f"Invalid schema for database '{token}'.")
    if schema == "public":
        raise ValueError("Refusing to delete schema 'public'. Create dedicated schemas and delete those instead.")

    if drop_data:
        if psycopg is None:
            raise ValueError("psycopg is required to delete PostgreSQL schema.")
        conn = psycopg.connect(database_url, autocommit=True)
        try:
            with conn.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            conn.close()

    manual_entries = _read_manual_entries()
    filtered_entries = [item for item in manual_entries if str(item.get("id") or "") != token]
    _write_manual_entries(filtered_entries)
    return get_mmp_database_catalog(include_hidden=True, include_stats=include_stats)
