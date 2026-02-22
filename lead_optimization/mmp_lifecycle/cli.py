from __future__ import annotations

import argparse
import json
import logging

from .models import PostgresTarget
from .services import check_service, registry_service, report_service, setup_service, template_service, verify_service


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def _build_target(args: argparse.Namespace) -> PostgresTarget:
    return PostgresTarget.from_inputs(url=str(getattr(args, "postgres_url", "") or ""), schema=str(getattr(args, "postgres_schema", "") or ""))


def _print_dataset_stats(prefix: str, stats: verify_service.DatasetStats) -> None:
    print(f"{prefix} compounds={stats.compounds} rules={stats.rules} pairs={stats.pairs} rule_envs={stats.rule_environments}")


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--postgres_url", type=str, default="", help="PostgreSQL DSN")
    parser.add_argument("--postgres_schema", type=str, default="", help="PostgreSQL schema")


def _cmd_db_build(args: argparse.Namespace) -> int:
    target = _build_target(args)
    ok = setup_service.build_database_from_smiles(
        target,
        smiles_file=args.smiles_file,
        output_dir=args.output_dir,
        max_heavy_atoms=args.max_heavy_atoms,
        properties_file=args.properties_file,
        property_metadata_file=args.property_metadata_file,
        skip_attachment_enrichment=args.skip_attachment_enrichment,
        attachment_force_recompute=args.attachment_force_recompute,
        force_rebuild_schema=args.force,
        keep_fragdb=args.keep_fragdb,
        fragment_jobs=args.fragment_jobs,
        index_maintenance_work_mem_mb=args.pg_index_maintenance_work_mem_mb,
        index_work_mem_mb=args.pg_index_work_mem_mb,
        index_parallel_workers=args.pg_index_parallel_workers,
        index_commit_every_flushes=args.pg_index_commit_every_flushes,
        build_construct_tables=not args.pg_skip_construct_tables,
        build_constant_smiles_mol_index=not args.pg_skip_constant_smiles_mol_index,
    )
    if not ok:
        return 1
    stats = verify_service.fetch_dataset_stats(target)
    _print_dataset_stats("build_done", stats)
    return 0


def _cmd_db_index_fragdb(args: argparse.Namespace) -> int:
    target = _build_target(args)
    ok = setup_service.index_fragdb_into_database(
        target,
        fragments_file=args.fragments_file,
        force_rebuild_schema=args.force,
        properties_file=args.properties_file,
        property_metadata_file=args.property_metadata_file,
        skip_attachment_enrichment=args.skip_attachment_enrichment,
        attachment_force_recompute=args.attachment_force_recompute,
        index_maintenance_work_mem_mb=args.pg_index_maintenance_work_mem_mb,
        index_work_mem_mb=args.pg_index_work_mem_mb,
        index_parallel_workers=args.pg_index_parallel_workers,
        index_commit_every_flushes=args.pg_index_commit_every_flushes,
        build_construct_tables=not args.pg_skip_construct_tables,
        build_constant_smiles_mol_index=not args.pg_skip_constant_smiles_mol_index,
    )
    if not ok:
        return 1
    stats = verify_service.fetch_dataset_stats(target)
    _print_dataset_stats("index_done", stats)
    return 0


def _cmd_property_import(args: argparse.Namespace) -> int:
    target = _build_target(args)
    ok = setup_service.import_property_batch(
        target,
        property_file=args.file,
        batch_id=args.batch_id,
        batch_label=args.batch_label,
        batch_notes=args.batch_notes,
        smiles_column=args.smiles_column,
        canonicalize_smiles=not args.no_canonicalize,
    )
    return 0 if ok else 1


def _cmd_property_delete(args: argparse.Namespace) -> int:
    target = _build_target(args)
    ok = setup_service.delete_property_batch(target, batch_id=args.batch_id)
    return 0 if ok else 1


def _cmd_property_list(args: argparse.Namespace) -> int:
    target = _build_target(args)
    ok = setup_service.list_property_batches(target)
    return 0 if ok else 1


