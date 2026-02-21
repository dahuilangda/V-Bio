from __future__ import annotations

from typing import Any, Dict

from lead_optimization import mmp_database_registry as registry


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


def delete_database(database_id: str, *, drop_data: bool = False, include_stats: bool = True) -> Dict[str, Any]:
    return registry.delete_mmp_database(database_id, drop_data=drop_data, include_stats=include_stats)
