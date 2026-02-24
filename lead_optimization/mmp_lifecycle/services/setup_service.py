from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Tuple

from lead_optimization.mmp_lifecycle import engine as legacy

from ..models import PostgresTarget


def _run_with_legacy_error_capture(fn: Callable[[], bool]) -> Tuple[bool, str]:
    logger = logging.getLogger(getattr(legacy, "__name__", __name__))
    thread_id = threading.get_ident()
    errors: list[str] = []

    class _ThreadErrorHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.thread != thread_id:
                return
            if record.levelno < logging.ERROR:
                return
            message = str(record.getMessage() or "").strip()
            if message:
                errors.append(message)

    handler = _ThreadErrorHandler(level=logging.ERROR)
    logger.addHandler(handler)
    try:
        ok = bool(fn())
    finally:
        logger.removeHandler(handler)
    return ok, (errors[-1] if errors else "")


def _supports_auto_cache_reseed(error_message: str) -> bool:
    token = str(error_message or "").lower()
    if not token:
        return False
    fallback_markers = (
        "failed ensuring fragment cache",
        "requires existing fragment cache",
        "fragment cache file is missing",
        "fragment cache metadata mismatch",
        "strict incremental mode forbids implicit full-cache rebuild",
        "strict incremental mode is enabled",
    )
    return any(marker in token for marker in fallback_markers)


def build_database_from_smiles(
    target: PostgresTarget,
    *,
    smiles_file: str,
    output_dir: str,
    max_heavy_atoms: int,
    properties_file: str = "",
    property_metadata_file: str = "",
    skip_attachment_enrichment: bool = False,
    attachment_force_recompute: bool = False,
    force_rebuild_schema: bool = False,
    keep_fragdb: bool = False,
    fragment_jobs: int = 0,
    index_maintenance_work_mem_mb: int = 0,
    index_work_mem_mb: int = 0,
    index_parallel_workers: int = 0,
    index_commit_every_flushes: int = 0,
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
) -> bool:
    ok = legacy.create_mmp_database(
        smiles_file,
        target.url,
        max_heavy_atoms,
        output_dir=output_dir,
        postgres_schema=target.schema,
        properties_file=properties_file or None,
        skip_attachment_enrichment=skip_attachment_enrichment,
        attachment_force_recompute=attachment_force_recompute,
        force_rebuild_schema=force_rebuild_schema,
        keep_fragments=keep_fragdb,
        fragment_jobs=fragment_jobs,
        index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
        index_work_mem_mb=index_work_mem_mb,
        index_parallel_workers=index_parallel_workers,
        index_commit_every_flushes=index_commit_every_flushes,
        build_construct_tables=build_construct_tables,
        build_constant_smiles_mol_index=build_constant_smiles_mol_index,
    )
    if ok and property_metadata_file:
        return legacy.apply_property_metadata_postgres(target.url, target.schema, property_metadata_file)
    return ok


def index_fragdb_into_database(
    target: PostgresTarget,
    *,
    fragments_file: str,
    force_rebuild_schema: bool = False,
    properties_file: str = "",
    property_metadata_file: str = "",
    skip_attachment_enrichment: bool = False,
    attachment_force_recompute: bool = False,
    index_maintenance_work_mem_mb: int = 0,
    index_work_mem_mb: int = 0,
    index_parallel_workers: int = 0,
    index_commit_every_flushes: int = 0,
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
) -> bool:
    ok = legacy._index_fragments_to_postgres(
        fragments_file,
        target.url,
        postgres_schema=target.schema,
        force_rebuild_schema=force_rebuild_schema,
        properties_file=properties_file or None,
        skip_attachment_enrichment=skip_attachment_enrichment,
        attachment_force_recompute=attachment_force_recompute,
        index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
        index_work_mem_mb=index_work_mem_mb,
        index_parallel_workers=index_parallel_workers,
        index_commit_every_flushes=index_commit_every_flushes,
        build_construct_tables=build_construct_tables,
        build_constant_smiles_mol_index=build_constant_smiles_mol_index,
    )
    if ok and property_metadata_file:
        return legacy.apply_property_metadata_postgres(target.url, target.schema, property_metadata_file)
    return ok


