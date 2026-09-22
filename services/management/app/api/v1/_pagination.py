"""Shared pagination for management API list endpoints (audit-2026-09-14).

Every list handler previously ran ``db(query).select()`` with no bound, so a
caller (or a large tenant) could force an unbounded result set into memory --
the DoS/resource finding from the security audit. This module gives those
handlers one consistent, bounded ``limitby`` derived from ``?page=&limit=``
query parameters, with a hard ceiling that a caller cannot exceed.

Usage in a handler::

    from ._pagination import PageRequest

    page = PageRequest.from_request()          # reads request.args, clamps
    rows = db(query).select(limitby=page.limitby, orderby=db.table.id)
    total = db(query).count()
    return jsonify({"data": [...], "count": len(rows), **page.meta(total)})

The ceiling is deliberately generous (``MAX_PAGE_SIZE``) so existing callers
that expected "all rows" keep working for any realistic tenant, while a
pathological request is still bounded. ``orderby`` is the caller's
responsibility -- ``limitby`` without a stable order returns arbitrary rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from quart import request

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000


def _clamp_int(raw: object, *, default: int, minimum: int, maximum: int) -> int:
    """Coerce *raw* to an int in ``[minimum, maximum]``, falling back to *default*.

    Garbage input (``None``, ``"abc"``, ``-5``, a float string) never raises and
    never escapes the bounds -- a hostile ``?limit=99999999`` is clamped, not
    honoured, and ``?page=-1`` cannot produce a negative offset.
    """
    if not isinstance(raw, (str, int, float)):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True, slots=True)
class PageRequest:
    """A validated, clamped pagination window for a single list request."""

    page: int
    limit: int

    @classmethod
    def from_request(
        cls,
        *,
        default_limit: int = DEFAULT_PAGE_SIZE,
        max_limit: int = MAX_PAGE_SIZE,
    ) -> PageRequest:
        """Build a ``PageRequest`` from the current request's query string.

        Reads ``page`` (1-based) and ``limit``; both are clamped to safe
        bounds so no query-string value can request an unbounded or negative
        window.
        """
        limit = _clamp_int(
            request.args.get("limit"), default=default_limit, minimum=1, maximum=max_limit
        )
        page = _clamp_int(request.args.get("page"), default=1, minimum=1, maximum=1_000_000)
        return cls(page=page, limit=limit)

    @property
    def limitby(self) -> tuple[int, int]:
        """The ``(start, stop)`` tuple for PyDAL/penguin-dal ``select(limitby=...)``."""
        start = (self.page - 1) * self.limit
        return (start, start + self.limit)

    def meta(self, total: int | None = None) -> dict[str, dict[str, int | None]]:
        """Pagination metadata to merge into a list response body.

        Pass *total* (a ``db(query).count()``) when the handler can afford the
        extra count query, so clients know how many pages exist; omit it
        otherwise and ``total``/``pages`` are reported as ``None``.
        """
        pages = None if total is None else max(1, (total + self.limit - 1) // self.limit)
        return {
            "pagination": {
                "page": self.page,
                "limit": self.limit,
                "total": total,
                "pages": pages,
            }
        }
