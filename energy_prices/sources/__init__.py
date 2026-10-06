"""Price sources. Each module exposes SOURCE and fetch_day_ahead(...) -> pl.DataFrame."""

from __future__ import annotations

from types import ModuleType

from . import energycharts, entsoe

SOURCES: dict[str, ModuleType] = {
    energycharts.SOURCE: energycharts,
    entsoe.SOURCE: entsoe,
}

# ENTSO-E is the origin of the prices rather than a re-publisher, and it states
# the resolution instead of leaving it to be inferred from timestamp spacing.
DEFAULT = entsoe.SOURCE

__all__ = ["SOURCES", "DEFAULT", "energycharts", "entsoe"]
