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
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field

from shared.knowledge.code_chunker import CodeChunkDraft, chunk_code
from shared.knowledge.embed import embed_cached

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.coderag"

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
    """Outcome of one ``CodeRagWorker.index()`` run."""

    repo_id: int
    branch_ref: str
    index_status: str  # indexed | skipped_flag_off | error
    last_commit: str | None = None
    files_changed: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    error: str | None = None


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

    def __init__(self, db: object, workdir: str | None = None) -> None:
        """Bind the worker to a penguin-dal handle and a scratch workdir for clones."""
        self.db = db
        self.workdir = workdir or tempfile.gettempdir()

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

        await asyncio.to_thread(self._mark_indexed, repo_id, last_commit)
        return IndexResult(
            repo_id=repo_id,
            branch_ref=branch_ref,
            index_status="indexed",
            last_commit=last_commit,
            files_changed=changed,
            files_deleted=deleted,
        )

    def handle_webhook(self, payload: dict) -> tuple[int, str] | None:
        """Resolve a push-webhook payload (GitHub/Gitea shape) to ``(repo_id, branch)``."""
        clone_url = (payload.get("repository") or {}).get("clone_url")
        ref = payload.get("ref", "")
        branch = ref.rsplit("/", 1)[-1] if ref else None
        if not clone_url:
            return None
        repo_row = self._fetch_repo_by_source_url(clone_url)
        if repo_row is None:
            return None
        return repo_row["id"], branch or "main"

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

    def _fetch_repo_by_source_url(self, source_url: str) -> dict | None:
        row = self.db(self.db.code_repos.source_url == source_url).select().first()
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