def import_property_batch(
    target: PostgresTarget,
    *,
    property_file: str,
    batch_id: str = "",
    batch_label: str = "",
    batch_notes: str = "",
    smiles_column: str = "",
    canonicalize_smiles: bool = True,
    overwrite_existing_batch: bool = False,
) -> bool:
    return legacy.import_property_batch_postgres(
        target.url,
        schema=target.schema,
        property_file=property_file,
        batch_id=batch_id,
        batch_label=batch_label,
        batch_notes=batch_notes,
        smiles_column=smiles_column,
        canonicalize_smiles=canonicalize_smiles,
        overwrite_existing_batch=overwrite_existing_batch,
    )


def delete_property_batch(target: PostgresTarget, *, batch_id: str) -> bool:
    return legacy.delete_property_batch_postgres(target.url, schema=target.schema, batch_id=batch_id)


def list_property_batches(target: PostgresTarget) -> bool:
    return legacy.list_property_batches_postgres(target.url, schema=target.schema)


def import_compound_batch(
    target: PostgresTarget,
    *,
    structures_file: str,
    batch_id: str = "",
    batch_label: str = "",
    batch_notes: str = "",
    smiles_column: str = "",
    id_column: str = "",
    canonicalize_smiles: bool = True,
    output_dir: str = "data",
    max_heavy_atoms: int = 50,
    skip_attachment_enrichment: bool = False,
    attachment_force_recompute: bool = False,
    fragment_jobs: int = 8,
    index_maintenance_work_mem_mb: int = 1024,
    index_work_mem_mb: int = 128,
    index_parallel_workers: int = 4,
    index_commit_every_flushes: int = 0,
    incremental_index_shards: int = 1,
    incremental_index_jobs: int = 1,
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
    skip_incremental_analyze: bool = False,
    overwrite_existing_batch: bool = False,
) -> bool:
    ok, _ = import_compound_batch_with_diagnostics(
        target,
        structures_file=structures_file,
        batch_id=batch_id,
        batch_label=batch_label,
        batch_notes=batch_notes,
        smiles_column=smiles_column,
        id_column=id_column,
        canonicalize_smiles=canonicalize_smiles,
        output_dir=output_dir,
        max_heavy_atoms=max_heavy_atoms,
        skip_attachment_enrichment=skip_attachment_enrichment,
        attachment_force_recompute=attachment_force_recompute,
        fragment_jobs=fragment_jobs,
        index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
        index_work_mem_mb=index_work_mem_mb,
        index_parallel_workers=index_parallel_workers,
        index_commit_every_flushes=index_commit_every_flushes,
        incremental_index_shards=incremental_index_shards,
        incremental_index_jobs=incremental_index_jobs,
        build_construct_tables=build_construct_tables,
        build_constant_smiles_mol_index=build_constant_smiles_mol_index,
        skip_incremental_analyze=skip_incremental_analyze,
        overwrite_existing_batch=overwrite_existing_batch,
    )
    return ok


