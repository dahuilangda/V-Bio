from __future__ import annotations

import argparse
import hashlib
import logging
import os
from pathlib import Path
from typing import Iterable

import psycopg2


LOGGER = logging.getLogger(__name__)
DEFAULT_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "supabase-lite"
    / "init"
    / "migrations"
)
MIGRATION_LOCK_NAME = "vbio_schema_migrations"


def _migration_files(directory: Path) -> Iterable[Path]:
    if not directory.is_dir():
        raise RuntimeError(f"PostgreSQL migration directory does not exist: {directory}")
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix == ".sql")
    if not files:
        raise RuntimeError(f"PostgreSQL migration directory contains no SQL files: {directory}")
    return files


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migrations(database_url: str, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR) -> list[str]:
    normalized_url = str(database_url or "").strip()
    if not normalized_url:
        raise RuntimeError("VBIO_MONITOR_MIGRATION_DATABASE_URL is required for PostgreSQL monitor migrations")

    connect_timeout = max(1, int(os.environ.get("VBIO_MONITOR_DB_CONNECT_TIMEOUT_SECONDS", "10")))
    connection = psycopg2.connect(normalized_url, connect_timeout=connect_timeout, application_name="vbio-migrations")
    applied: list[str] = []
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_lock(hashtext(%s))", (MIGRATION_LOCK_NAME,))
        connection.autocommit = False
        with connection.cursor() as cursor:
            cursor.execute(
                """
                create table if not exists public.vbio_schema_migrations (
                  version text primary key,
                  checksum text not null,
                  applied_at timestamptz not null default now()
                )
                """
            )
        connection.commit()

        for path in _migration_files(migrations_dir):
            version = path.stem
            checksum = _checksum(path)
            with connection.cursor() as cursor:
                cursor.execute(
                    "select checksum from public.vbio_schema_migrations where version = %s",
                    (version,),
                )
                existing = cursor.fetchone()
                if existing:
                    if str(existing[0]) != checksum:
                        raise RuntimeError(
                            f"PostgreSQL migration checksum mismatch for {version}: "
                            f"database={existing[0]} file={checksum}"
                        )
                    connection.rollback()
                    continue

                LOGGER.info("Applying PostgreSQL migration %s", version)
                cursor.execute(path.read_text(encoding="utf-8"))
                cursor.execute(
                    "insert into public.vbio_schema_migrations (version, checksum) values (%s, %s)",
                    (version, checksum),
                )
            connection.commit()
            applied.append(version)
    except Exception:
        connection.rollback()
        raise
    finally:
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("select pg_advisory_unlock(hashtext(%s))", (MIGRATION_LOCK_NAME,))
        finally:
            connection.close()
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply V-Bio PostgreSQL schema migrations")
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=DEFAULT_MIGRATIONS_DIR,
        help="Directory containing ordered .sql migration files",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    database_url = os.environ.get("VBIO_MONITOR_MIGRATION_DATABASE_URL", "")
    applied = apply_migrations(database_url, args.migrations_dir)
    if applied:
        LOGGER.info("Applied PostgreSQL migrations: %s", ", ".join(applied))
    else:
        LOGGER.info("PostgreSQL schema is current")


if __name__ == "__main__":
    main()
