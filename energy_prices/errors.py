"""Errors that callers distinguish between."""

from __future__ import annotations


class SourceUnavailable(RuntimeError):
    """An upstream service could not be reached, or is failing on its side.

    Raised for connection errors, 5xx responses and exhausted rate-limit retries —
    conditions that say nothing about whether this code is correct and that usually
    clear on their own. A bad request or a rejected credential is not this: those are
    our problem and are raised as ordinary errors so they fail loudly.
    """
