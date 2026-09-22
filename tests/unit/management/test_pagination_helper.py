"""Tests for the shared list-endpoint pagination helper (audit-2026-09-14)."""

from __future__ import annotations

import pytest
from quart import Quart

from services.management.app.api.v1._pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    PageRequest,
    _clamp_int,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5", 5),
        (None, 100),  # default
        ("abc", 100),  # non-numeric -> default
        ("-5", 1),  # below minimum -> clamped up
        ("99999999", 1000),  # above maximum -> clamped down
        (3.9, 3),  # float coerces via int()
    ],
)
def test_clamp_int_never_escapes_bounds_or_raises(raw: object, expected: int) -> None:
    """A hostile or malformed value is clamped, never raised or honoured."""
    assert _clamp_int(raw, default=100, minimum=1, maximum=1000) == expected


@pytest.mark.asyncio
async def test_from_request_defaults() -> None:
    """No query params -> first page at the default size."""
    app = Quart(__name__)
    async with app.test_request_context("/x", method="GET"):
        page = PageRequest.from_request()
    assert page.page == 1
    assert page.limit == DEFAULT_PAGE_SIZE
    assert page.limitby == (0, DEFAULT_PAGE_SIZE)


@pytest.mark.asyncio
async def test_from_request_clamps_hostile_limit() -> None:
    """`?limit=99999999` is capped at MAX_PAGE_SIZE -- the whole point of the fix."""
    app = Quart(__name__)
    async with app.test_request_context("/x?limit=99999999&page=2", method="GET"):
        page = PageRequest.from_request()
    assert page.limit == MAX_PAGE_SIZE
    assert page.page == 2
    assert page.limitby == (MAX_PAGE_SIZE, 2 * MAX_PAGE_SIZE)


@pytest.mark.asyncio
async def test_from_request_rejects_negative_page() -> None:
    """`?page=-1` cannot produce a negative offset."""
    app = Quart(__name__)
    async with app.test_request_context("/x?page=-1", method="GET"):
        page = PageRequest.from_request()
    assert page.page == 1
    assert page.limitby[0] == 0


def test_limitby_offset_math() -> None:
    """Page 3 at limit 20 starts at offset 40."""
    page = PageRequest(page=3, limit=20)
    assert page.limitby == (40, 60)


def test_meta_computes_page_count() -> None:
    """`total` yields a ceiling page count; omitting it reports None."""
    page = PageRequest(page=1, limit=20)
    assert page.meta(total=41)["pagination"]["pages"] == 3  # 41 -> 3 pages of 20
    assert page.meta()["pagination"]["pages"] is None
    assert page.meta(total=0)["pagination"]["pages"] == 1
