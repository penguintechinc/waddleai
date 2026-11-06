"""Model-store pull entrypoint run inside the K8s pull Job (AM19a).

Downloads a HuggingFace repo snapshot into the RWX model store, reports progress by writing
``.progress`` (JSON bytes_done/bytes_total) beside the weights, and publishes a ``.digest``
marker (sha256 over every file's path and content hash) only after the download completed, so a
partial pull is never mistaken for present weights. The HF token comes from ``HF_TOKEN`` in the
environment and is never logged or passed as an argument.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DIGEST_MARKER = ".digest"
PROGRESS_FILE = ".progress"
_POLL_S = 2.0


def dir_bytes(path: Path) -> int:
    """Total size of regular files under ``path`` (including in-flight partial downloads)."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def compute_digest(path: Path) -> str:
    """Deterministic sha256 over the sorted relative paths and content hashes of all files."""
    outer = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file() and p.name not in _MARKERS):
        inner = hashlib.sha256()
        with file.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                inner.update(chunk)
        outer.update(f"{file.relative_to(path).as_posix()}:{inner.hexdigest()}\n".encode())
    return outer.hexdigest()


_MARKERS = frozenset({DIGEST_MARKER, PROGRESS_FILE})


def _write_progress(dest: Path, done: int, total: int) -> None:
    """Atomically publish download progress for the management poller."""
    tmp = dest / (PROGRESS_FILE + ".tmp")
    tmp.write_text(json.dumps({"bytes_done": done, "bytes_total": total}))
    os.replace(tmp, dest / PROGRESS_FILE)


def pull(repo_id: str, dest: Path, hub: Any | None = None) -> str:
    """Download ``repo_id`` into ``dest`` and return the published digest."""
    if hub is None:
        import huggingface_hub as hub  # noqa: PLC0415 -- only the Job image needs it
    token = os.environ.get("HF_TOKEN") or None
    dest.mkdir(parents=True, exist_ok=True)
    info = hub.HfApi().model_info(repo_id, files_metadata=True, token=token)
    total = sum(int(getattr(s, "size", 0) or 0) for s in info.siblings)
    errors: list[BaseException] = []

    def _download() -> None:
        try:
            hub.snapshot_download(repo_id=repo_id, local_dir=str(dest), token=token)
        except BaseException as exc:  # surfaced to the main thread below
            errors.append(exc)

    worker = threading.Thread(target=_download, daemon=True)
    worker.start()
    while worker.is_alive():
        _write_progress(dest, dir_bytes(dest), total)
        worker.join(_POLL_S)
    if errors:
        raise errors[0]
    _write_progress(dest, total, total)
    digest = compute_digest(dest)
    (dest / DIGEST_MARKER).write_text(digest)
    return digest


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``--model-ref`` and ``--dest``; exit 0 only when the digest was published."""
    parser = argparse.ArgumentParser(description="Pull model weights into the model store")
    parser.add_argument("--model-ref", required=True)
    parser.add_argument("--dest", required=True)
    args = parser.parse_args(argv)
    try:
        digest = pull(args.model_ref, Path(args.dest))
    except Exception as exc:
        # message only: the hub exception text never includes the token
        logger.error("model pull failed: %s", type(exc).__name__)
        return 1
    logger.info("model pulled, digest %s", digest[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
