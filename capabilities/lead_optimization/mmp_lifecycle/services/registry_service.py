from __future__ import annotations

from typing import Any, Dict

from capabilities.lead_optimization import mmp_database_registry as registry


def list_catalog(*, include_hidden: bool = False, include_stats: bool = True) -> Dict[str, Any]:
    return registry.get_mmp_database_catalog(include_hidden=include_hidden, include_stats=include_stats)


def set_database_flags(
    database_id: str,
    *,
    visible: bool | None = None,
    label: str | None = None,
    description: str | None = None,
    is_default: bool | None = None,
    include_stats: bool = True,
) -> Dict[str, Any]:
    return registry.patch_mmp_database(
        database_id,
        visible=visible,
        label=label,
        description=description,
        is_default=is_default,
        include_stats=include_stats,
    )


def upsert_database(
    *,
    database_id: str = "",
    database_url: str = "",
    schema: str = "",
    label: str | None = None,
    description: str | None = None,
    visible: bool | None = None,
    is_default: bool | None = None,
    status: str | None = None,
    status_message: str | None = None,
    include_stats: bool = True,
) -> Dict[str, Any]:
    return registry.upsert_mmp_database(
        database_id=database_id,
        database_url=database_url,
        schema=schema,
        label=label,
        description=description,
        visible=visible,
        is_default=is_default,
        status=status,
        status_message=status_message,
        include_stats=include_stats,
    )


def delete_database(database_id: str, *, drop_data: bool = False, include_stats: bool = True) -> Dict[str, Any]:
    return registry.delete_mmp_database(database_id, drop_data=drop_data, include_stats=include_stats)
