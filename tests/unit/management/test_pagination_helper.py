"""Tests for the shared list-endpoint pagination helper (audit-2026-09-14)."""

from __future__ import annotations

from typing import Any

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
    assert page.limitby == (MAX_PAGE_SIZE, MAX_PAGE_SIZE)


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
    assert page.limitby == (40, 20)  # (offset, limit) -- penguin_dal convention
    assert page.bounds == (40, 60)  # (start, stop) -- list slicing


def test_meta_computes_page_count() -> None:
    """`total` yields a ceiling page count; omitting it reports None."""
    page = PageRequest(page=1, limit=20)
    assert page.meta(total=41)["pagination"]["pages"] == 3  # 41 -> 3 pages of 20
    assert page.meta()["pagination"]["pages"] is None
    assert page.meta(total=0)["pagination"]["pages"] == 1


def _sqlite_db_with_rows(tmp_path: Any, count: int) -> Any:
    """Real penguin_dal sqlite DB holding *count* ordered rows (no mocks)."""
    from penguin_dal.db import DB
    from penguin_dal.field import Field

    db = DB(f"sqlite:///{tmp_path}/page.db", reflect=False)
    db.define_table("things", Field("name", "string"))
    for i in range(1, count + 1):
        db.things.insert(name=f"n{i}")
    return db


@pytest.mark.parametrize(
    ("page_no", "limit", "expected_ids"),
    [
        (1, 10, list(range(1, 11))),
        (2, 10, list(range(11, 21))),
        (3, 10, list(range(21, 26))),
        (2, 7, list(range(8, 15))),
    ],
)
def test_limitby_pages_against_real_penguin_dal(
    tmp_path: Any, page_no: int, limit: int, expected_ids: list[int]
) -> None:
    """regression: page N of 25 rows returns exactly the right window (was over-returning)."""
    db = _sqlite_db_with_rows(tmp_path, 25)
    page = PageRequest(page=page_no, limit=limit)
    rows = db(db.things.id > 0).select(limitby=page.limitby, orderby=db.things.id)
    assert [r.id for r in rows] == expected_ids
