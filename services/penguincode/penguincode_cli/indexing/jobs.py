"""`IndexJob`: the durable row shape backing `IndexJobStore` (`penguincode.index_jobs`).

`JobType`/`JobState` are plain `str` enums so they round-trip to the
`index_jobs.job_type`/`.state` text columns (and the SQL `CHECK` constraints
in `db/migrations/0008_index_jobs.sql`) without a separate mapping table --
`server/services/knowledge.py` maps these to/from the proto `JobType`/
`JobState` enums at the RPC boundary only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobType(StrEnum):
    """The two kinds of background indexing work this queue carries."""

    INDEX_DOCS = (
        "index_docs"  #: `Index` RPC -- `DocumentationIndexer.index_library`/`.index_language`.
    )
    INDEX_CODE = "index_code"  #: `IndexCode` RPC -- `graphs.code.index_code`.


class JobState(StrEnum):
    """A job's lifecycle state -- `QUEUED` -> `RUNNING` -> `SUCCEEDED`/`FAILED`.

    There is no transition back to `QUEUED` from `RUNNING` -- a job
    interrupted by a pod restart is marked `FAILED` (reason `"interrupted"`)
    at the next startup, never silently re-queued (see
    `IndexJobStore.reap_interrupted`'s docstring for why).
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: Terminal states -- a job in one of these will never change state again.
TERMINAL_STATES = frozenset({JobState.SUCCEEDED, JobState.FAILED})


@dataclass(slots=True, frozen=True)
class IndexJob:
    """One `index_jobs` row, scope-stamped at creation, read back by `IndexJobStore`.

    `tenant_id`/`owner_user_id` are the row's visibility key (see
    `IndexJobStore.get`'s docstring) -- `team_id`/`org_id` are provenance
    only, mirroring `lessons/store.py`'s `PendingLessonRecord` split between
    visibility columns and provenance columns.
    """

    id: str
    tenant_id: str
    owner_user_id: str
    job_type: JobType
    state: JobState
    chunks_done: int
    chunks_total: int
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
