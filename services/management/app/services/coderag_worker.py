"""CodeRAG git-pull worker (§9.1): clone/pull, content-hash incremental re-index.

Async Management worker: clone/pull a registered repo (server-side; git
credentials via the provider-credential pattern) -> diff stored chunk
``content_hash``es per path -> re-chunk + re-embed changed files only ->
upsert ``code_chunks``, keyed on ``(repo_id, branch_ref)`` so parallel
branches/worktrees never cross-contaminate. Triggers: push webhook
(GitHub/Gitea), cron (supercronic via ``run_scheduled()``), manual
(``index()``).

Gated on ``features.enabled("coderag", distinct_id=str(org_id))`` -- with
the flag off, ``index()`` is a no-op: no clone, no writes.

Alongside chunk indexing, ``index()`` also emits the structural graph
(Task 9/10's ``extract_graph``) for changed files and scrubs it for
deleted ones, through the tenant-scoped ``TenantGraphClient`` (Task 8),
gated on its own ``waddleai.graph`` flag (fail-safe OFF). Graph extraction
is deterministic tree-sitter parsing -- the >=2B minimum-model rule is N/A,
there is no model anywhere in this path. The graph is an additive,
best-effort index: any graph-store failure (including an unavailable/
not-ready instance or a bad org id) is caught, logged, and never fails or
raises out of the chunk indexing pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from shared.graph.types import GraphUnavailableError, TenantScope
from shared.knowledge.code_chunker import CodeChunkDraft, chunk_code
from shared.knowledge.code_graph import GraphFragment, extract_graph
from shared.knowledge.embed import embed_cached

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.coderag"
_GRAPH_FLAG_KEY = "waddleai.graph"


@runtime_checkable
class _GraphClientLike(Protocol):
    """Structural subset of `TenantGraphClient` the worker actually calls.

    Kept as a Protocol (mirroring `shared.graph.resolver._SqlDB`) rather
    than importing `TenantGraphClient` at module scope for typing purposes
    -- an injected test double only needs to satisfy this shape, and mypy
    --strict gets a real type for `graph_client` instead of `object`.
    """

    async def upsert_node(
        self, scope: TenantScope, label: str, qualified_name: str, props: dict[str, Any]
    ) -> None:
        """Create/update one node under `scope`."""
        ...

    async def upsert_edge(
        self,
        scope: TenantScope,
        edge_type: str,
        src_qn: str,
        dst_qn: str,
        props: dict[str, Any],
    ) -> None:
        """Create/update one directed edge under `scope`."""
        ...

    async def delete_scope(self, scope: TenantScope, path: str | None = None) -> int:
        """Delete nodes/edges under `scope`, optionally narrowed to `path`."""
        ...


# Files larger than this are skipped -- avoids embedding huge generated/binary blobs.
_MAX_FILE_BYTES = 512_000
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "vendor", "dist", "build"}


def _coderag_enabled(org_id: int) -> bool:
    """Fail-safe-OFF check of the ``waddleai.coderag`` flag (§14.5)."""
    try:
        from shared.utils.feature_flags import is_feature_enabled

        return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id), default=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("coderag flag evaluation failed, treating as OFF: %s", exc)
        return False


@dataclass(slots=True)
class IndexResult:
    """Outcome of one ``CodeRagWorker.index()`` run.

    ``graph_status`` is independent of ``index_status`` -- chunk indexing
    can succeed (``"indexed"``) while the graph side is ``"skipped"``
    (flag off), ``"emitted"`` (graph nodes/edges written), ``"unavailable"``
    (the org's graph instance isn't ready -- resolved cleanly, best-effort
    skip), or ``"error"`` (any other graph-store failure, also best-effort
    skip). The graph is a rebuildable index, so none of those graph
    outcomes ever downgrade ``index_status``.
    """

    repo_id: int
    branch_ref: str
    index_status: str  # indexed | skipped_flag_off | error
    last_commit: str | None = None
    files_changed: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    error: str | None = None
    graph_status: str = "skipped"  # skipped | emitted | unavailable | error


def diff_paths(
    existing_hashes_by_path: dict[str, frozenset[str]],
    new_chunks_by_path: dict[str, list[CodeChunkDraft]],
) -> tuple[list[str], list[str]]:
    """Pure diff: which paths changed (need re-embed) vs. which were deleted.

    A path is unchanged exactly when its freshly-chunked content_hash set is
    identical to what's already stored -- deterministic chunking means
    identical file content always produces the identical hash set, so this
    is the short-circuit that keeps unchanged files out of ``embed_cached``
    entirely (not just relying on ``embed_cached``'s own dedup).
    """
    changed: list[str] = []
    for path, new_chunks in new_chunks_by_path.items():
        new_hashes = frozenset(c.content_hash for c in new_chunks)
        if existing_hashes_by_path.get(path) != new_hashes:
            changed.append(path)
    deleted = [p for p in existing_hashes_by_path if p not in new_chunks_by_path]
    return changed, deleted


class CodeRagWorker:
    """Clones/pulls a registered repo and incrementally re-indexes its CodeRAG chunks."""

    def __init__(
        self,
        db: object,
        workdir: str | None = None,
        graph_client: _GraphClientLike | None = None,
    ) -> None:
        """Bind the worker to a penguin-dal handle and a scratch workdir for clones.

        ``graph_client`` (a ``TenantGraphClient``) is injectable for tests;
        when ``None`` and the ``waddleai.graph`` flag is on, ``index()``
        builds one from ``self.db`` on demand.
        """
        self.db = db
        self.workdir = workdir or tempfile.gettempdir()
        self.graph_client = graph_client

    def _graph_enabled(self, org_id: int) -> bool:
        """Fail-safe-OFF check of the ``waddleai.graph`` flag."""
        try:
            from shared.utils.feature_flags import is_feature_enabled

            return is_feature_enabled(_GRAPH_FLAG_KEY, distinct_id=str(org_id), default=False)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("graph flag evaluation failed, treating as OFF: %s", exc)
            return False

    def _extract_file_graph(self, clone_dir: str, path: str) -> GraphFragment | None:
        """Read one working-tree file and extract its structural graph fragment.

        Mirrors ``_chunk_working_tree``'s skip-on-unreadable behavior -- a
        file that vanished mid-diff or isn't valid UTF-8 text is skipped
        (``None``), never raised, so one bad file can't abort the whole
        incremental graph pass.
        """
        full_path = pathlib.Path(clone_dir) / path
        try:
            content = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return extract_graph(path, content)

    async def _emit_graph_changes(
        self,
        graph_client: _GraphClientLike,
        scope: TenantScope,
        clone_dir: str,
        changed: list[str],
        deleted: list[str],
    ) -> None:
        """Mirror the chunk diff into the graph under ``scope``.

        Deleted paths are scrubbed via ``delete_scope``. Changed paths are
        scrubbed first (clearing any stale nodes/edges from the prior
        version of the file) and then re-extracted and re-emitted -- the
        same delete-then-reinsert shape ``index()`` already uses for
        chunks, so graph state never lags or diverges from chunk state for
        a given path.
        """
        for path in deleted:
            await graph_client.delete_scope(scope, path=path)
        for path in changed:
            await graph_client.delete_scope(scope, path=path)
            fragment = await asyncio.to_thread(self._extract_file_graph, clone_dir, path)
            if fragment is None:
                continue
            for node in fragment.nodes:
                await graph_client.upsert_node(
                    scope,
                    node.label,
                    node.qualified_name,
                    {"name": node.name, "path": node.path},
                )
            for edge in fragment.edges:
                await graph_client.upsert_edge(
                    scope, edge.edge_type, edge.src_qn, edge.dst_qn, {"path": edge.path}
                )

    async def index(
        self, repo_id: int, branch: str | None = None, trigger: str = "manual"
    ) -> IndexResult:
        """Index (or incrementally re-index) one repo/branch.

        Args:
            repo_id: ``code_repos.id``.
            branch: Branch to index; defaults to the repo's configured branch.
            trigger: ``"webhook" | "cron" | "manual"``, for logging/audit.

        """
        fallback_branch_ref = branch or "main"
        repo_row = await asyncio.to_thread(self._fetch_repo, repo_id)
        if repo_row is None:
            return IndexResult(
                repo_id=repo_id,
                branch_ref=fallback_branch_ref,
                index_status="error",
                error="repo_not_found",
            )

        if not _coderag_enabled(repo_row["org_id"]):
            return IndexResult(
                repo_id=repo_id, branch_ref=fallback_branch_ref, index_status="skipped_flag_off"
            )

        branch_ref = fallback_branch_ref
        logger.info("coderag index: repo=%s branch=%s trigger=%s", repo_id, branch_ref, trigger)

        try:
            clone_dir, last_commit = await asyncio.to_thread(
                self._clone_or_pull, repo_row["source_url"], branch_ref, repo_id
            )
        except Exception as exc:
            logger.error("coderag index: clone/pull failed for repo %s: %s", repo_id, exc)
            return IndexResult(
                repo_id=repo_id, branch_ref=branch_ref, index_status="error", error=str(exc)
            )

        new_chunks_by_path = await asyncio.to_thread(self._chunk_working_tree, clone_dir)
        existing_hashes_by_path = await asyncio.to_thread(
            self._fetch_existing_hashes, repo_id, branch_ref
        )
        changed, deleted = diff_paths(existing_hashes_by_path, new_chunks_by_path)

        for path in deleted:
            await asyncio.to_thread(self._delete_path_chunks, repo_id, branch_ref, path)

        for path in changed:
            await asyncio.to_thread(self._delete_path_chunks, repo_id, branch_ref, path)
            for draft in new_chunks_by_path[path]:
                vector = await embed_cached(draft.content, db=self.db)
                await asyncio.to_thread(self._insert_chunk, repo_id, branch_ref, draft, vector)

        graph_status = "skipped"
        if self._graph_enabled(repo_row["org_id"]):
            try:
                from shared.graph.client import TenantGraphClient

                client = self.graph_client or TenantGraphClient(self.db)
                scope = TenantScope(
                    org_id=str(repo_row["org_id"]), repo_id=str(repo_id), branch_ref=branch_ref
                )
                await self._emit_graph_changes(client, scope, clone_dir, changed, deleted)
                graph_status = "emitted"
            except GraphUnavailableError as exc:
                # Best-effort: the org's graph instance isn't ready/reachable (or
                # failed to resolve, e.g. a bad org id) -- skip graph emission,
                # never touch chunk indexing, which has already completed above.
                logger.warning(
                    "coderag graph unavailable for repo %s org %s: %s",
                    repo_id,
                    repo_row["org_id"],
                    exc,
                )
                graph_status = "unavailable"
            except Exception as exc:
                # The graph is a rebuildable index (spec §2) -- any other failure
                # here must never fail or raise out of the chunk indexing pipeline.
                logger.error("coderag graph emission failed for repo %s: %s", repo_id, exc)
                graph_status = "error"

        await asyncio.to_thread(self._mark_indexed, repo_id, last_commit)
        return IndexResult(
            repo_id=repo_id,
            branch_ref=branch_ref,
            index_status="indexed",
            last_commit=last_commit,
            files_changed=changed,
            files_deleted=deleted,
            graph_status=graph_status,
        )

    async def run_scheduled(self) -> list[IndexResult]:
        """Supercronic entrypoint: re-index every non-disabled registered repo."""
        repo_ids = await asyncio.to_thread(self._fetch_all_repo_ids)
        return [await self.index(repo_id, trigger="cron") for repo_id in repo_ids]

    # -- DB/git IO (thin, mockable) -------------------------------------

    def _fetch_repo(self, repo_id: int) -> dict | None:
        row = self.db(self.db.code_repos.id == repo_id).select().first()
        if row is None:
            return None
        return {"id": row.id, "org_id": row.org_id, "source_url": row.source_url}

    def _fetch_all_repo_ids(self) -> list[int]:
        rows = self.db(self.db.code_repos.index_status != "disabled").select()
        return [r.id for r in rows]

    def _clone_or_pull(self, source_url: str, branch: str, repo_id: int) -> tuple[str, str]:
        import git

        clone_dir = os.path.join(self.workdir, f"coderag-repo-{repo_id}")
        if os.path.isdir(os.path.join(clone_dir, ".git")):
            repo = git.Repo(clone_dir)
            repo.remotes.origin.fetch()
            repo.git.checkout(branch)
            repo.remotes.origin.pull()
        else:
            os.makedirs(self.workdir, exist_ok=True)
            repo = git.Repo.clone_from(source_url, clone_dir, branch=branch)
        return clone_dir, repo.head.commit.hexsha

    def _chunk_working_tree(self, clone_dir: str) -> dict[str, list[CodeChunkDraft]]:
        result: dict[str, list[CodeChunkDraft]] = {}
        for root, dirs, files in os.walk(clone_dir):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            for filename in sorted(files):
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, clone_dir)
                try:
                    if os.path.getsize(full_path) > _MAX_FILE_BYTES:
                        continue
                    with open(full_path, encoding="utf-8") as fh:
                        content = fh.read()
                except (UnicodeDecodeError, OSError):
                    continue
                result[rel_path] = chunk_code(rel_path, content)
        return result

    def _fetch_existing_hashes(self, repo_id: int, branch_ref: str) -> dict[str, frozenset[str]]:
        chunks = self.db.code_chunks
        rows = self.db((chunks.repo_id == repo_id) & (chunks.branch_ref == branch_ref)).select()
        by_path: dict[str, set[str]] = {}
        for row in rows:
            by_path.setdefault(row.path, set()).add(row.content_hash)
        return {path: frozenset(hashes) for path, hashes in by_path.items()}

    def _delete_path_chunks(self, repo_id: int, branch_ref: str, path: str) -> None:
        self.db(
            (self.db.code_chunks.repo_id == repo_id)
            & (self.db.code_chunks.branch_ref == branch_ref)
            & (self.db.code_chunks.path == path)
        ).delete()
        self.db.commit()

    def _insert_chunk(
        self, repo_id: int, branch_ref: str, draft: CodeChunkDraft, vector: list[float]
    ) -> None:
        self.db.code_chunks.insert(
            repo_id=repo_id,
            branch_ref=branch_ref,
            path=draft.path,
            symbol=draft.symbol,
            kind=draft.kind,
            start_line=draft.start_line,
            end_line=draft.end_line,
            content=draft.content,
            content_hash=draft.content_hash,
            embedding=vector,
            status="active",
        )
        self.db.commit()

    def _mark_indexed(self, repo_id: int, last_commit: str) -> None:
        self.db(self.db.code_repos.id == repo_id).update(
            index_status="indexed", last_commit=last_commit
        )
        self.db.commit()


def create_coderag_worker(db: object, workdir: str | None = None) -> CodeRagWorker:
    """Factory function, matching this service package's ``create_*`` convention."""
    return CodeRagWorker(db, workdir=workdir)


__all__ = ["CodeRagWorker", "IndexResult", "diff_paths", "create_coderag_worker"]
