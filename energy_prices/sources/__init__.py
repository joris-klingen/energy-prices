"""Price sources. Each module exposes SOURCE and fetch_day_ahead(...) -> pl.DataFrame."""

from __future__ import annotations

from types import ModuleType

from . import energycharts, entsoe

SOURCES: dict[str, ModuleType] = {
    energycharts.SOURCE: energycharts,
    entsoe.SOURCE: entsoe,
}

DEFAULT = energycharts.SOURCE

__all__ = ["SOURCES", "DEFAULT", "energycharts", "entsoe"]
