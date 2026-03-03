"""Minimal lead optimization runtime configuration.

Only PostgreSQL MMP runtime fields are kept.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml


@dataclass
class MMPDatabaseConfig:
    database_url: str = ""
    database_schema: str = "public"

    def __post_init__(self) -> None:
        self.database_url = str(self.database_url or os.getenv("LEAD_OPT_MMP_DB_URL", "")).strip()
        self.database_schema = str(
            self.database_schema or os.getenv("LEAD_OPT_MMP_DB_SCHEMA", "public")
        ).strip() or "public"


@dataclass
class LeadOptimizationConfig:
    mmp_database: MMPDatabaseConfig = field(default_factory=MMPDatabaseConfig)

    @classmethod
    def from_yaml(cls, config_path: str) -> "LeadOptimizationConfig":
        path = Path(config_path)
        payload: Any = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        data = payload if isinstance(payload, dict) else {}

        mmp_raw = data.get("mmp_database")
        if not isinstance(mmp_raw, dict):
            mmp_raw = {}
        # Backward-compatible top-level keys.
        if "database_url" in data and "database_url" not in mmp_raw:
            mmp_raw["database_url"] = data.get("database_url")
        if "database_schema" in data and "database_schema" not in mmp_raw:
            mmp_raw["database_schema"] = data.get("database_schema")

        return cls(
            mmp_database=MMPDatabaseConfig(
                database_url=str(mmp_raw.get("database_url") or ""),
                database_schema=str(mmp_raw.get("database_schema") or "public"),
            )
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        url = str(self.mmp_database.database_url or "").strip().lower()
        if not url:
            errors.append("MMP PostgreSQL database_url is required (LEAD_OPT_MMP_DB_URL).")
        elif not (url.startswith("postgresql://") or url.startswith("postgres://")):
            errors.append("MMP database_url must be a PostgreSQL DSN.")
        return errors


DEFAULT_CONFIG = LeadOptimizationConfig()


def load_config(config_path: Optional[str] = None) -> LeadOptimizationConfig:
    if config_path and Path(config_path).exists():
        return LeadOptimizationConfig.from_yaml(config_path)
    return DEFAULT_CONFIG