def import_compound_batch_with_diagnostics(
    target: PostgresTarget,
    *,
    structures_file: str,
    batch_id: str = "",
    batch_label: str = "",
    batch_notes: str = "",
    smiles_column: str = "",
    id_column: str = "",
    canonicalize_smiles: bool = True,
    output_dir: str = "data",
    max_heavy_atoms: int = 50,
    skip_attachment_enrichment: bool = False,
    attachment_force_recompute: bool = False,
    fragment_jobs: int = 8,
    index_maintenance_work_mem_mb: int = 1024,
    index_work_mem_mb: int = 128,
    index_parallel_workers: int = 4,
    index_commit_every_flushes: int = 0,
    incremental_index_shards: int = 1,
    incremental_index_jobs: int = 1,
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
    skip_incremental_analyze: bool = False,
    overwrite_existing_batch: bool = False,
) -> Tuple[bool, str]:
    def _invoke_import() -> bool:
        return legacy.import_compound_batch_postgres(
            target.url,
            schema=target.schema,
            structures_file=structures_file,
            batch_id=batch_id,
            batch_label=batch_label,
            batch_notes=batch_notes,
            smiles_column=smiles_column,
            id_column=id_column,
            canonicalize_smiles=canonicalize_smiles,
            output_dir=output_dir,
            max_heavy_atoms=max_heavy_atoms,
            skip_attachment_enrichment=skip_attachment_enrichment,
            attachment_force_recompute=attachment_force_recompute,
            fragment_jobs=fragment_jobs,
            index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
            index_work_mem_mb=index_work_mem_mb,
            index_parallel_workers=index_parallel_workers,
            index_commit_every_flushes=index_commit_every_flushes,
            incremental_index_shards=incremental_index_shards,
            incremental_index_jobs=incremental_index_jobs,
            build_construct_tables=build_construct_tables,
            build_constant_smiles_mol_index=build_constant_smiles_mol_index,
            skip_incremental_analyze=skip_incremental_analyze,
            overwrite_existing_batch=overwrite_existing_batch,
        )

    ok, error_message = _run_with_legacy_error_capture(_invoke_import)
    if ok:
        return True, ""
    if not _supports_auto_cache_reseed(error_message):
        return False, error_message

    env_key = "LEADOPT_MMP_ALLOW_CACHE_RESEED"
    previous_value = os.environ.get(env_key)
    os.environ[env_key] = "1"
    try:
        retry_ok, retry_error = _run_with_legacy_error_capture(_invoke_import)
    finally:
        if previous_value is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = previous_value
    if retry_ok:
        return True, ""
    if retry_error:
        return False, retry_error
    return False, error_message


def get_compound_import_failure_diagnostic(
    target: PostgresTarget,
    *,
    structures_file: str,
    output_dir: str = "data",
) -> str:
    schema = str(target.schema or "").strip()
    if not schema:
        return ""
    source = str(structures_file or "").strip()
    if source and not os.path.exists(source):
        return f"Compound file not found: {source}"
    cache_file = os.path.join(output_dir, f"{schema}_compound_state.cache.fragdb")
    if not os.path.exists(cache_file):
        return (
            f"Missing incremental fragment cache for schema '{schema}': {cache_file}. "
            "Auto reseed was attempted; if it still failed, check server logs for fragment/index build errors."
        )
    return ""


def delete_compound_batch(
    target: PostgresTarget,
    *,
    batch_id: str,
    output_dir: str = "data",
    max_heavy_atoms: int = 50,
    skip_attachment_enrichment: bool = False,
    attachment_force_recompute: bool = False,
    fragment_jobs: int = 8,
    index_maintenance_work_mem_mb: int = 1024,
    index_work_mem_mb: int = 128,
    index_parallel_workers: int = 4,
    index_commit_every_flushes: int = 0,
    incremental_index_shards: int = 1,
    incremental_index_jobs: int = 1,
    build_construct_tables: bool = True,
    build_constant_smiles_mol_index: bool = True,
    skip_incremental_analyze: bool = False,
) -> bool:
    return legacy.delete_compound_batch_postgres(
        target.url,
        schema=target.schema,
        batch_id=batch_id,
        output_dir=output_dir,
        max_heavy_atoms=max_heavy_atoms,
        skip_attachment_enrichment=skip_attachment_enrichment,
        attachment_force_recompute=attachment_force_recompute,
        fragment_jobs=fragment_jobs,
        index_maintenance_work_mem_mb=index_maintenance_work_mem_mb,
        index_work_mem_mb=index_work_mem_mb,
        index_parallel_workers=index_parallel_workers,
        index_commit_every_flushes=index_commit_every_flushes,
        incremental_index_shards=incremental_index_shards,
        incremental_index_jobs=incremental_index_jobs,
        build_construct_tables=build_construct_tables,
        build_constant_smiles_mol_index=build_constant_smiles_mol_index,
        skip_incremental_analyze=skip_incremental_analyze,
    )


def list_compound_batches(target: PostgresTarget) -> bool:
    return legacy.list_compound_batches_postgres(target.url, schema=target.schema)


def apply_property_metadata(target: PostgresTarget, *, metadata_file: str) -> bool:
    return legacy.apply_property_metadata_postgres(target.url, target.schema, metadata_file)
