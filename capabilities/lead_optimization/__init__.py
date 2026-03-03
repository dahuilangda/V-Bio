"""Lead optimization runtime package (V-Bio active subset).

This package now contains PostgreSQL-based MMP runtime utilities only.
Legacy optimization-engine modules are removed from the active surface.
"""

__all__ = [
    "config",
    "mmp_lifecycle",
    "mmp_database_registry",
    "mmp_query_service",
]
