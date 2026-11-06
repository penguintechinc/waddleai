"""Wave-5 Hugging Face artifact contract tests."""

import hashlib

import pytest

from shared.fleet.huggingface import (
    ArtifactDownloader,
    HuggingFaceArtifact,
    HuggingFaceArtifactError,
    build_download_plan,
)


def artifact(**overrides):
    """Test helper: artifact."""
    values = dict(
        repo="ibm-granite/granite-8b-code-instruct-GGUF",
        revision="9f8e7d6c5b4a",
        filename="model.Q4_K_M.gguf",
        sha256="a" * 64,
        destination="model-cache",
        license_approved=True,
        origin_approved=True,
        max_bytes=8_000_000_000,
    )
    values.update(overrides)
    return HuggingFaceArtifact(**values)


def test_build_download_plan_keeps_only_credential_reference():
    """Verify: build download plan keeps only credential reference."""
    plan = build_download_plan(
        artifact(private=True, credentials_ref="vault://models/granite"),
        allowed_destinations={"model-cache"},
    )
    assert plan.credentials_ref == "vault://models/granite"
    assert plan.sha256 == "a" * 64


@pytest.mark.parametrize(
    "overrides",
    [
        {"revision": "main"},
        {"sha256": "not-a-digest"},
        {"private": True},
        {"credentials_ref": "hf_secret_token"},
        {"license_approved": False},
        {"max_bytes": 0},
    ],
)
def test_artifact_contract_rejects_unapproved_or_secret_inputs(overrides):
    """Verify: artifact contract rejects unapproved or secret inputs."""
    with pytest.raises(HuggingFaceArtifactError):
        build_download_plan(artifact(**overrides), allowed_destinations={"model-cache"})


class Source:
    """Test double for Source."""

    def __init__(self, chunks):
        """Initialise the test double."""
        self.chunks = chunks

    async def stream(self, _plan):
        """Test helper: stream."""
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_downloader_verifies_digest_publishes_atomically_and_revokes(tmp_path):
    """Verify: downloader verifies digest publishes atomically and revokes."""
    data = b"verified model artifact"
    plan = build_download_plan(
        artifact(sha256=hashlib.sha256(data).hexdigest(), max_bytes=len(data)),
        allowed_destinations={"model-cache"},
    )
    downloader = ArtifactDownloader(
        Source([data[:5], data[5:]]), destination_roots={"model-cache": tmp_path}
    )
    path = await downloader.download(plan)
    assert path.read_bytes() == data
    assert downloader.revoke(plan) is True
    assert downloader.revoke(plan) is False


@pytest.mark.asyncio
async def test_downloader_removes_partial_on_digest_or_quota_failure(tmp_path):
    """Verify: downloader removes partial on digest or quota failure."""
    plan = build_download_plan(
        artifact(sha256="b" * 64, max_bytes=3), allowed_destinations={"model-cache"}
    )
    downloader = ArtifactDownloader(
        Source([b"too-large"]), destination_roots={"model-cache": tmp_path}
    )
    with pytest.raises(HuggingFaceArtifactError):
        await downloader.download(plan)
    assert not list(tmp_path.glob("*.part"))
