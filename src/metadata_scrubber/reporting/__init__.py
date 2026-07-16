"""Reporting — génération de rapports d'audit."""

from .duplicates_registry import (
    build_duplicates_registry,
    write_duplicates_registry,
)

__all__ = [
    "build_duplicates_registry",
    "write_duplicates_registry",
]