def _cmd_compound_import(args: argparse.Namespace) -> int:
    target = _build_target(args)
    before = verify_service.fetch_dataset_stats(target)
    ok = setup_service.import_compound_batch(
        target,
        structures_file=args.file,
        batch_id=args.batch_id,
        batch_label=args.batch_label,
        batch_notes=args.batch_notes,
        smiles_column=args.smiles_column,
        id_column=args.id_column,
        canonicalize_smiles=not args.no_canonicalize,
        output_dir=args.output_dir,
        max_heavy_atoms=args.max_heavy_atoms,
        skip_attachment_enrichment=args.skip_attachment_enrichment,
        attachment_force_recompute=args.attachment_force_recompute,
        fragment_jobs=args.fragment_jobs,
        index_maintenance_work_mem_mb=args.pg_index_maintenance_work_mem_mb,
        index_work_mem_mb=args.pg_index_work_mem_mb,
        index_parallel_workers=args.pg_index_parallel_workers,
        index_commit_every_flushes=args.pg_index_commit_every_flushes,
        incremental_index_shards=args.pg_incremental_index_shards,
        incremental_index_jobs=args.pg_incremental_index_jobs,
        build_construct_tables=not args.pg_skip_construct_tables,
        build_constant_smiles_mol_index=not args.pg_skip_constant_smiles_mol_index,
    )
    if not ok:
        return 1
    after = verify_service.fetch_dataset_stats(target)
    _print_dataset_stats("compound_import_before", before)
    _print_dataset_stats("compound_import_after", after)
    print(f"pair_delta={after.pairs - before.pairs}")
    if args.report_batch_pairs and args.batch_id:
        pair_touch = verify_service.count_pairs_touching_compound_batch(target, args.batch_id)
        print(f"batch_pairs_touching={pair_touch}")
    return 0


def _cmd_compound_delete(args: argparse.Namespace) -> int:
    target = _build_target(args)
    before = verify_service.fetch_dataset_stats(target)
    ok = setup_service.delete_compound_batch(
        target,
        batch_id=args.batch_id,
        output_dir=args.output_dir,
        max_heavy_atoms=args.max_heavy_atoms,
        skip_attachment_enrichment=args.skip_attachment_enrichment,
        attachment_force_recompute=args.attachment_force_recompute,
        fragment_jobs=args.fragment_jobs,
        index_maintenance_work_mem_mb=args.pg_index_maintenance_work_mem_mb,
        index_work_mem_mb=args.pg_index_work_mem_mb,
        index_parallel_workers=args.pg_index_parallel_workers,
        index_commit_every_flushes=args.pg_index_commit_every_flushes,
        incremental_index_shards=args.pg_incremental_index_shards,
        incremental_index_jobs=args.pg_incremental_index_jobs,
        build_construct_tables=not args.pg_skip_construct_tables,
        build_constant_smiles_mol_index=not args.pg_skip_constant_smiles_mol_index,
    )
    if not ok:
        return 1
    after = verify_service.fetch_dataset_stats(target)
    _print_dataset_stats("compound_delete_before", before)
    _print_dataset_stats("compound_delete_after", after)
    print(f"pair_delta={after.pairs - before.pairs}")
    return 0


def _cmd_compound_list(args: argparse.Namespace) -> int:
    target = _build_target(args)
    ok = setup_service.list_compound_batches(target)
    return 0 if ok else 1


def _cmd_metadata_apply(args: argparse.Namespace) -> int:
    target = _build_target(args)
    ok = setup_service.apply_property_metadata(target, metadata_file=args.file)
    return 0 if ok else 1


