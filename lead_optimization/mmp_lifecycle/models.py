from __future__ import annotations

import os
import re
from dataclasses import dataclass


_PG_DSN_PREFIXES = ("postgres://", "postgresql://")
_PG_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_postgres_dsn(value: str) -> bool:
    token = str(value or "").strip().lower()
    return token.startswith(_PG_DSN_PREFIXES)


def normalize_schema(value: str) -> str:
    token = str(value or "").strip() or "public"
    if not _PG_SCHEMA_RE.match(token):
        raise ValueError(f"Invalid PostgreSQL schema: {value}")
    return token


@dataclass(frozen=True)
class PostgresTarget:
    url: str
    schema: str = "public"

    @classmethod
    def from_inputs(cls, url: str = "", schema: str = "") -> "PostgresTarget":
        resolved_url = str(url or os.getenv("LEAD_OPT_MMP_DB_URL", "")).strip()
        resolved_schema = str(schema or os.getenv("LEAD_OPT_MMP_DB_SCHEMA", "public")).strip()
        if not is_postgres_dsn(resolved_url):
            raise ValueError("PostgreSQL DSN is required, e.g. postgresql://user:pass@host:5432/db")
        return cls(url=resolved_url, schema=normalize_schema(resolved_schema))
