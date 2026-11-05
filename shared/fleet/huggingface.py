"""Approved Hugging Face/GGUF artifact contract (Wave 5).

This is validation and planning only.  The runner that downloads an artifact
receives a credential *reference*, never a Hugging Face token.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


class HuggingFaceArtifactError(ValueError):
    """Raised when a HuggingFace artifact request violates the contract.

    Covers unapproved destinations and inline secret inputs.
    """

    pass


@dataclass(frozen=True, slots=True)
class HuggingFaceArtifact:
    """Description of a HuggingFace model artifact to download.

    Carries a credential reference only, never the secret itself.
    """

    repo: str
    revision: str
    filename: str
    sha256: str
    destination: str
    license_approved: bool
    origin_approved: bool
    credentials_ref: str | None = None
    private: bool = False
    max_bytes: int = 0
    format: str = "gguf"

    def validate(self, *, allowed_destinations: set[str]) -> None:
        """Validate the artifact against the allowed destinations.

        Rejects unapproved destinations and secret-bearing inputs.
        """
        if not _REPO.fullmatch(self.repo):
            raise HuggingFaceArtifactError("repo must be an owner/name identifier")
        if not self.revision or self.revision.lower() in {"main", "latest", "master"}:
            raise HuggingFaceArtifactError(
                "revision must be an immutable commit SHA or release revision"
            )
        if not self.filename or "/" in self.filename or ".." in self.filename:
            raise HuggingFaceArtifactError("filename must be a single artifact filename")
        if not _SHA256.fullmatch(self.sha256):
            raise HuggingFaceArtifactError("artifact sha256 is required")
        if self.destination not in allowed_destinations:
            raise HuggingFaceArtifactError("artifact destination is not allowlisted")
        if not self.license_approved or not self.origin_approved:
            raise HuggingFaceArtifactError("license and origin approval are required")
        if self.private and not self.credentials_ref:
            raise HuggingFaceArtifactError("private artifact requires credentials_ref")
        if self.credentials_ref and (
            "hf_" in self.credentials_ref.lower() or "token" in self.credentials_ref.lower()
        ):
            raise HuggingFaceArtifactError("credentials_ref must not embed a token")
        if self.max_bytes <= 0:
            raise HuggingFaceArtifactError("max_bytes quota is required")
        if self.format not in {"gguf", "safetensors"}:
            raise HuggingFaceArtifactError("unsupported model artifact format")


@dataclass(frozen=True, slots=True)
class ArtifactDownloadPlan:
    """A vetted plan for downloading one artifact.

    Keeps only the credential reference so secrets never persist.
    """

    repo: str
    revision: str
    filename: str
    sha256: str
    destination: str
    credentials_ref: str | None
    max_bytes: int
    format: str


class ArtifactSource(Protocol):
    """Authenticated transport seam; receives only a vault reference, never a token."""

    def stream(self, plan: ArtifactDownloadPlan) -> AsyncIterator[bytes]:
        """Stream the artifact bytes for a plan.

        Implemented by the transport that talks to HuggingFace.
        """


class ArtifactDownloader:
    """Quota-bound, digest-verifying downloader with atomic publication and revocation."""

    def __init__(self, source: ArtifactSource, *, destination_roots: dict[str, Path]) -> None:
        """Bind a byte source to the approved destination roots.

        Downloads can only land under these roots.
        """
        self._source = source
        self._roots = {name: Path(root).resolve() for name, root in destination_roots.items()}

    def _target(self, plan: ArtifactDownloadPlan) -> Path:
        root = self._roots.get(plan.destination)
        if root is None:
            raise HuggingFaceArtifactError("artifact destination is not configured")
        target = (root / plan.filename).resolve()
        if root not in target.parents:
            raise HuggingFaceArtifactError("artifact target escapes destination")
        return target

    async def download(self, plan: ArtifactDownloadPlan) -> Path:
        """Download an artifact, verify its digest and publish it atomically.

        Removes partial files on digest or quota failure and revokes the credential.
        """
        target = self._target(plan)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".part")
        digest = hashlib.sha256()
        total = 0
        try:
            with partial.open("xb") as output:
                async for chunk in self._source.stream(plan):
                    if not isinstance(chunk, bytes):
                        raise HuggingFaceArtifactError("artifact source yielded non-bytes data")
                    total += len(chunk)
                    if total > plan.max_bytes:
                        raise HuggingFaceArtifactError("artifact exceeded approved quota")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != plan.sha256:
                raise HuggingFaceArtifactError("artifact digest mismatch")
            os.replace(partial, target)
            return target
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def revoke(self, plan: ArtifactDownloadPlan) -> bool:
        """Remove an approved artifact after a source/licence revocation event."""
        target = self._target(plan)
        if not target.exists():
            return False
        target.unlink()
        return True


def build_download_plan(
    artifact: HuggingFaceArtifact, *, allowed_destinations: set[str]
) -> ArtifactDownloadPlan:
    """Validate an artifact and build its download plan.

    The plan retains only the credential reference.
    """
    artifact.validate(allowed_destinations=allowed_destinations)
    return ArtifactDownloadPlan(
        repo=artifact.repo,
        revision=artifact.revision,
        filename=artifact.filename,
        sha256=artifact.sha256,
        destination=artifact.destination,
        credentials_ref=artifact.credentials_ref,
        max_bytes=artifact.max_bytes,
        format=artifact.format,
    )
