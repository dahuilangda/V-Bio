"""Lead Optimization MMP lifecycle toolkit.

This package provides an engineered command layer over MMP PostgreSQL data
lifecycle operations: build, incremental compound/property updates, registry,
and verification utilities.
"""

from .models import PostgresTarget

__all__ = [
    "PostgresTarget",
]