def _cmd_registry_list(args: argparse.Namespace) -> int:
    payload = registry_service.list_catalog(include_hidden=args.include_hidden, include_stats=not args.no_stats)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_registry_set(args: argparse.Namespace) -> int:
    payload = registry_service.set_database_flags(
        args.database_id,
        visible=args.visible,
        label=args.label,
        description=args.description,
        is_default=args.is_default,
        include_stats=not args.no_stats,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_registry_delete(args: argparse.Namespace) -> int:
    payload = registry_service.delete_database(args.database_id, drop_data=args.drop_data, include_stats=not args.no_stats)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_verify_schema(args: argparse.Namespace) -> int:
    target = _build_target(args)
    stats = verify_service.fetch_dataset_stats(target)
    table_counts = verify_service.fetch_core_table_counts(target)
    _print_dataset_stats("dataset", stats)
    print(json.dumps(table_counts, ensure_ascii=False, indent=2))
    return 0


def _cmd_verify_pair_smiles(args: argparse.Namespace) -> int:
    target = _build_target(args)
    pair_count = verify_service.count_pairs_touching_smiles(target, args.smiles)
    print(f"smiles={args.smiles}\npairs_touching={pair_count}")
    return 0


def _cmd_verify_pair_batch(args: argparse.Namespace) -> int:
    target = _build_target(args)
    pair_count = verify_service.count_pairs_touching_compound_batch(target, args.batch_id)
    print(f"batch_id={args.batch_id}\npairs_touching={pair_count}")
    return 0


def _cmd_template_compound(args: argparse.Namespace) -> int:
    path = template_service.write_compound_batch_template(args.output, rows=args.rows)
    print(path)
    return 0


def _cmd_template_property(args: argparse.Namespace) -> int:
    properties = [token.strip() for token in str(args.properties or "").split(",") if token.strip()]
    path = template_service.write_property_batch_template(
        args.output,
        property_names=properties,
        rows=args.rows,
    )
    print(path)
    return 0


def _cmd_report_metrics(args: argparse.Namespace) -> int:
    target = _build_target(args)
    payload = report_service.fetch_schema_metrics(target, recent_limit=args.recent_limit)
    if args.output_json:
        path = report_service.write_metrics_json(args.output_json, payload)
        print(path)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _print_action_preview_rows(rows: list[dict[str, object]], *, columns: list[str], limit: int) -> None:
    if not rows:
        print("(empty)")
        return
    preview_limit = max(1, int(limit))
    shown = rows[:preview_limit]
    print("\\t".join(columns))
    for row in shown:
        print(
            "\\t".join(
                "1"
                if row.get(col) is True
                else "0"
                if row.get(col) is False
                else ""
                if row.get(col) is None
                else str(row.get(col))
                for col in columns
            )
        )
    if len(rows) > preview_limit:
        print(f"... ({len(rows) - preview_limit} more rows omitted)")


def _cmd_check_compound_import(args: argparse.Namespace) -> int:
    target = _build_target(args)
    payload = check_service.preview_compound_import(
        target,
        structures_file=args.file,
        smiles_column=args.smiles_column,
        id_column=args.id_column,
        canonicalize_smiles=not args.no_canonicalize,
    )
    print(json.dumps(payload.get("summary", {}), ensure_ascii=False, indent=2))
    columns = list(payload.get("columns", []))
    rows = list(payload.get("rows", []))
    _print_action_preview_rows(rows, columns=columns, limit=args.print_limit)
    if args.output_tsv:
        path = check_service.write_annotated_table_tsv(args.output_tsv, columns, rows)
        print(path)
    return 0


def _cmd_check_property_import(args: argparse.Namespace) -> int:
    target = _build_target(args)
    payload = check_service.preview_property_import(
        target,
        property_file=args.file,
        smiles_column=args.smiles_column,
        canonicalize_smiles=not args.no_canonicalize,
    )
    print(json.dumps(payload.get("summary", {}), ensure_ascii=False, indent=2))
    columns = list(payload.get("columns", []))
    rows = list(payload.get("rows", []))
    _print_action_preview_rows(rows, columns=columns, limit=args.print_limit)
    if args.output_tsv:
        path = check_service.write_annotated_table_tsv(args.output_tsv, columns, rows)
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leadopt-mmp",
        description="Lead Optimization MMP lifecycle toolkit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("db-build", help="Create new MMP database from SMILES")
    _add_target_args(p_build)
    p_build.add_argument("--smiles_file", required=True, type=str)
    p_build.add_argument("--output_dir", default="lead_optimization/data", type=str)
    p_build.add_argument("--max_heavy_atoms", default=50, type=int)
    p_build.add_argument("--properties_file", default="", type=str)
    p_build.add_argument("--property_metadata_file", default="", type=str)
    p_build.add_argument("--skip_attachment_enrichment", action="store_true")
    p_build.add_argument("--attachment_force_recompute", action="store_true")
    p_build.add_argument("--force", action="store_true")
    p_build.add_argument("--keep_fragdb", action="store_true")
    p_build.add_argument("--fragment_jobs", default=8, type=int)
    p_build.add_argument("--pg_index_maintenance_work_mem_mb", default=1024, type=int)
    p_build.add_argument("--pg_index_work_mem_mb", default=128, type=int)
    p_build.add_argument("--pg_index_parallel_workers", default=4, type=int)
    p_build.add_argument(
        "--pg_index_commit_every_flushes",
        default=0,
        type=int,
        help="Commit cadence for mmpdb index flushes (<=0: auto-adaptive).",
    )
    p_build.add_argument("--pg_skip_construct_tables", action="store_true")
    p_build.add_argument("--pg_skip_constant_smiles_mol_index", action="store_true")
    p_build.set_defaults(func=_cmd_db_build)

    p_index = sub.add_parser("db-index-fragdb", help="Index existing .fragdb into schema")
    _add_target_args(p_index)
    p_index.add_argument("--fragments_file", required=True, type=str)
    p_index.add_argument("--properties_file", default="", type=str)
    p_index.add_argument("--property_metadata_file", default="", type=str)
    p_index.add_argument("--skip_attachment_enrichment", action="store_true")
    p_index.add_argument("--attachment_force_recompute", action="store_true")
    p_index.add_argument("--force", action="store_true")
    p_index.add_argument("--pg_index_maintenance_work_mem_mb", default=1024, type=int)
    p_index.add_argument("--pg_index_work_mem_mb", default=128, type=int)
    p_index.add_argument("--pg_index_parallel_workers", default=4, type=int)
    p_index.add_argument(
        "--pg_index_commit_every_flushes",
        default=0,
        type=int,
        help="Commit cadence for mmpdb index flushes (<=0: auto-adaptive).",
    )
    p_index.add_argument("--pg_skip_construct_tables", action="store_true")
    p_index.add_argument("--pg_skip_constant_smiles_mol_index", action="store_true")
    p_index.set_defaults(func=_cmd_db_index_fragdb)

    p_prop_import = sub.add_parser("property-import", help="Import incremental property batch")
    _add_target_args(p_prop_import)
    p_prop_import.add_argument("--file", required=True, type=str)
    p_prop_import.add_argument("--batch_id", default="", type=str)
    p_prop_import.add_argument("--batch_label", default="", type=str)
    p_prop_import.add_argument("--batch_notes", default="", type=str)
    p_prop_import.add_argument("--smiles_column", default="", type=str)
    p_prop_import.add_argument("--no_canonicalize", action="store_true")
    p_prop_import.set_defaults(func=_cmd_property_import)

    p_prop_delete = sub.add_parser("property-delete", help="Delete incremental property batch")
    _add_target_args(p_prop_delete)
    p_prop_delete.add_argument("--batch_id", required=True, type=str)
    p_prop_delete.set_defaults(func=_cmd_property_delete)

    p_prop_list = sub.add_parser("property-list", help="List property batches")
    _add_target_args(p_prop_list)
    p_prop_list.set_defaults(func=_cmd_property_list)

    p_cmp_import = sub.add_parser("compound-import", help="Import incremental compound batch")
    _add_target_args(p_cmp_import)
    p_cmp_import.add_argument("--file", required=True, type=str)
    p_cmp_import.add_argument("--batch_id", default="", type=str)
    p_cmp_import.add_argument("--batch_label", default="", type=str)
    p_cmp_import.add_argument("--batch_notes", default="", type=str)
    p_cmp_import.add_argument("--smiles_column", default="", type=str)
    p_cmp_import.add_argument("--id_column", default="", type=str)
    p_cmp_import.add_argument("--no_canonicalize", action="store_true")
    p_cmp_import.add_argument("--output_dir", default="lead_optimization/data", type=str)
    p_cmp_import.add_argument("--max_heavy_atoms", default=50, type=int)
    p_cmp_import.add_argument("--skip_attachment_enrichment", action="store_true")
    p_cmp_import.add_argument("--attachment_force_recompute", action="store_true")
    p_cmp_import.add_argument("--fragment_jobs", default=8, type=int)
    p_cmp_import.add_argument("--pg_index_maintenance_work_mem_mb", default=1024, type=int)
    p_cmp_import.add_argument("--pg_index_work_mem_mb", default=128, type=int)
    p_cmp_import.add_argument("--pg_index_parallel_workers", default=4, type=int)
    p_cmp_import.add_argument(
        "--pg_index_commit_every_flushes",
        default=0,
        type=int,
        help="Commit cadence for mmpdb index flushes (<=0: auto-adaptive).",
    )
    p_cmp_import.add_argument("--pg_incremental_index_shards", default=1, type=int)
    p_cmp_import.add_argument("--pg_incremental_index_jobs", default=1, type=int)
    p_cmp_import.add_argument("--pg_skip_construct_tables", action="store_true")
    p_cmp_import.add_argument("--pg_skip_constant_smiles_mol_index", action="store_true")
    p_cmp_import.add_argument("--report_batch_pairs", action="store_true")
    p_cmp_import.set_defaults(func=_cmd_compound_import)

    p_cmp_delete = sub.add_parser("compound-delete", help="Delete incremental compound batch")
    _add_target_args(p_cmp_delete)
    p_cmp_delete.add_argument("--batch_id", required=True, type=str)
    p_cmp_delete.add_argument("--output_dir", default="lead_optimization/data", type=str)
    p_cmp_delete.add_argument("--max_heavy_atoms", default=50, type=int)
    p_cmp_delete.add_argument("--skip_attachment_enrichment", action="store_true")
    p_cmp_delete.add_argument("--attachment_force_recompute", action="store_true")
    p_cmp_delete.add_argument("--fragment_jobs", default=8, type=int)
    p_cmp_delete.add_argument("--pg_index_maintenance_work_mem_mb", default=1024, type=int)
    p_cmp_delete.add_argument("--pg_index_work_mem_mb", default=128, type=int)
    p_cmp_delete.add_argument("--pg_index_parallel_workers", default=4, type=int)
    p_cmp_delete.add_argument(
        "--pg_index_commit_every_flushes",
        default=0,
        type=int,
        help="Commit cadence for mmpdb index flushes (<=0: auto-adaptive).",
    )
    p_cmp_delete.add_argument("--pg_incremental_index_shards", default=1, type=int)
    p_cmp_delete.add_argument("--pg_incremental_index_jobs", default=1, type=int)
    p_cmp_delete.add_argument("--pg_skip_construct_tables", action="store_true")
    p_cmp_delete.add_argument("--pg_skip_constant_smiles_mol_index", action="store_true")
    p_cmp_delete.set_defaults(func=_cmd_compound_delete)

    p_cmp_list = sub.add_parser("compound-list", help="List compound batches")
    _add_target_args(p_cmp_list)
    p_cmp_list.set_defaults(func=_cmd_compound_list)

    p_meta = sub.add_parser("metadata-apply", help="Apply property metadata CSV")
    _add_target_args(p_meta)
    p_meta.add_argument("--file", required=True, type=str)
    p_meta.set_defaults(func=_cmd_metadata_apply)

    p_reg_list = sub.add_parser("registry-list", help="List DB registry entries")
    p_reg_list.add_argument("--include_hidden", action="store_true")
    p_reg_list.add_argument("--no_stats", action="store_true")
    p_reg_list.set_defaults(func=_cmd_registry_list)

    p_reg_set = sub.add_parser("registry-set", help="Patch DB registry entry")
    p_reg_set.add_argument("--database_id", required=True, type=str)
    p_reg_set.add_argument("--visible", type=lambda x: str(x).lower() in {"1", "true", "yes", "y"}, default=None)
    p_reg_set.add_argument("--label", type=str, default=None)
    p_reg_set.add_argument("--description", type=str, default=None)
    p_reg_set.add_argument("--is_default", type=lambda x: str(x).lower() in {"1", "true", "yes", "y"}, default=None)
    p_reg_set.add_argument("--no_stats", action="store_true")
    p_reg_set.set_defaults(func=_cmd_registry_set)

    p_reg_del = sub.add_parser("registry-delete", help="Delete registry entry and optional schema")
    p_reg_del.add_argument("--database_id", required=True, type=str)
    p_reg_del.add_argument("--drop_data", action="store_true", help="Drop target schema CASCADE")
    p_reg_del.add_argument("--no_stats", action="store_true")
    p_reg_del.set_defaults(func=_cmd_registry_delete)

    p_verify_schema = sub.add_parser("verify-schema", help="Verify schema dataset/core table counts")
    _add_target_args(p_verify_schema)
    p_verify_schema.set_defaults(func=_cmd_verify_schema)

    p_verify_smiles = sub.add_parser("verify-pair-smiles", help="Count pair rows touching one clean_smiles")
    _add_target_args(p_verify_smiles)
    p_verify_smiles.add_argument("--smiles", required=True, type=str)
    p_verify_smiles.set_defaults(func=_cmd_verify_pair_smiles)

    p_verify_batch = sub.add_parser("verify-pair-batch", help="Count pair rows touching one compound batch")
    _add_target_args(p_verify_batch)
    p_verify_batch.add_argument("--batch_id", required=True, type=str)
    p_verify_batch.set_defaults(func=_cmd_verify_pair_batch)

    p_tpl_compound = sub.add_parser("template-compound", help="Write compound batch TSV template")
    p_tpl_compound.add_argument("--output", required=True, type=str)
    p_tpl_compound.add_argument("--rows", default=3, type=int)
    p_tpl_compound.set_defaults(func=_cmd_template_compound)

    p_tpl_property = sub.add_parser("template-property", help="Write property batch TSV template")
    p_tpl_property.add_argument("--output", required=True, type=str)
    p_tpl_property.add_argument("--properties", default="PROPERTY_A,PROPERTY_B", type=str)
    p_tpl_property.add_argument("--rows", default=3, type=int)
    p_tpl_property.set_defaults(func=_cmd_template_property)

    p_metrics = sub.add_parser("report-metrics", help="Collect schema metrics and recent batch summaries")
    _add_target_args(p_metrics)
    p_metrics.add_argument("--recent_limit", default=10, type=int)
    p_metrics.add_argument("--output_json", default="", type=str)
    p_metrics.set_defaults(func=_cmd_report_metrics)

    p_check_cmp = sub.add_parser(
        "check-compound-import",
        help="Dry-run check for compound batch import and annotate planned row operations",
    )
    _add_target_args(p_check_cmp)
    p_check_cmp.add_argument("--file", required=True, type=str)
    p_check_cmp.add_argument("--smiles_column", default="", type=str)
    p_check_cmp.add_argument("--id_column", default="", type=str)
    p_check_cmp.add_argument("--no_canonicalize", action="store_true")
    p_check_cmp.add_argument("--print_limit", default=80, type=int)
    p_check_cmp.add_argument("--output_tsv", default="", type=str)
    p_check_cmp.set_defaults(func=_cmd_check_compound_import)

    p_check_prop = sub.add_parser(
        "check-property-import",
        help="Dry-run check for property batch import and annotate planned row operations",
    )
    _add_target_args(p_check_prop)
    p_check_prop.add_argument("--file", required=True, type=str)
    p_check_prop.add_argument("--smiles_column", default="", type=str)
    p_check_prop.add_argument("--no_canonicalize", action="store_true")
    p_check_prop.add_argument("--print_limit", default=80, type=int)
    p_check_prop.add_argument("--output_tsv", default="", type=str)
    p_check_prop.set_defaults(func=_cmd_check_property_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        logging.getLogger(__name__).error("Command failed: %s", exc, exc_info=True)
        return 1
