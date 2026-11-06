"""C3b: model-store pull Job, progress events, gated token handling and weights teardown."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.management.app.services import fleet_secrets, model_store
from services.management.app.services.model_store_job import build_pull_job_manifest
from shared.fleet.vllm_ray import model_pull
from shared.security.credential_encryption import encrypt_credential
from tests.unit.management.vllm_harness import (
    FakeBackend,
    FakeK8s,
    seed_backend,
    seed_deployment,
)

REF = "org/Model-7B"
DEL = "/api/v1/fleet/models/org/Model-7B/weights"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A model-store root on tmp and an installed fake Kubernetes."""
    root = tmp_path / "models"
    root.mkdir()
    monkeypatch.setenv("WADDLEAI_MODEL_STORE_PATH", str(root))
    monkeypatch.setenv("WADDLEAI_MODEL_PULL_IMAGE", "registry/management@sha256:abc")
    k8s = FakeK8s()
    fleet_secrets.set_gateway(k8s)
    yield root, k8s
    fleet_secrets.set_gateway(None)


def _publish(root: Path, org: int = 1, ref: str = REF) -> Path:
    """Simulate a finished pull: the digest marker exists."""
    path = root / str(org) / ref
    path.mkdir(parents=True)
    (path / model_store.DIGEST_MARKER).write_text("digest")
    return path


def test_ensure_weights_skips_if_present(vllm_db, store) -> None:
    """A digest marker means no Job, no Secret, no PULLING event; the slug is linked."""
    root, k8s = store
    _publish(root)
    backend = seed_backend(vllm_db)
    dep = seed_deployment(vllm_db, backend, status=1, slug="dep-abc")
    path = asyncio.run(model_store.ensure_weights(vllm_db, 1, REF, dep.id, 1, slug="dep-abc"))
    assert path == f"{root}/1/{REF}"
    assert k8s.jobs == {} and k8s.secrets == {}
    assert vllm_db(vllm_db.vllm_deployment_events.id > 0).count() == 0
    assert (root / "dep-abc").resolve() == (root / "1" / REF).resolve()


def test_absent_weights_start_one_job_and_mark_pulling(vllm_db, store) -> None:
    """No marker: one rootless Job is created and the deployment is PULLING with an event."""
    root, k8s = store
    backend = seed_backend(vllm_db)
    dep = seed_deployment(vllm_db, backend, status=1)
    asyncio.run(model_store.ensure_weights(vllm_db, 1, REF, dep.id, 1, slug=dep.slug))
    assert len(k8s.jobs) == 1
    container = next(iter(k8s.jobs.values()))["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["args"][:2] == ["--model-ref", REF]
    row = vllm_db(vllm_db.vllm_deployments.id == dep.id).select().first()
    assert row.status == 2
    assert vllm_db(vllm_db.vllm_deployment_events.state == 2).count() == 1


def test_ensure_weights_rejects_non_vllm_kind_and_traversal(vllm_db, store) -> None:
    """Only vLLM pulls exist; a traversal model_ref never reaches the filesystem."""
    with pytest.raises(ValueError, match="only supported for vLLM"):
        asyncio.run(model_store.ensure_weights(vllm_db, 1, REF, 1, 2))
    for bad in ("../x", "a/../../b", "/abs", "a//b", "a b"):
        with pytest.raises(ValueError, match="invalid model reference"):
            model_store.canonical_path(1, bad)


class Clock:
    """A settable clock."""

    now = 0.0

    def __call__(self) -> float:
        """Current fake time."""
        return self.now


def test_pull_progress_events_every_10s(vllm_db, store) -> None:
    """Polls at 0,5,12,35 s write exactly three progress events with the file's bytes."""
    root, k8s = store
    backend = seed_backend(vllm_db)
    dep = seed_deployment(vllm_db, backend, status=2)
    path = root / "1" / REF
    path.mkdir(parents=True)
    clock = Clock()
    poller = model_store.PullPoller(clock=clock)
    fake = FakeBackend()
    for t, done in ((0, 10), (5, 20), (12, 30), (35, 40)):
        clock.now = t
        (path / model_store.PROGRESS_FILE).write_text(
            json.dumps({"bytes_done": done, "bytes_total": 100})
        )
        assert asyncio.run(poller(vllm_db, fake, dep)) is False
    events = list(
        vllm_db(vllm_db.vllm_deployment_events.deployment_id == dep.id).select(
            orderby=vllm_db.vllm_deployment_events.id
        )
    )
    assert [json.loads(e.message)["bytes_done"] for e in events] == [10, 30, 40]
    assert all(json.loads(e.message)["bytes_total"] == 100 for e in events)
    assert fake.calls == []


def test_pull_completion_links_slug_deploys_and_drops_token_secret(vllm_db, store) -> None:
    """When the weights land: the token Secret is deleted, the slug linked, deploy called."""
    root, k8s = store
    backend = seed_backend(vllm_db)
    dep = seed_deployment(vllm_db, backend, status=2, slug="dep-xyz")
    k8s.secrets[f"waddleai-hf-{dep.id}"] = {"HF_TOKEN": "x"}
    _publish(root)
    fake = FakeBackend()
    assert asyncio.run(model_store.PullPoller()(vllm_db, fake, dep)) is False
    assert fake.calls == [("deploy", "dep-xyz")]
    assert f"waddleai-hf-{dep.id}" in k8s.deleted_secrets
    assert (root / "dep-xyz").is_symlink()


@pytest.mark.parametrize("state", ["failed", "missing"])
def test_pull_job_failure_marks_deployment_failed(vllm_db, store, state) -> None:
    """A failed or vanished Job fails the deployment and removes the token Secret."""
    root, k8s = store
    backend = seed_backend(vllm_db)
    dep = seed_deployment(vllm_db, backend, status=2)
    k8s.job_states[model_store.job_name(1, REF)] = state
    k8s.secrets[f"waddleai-hf-{dep.id}"] = {"HF_TOKEN": "x"}
    asyncio.run(model_store.PullPoller()(vllm_db, FakeBackend(), dep))
    row = vllm_db(vllm_db.vllm_deployments.id == dep.id).select().first()
    assert row.status == 8 and state in row.status_detail
    assert f"waddleai-hf-{dep.id}" in k8s.deleted_secrets


def test_gated_token_via_short_lived_secret(vllm_db, store, caplog) -> None:
    """The backend's HF token goes only into a Secret + secretKeyRef; it never leaks."""
    root, k8s = store
    token = "hf_SECRET_gated_token_value"  # noqa: S105 -- test sentinel
    backend = seed_backend(vllm_db)
    vllm_db(vllm_db.fleet_backends.id == backend).update(credentials_ref=encrypt_credential(token))
    vllm_db.commit()
    dep = seed_deployment(vllm_db, backend, status=1)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(model_store.ensure_weights(vllm_db, 1, REF, dep.id, 1, slug=dep.slug))
    assert k8s.secrets[f"waddleai-hf-{dep.id}"] == {"HF_TOKEN": token}
    manifest = json.dumps(next(iter(k8s.jobs.values())))
    assert token not in manifest and "secretKeyRef" in manifest
    events = [e.message for e in vllm_db(vllm_db.vllm_deployment_events.id > 0).select()]
    assert token not in " ".join(events) and token not in caplog.text


def test_manifest_without_secret_has_no_token_env() -> None:
    """No credentials: the Job carries no HF_TOKEN env at all."""
    manifest = build_pull_job_manifest(
        name="n", namespace="ns", image="img", model_ref=REF, store_path="/models/1/x",
        secret_name=None,
    )  # fmt: skip
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["env"] == [] and manifest["spec"]["ttlSecondsAfterFinished"] == 300
    assert manifest["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}


async def test_delete_weights_refused_while_referenced_then_succeeds(
    client, vllm_db, both_flags, auth_headers, store
) -> None:
    """An own-org vLLM/Ollama/llama.cpp reference blocks deletion (409); then it succeeds."""
    root, _ = store
    path = _publish(root)
    backend = seed_backend(vllm_db)
    dep = seed_deployment(vllm_db, backend, model_ref=REF)
    resp = await client.delete(DEL, headers=auth_headers)
    body = await resp.get_json()
    assert resp.status_code == 409 and body["reason"] == "weights in use"
    assert body["deployments"] == [{"kind": 1, "public_id": str(dep.public_id)}]
    assert path.exists()
    vllm_db(vllm_db.vllm_deployments.id == dep.id).delete()
    vllm_db.commit()
    ok = await client.delete(DEL, headers=auth_headers)
    assert ok.status_code == 200 and not path.exists()


async def test_delete_weights_counts_own_org_ollama_and_llamacpp(
    client, vllm_db, both_flags, auth_headers, store
) -> None:
    """Ollama (via ollama_models) and llama.cpp deployments of this org's backends block it."""
    root, _ = store
    _publish(root)
    backend = seed_backend(vllm_db)
    ollama_id = vllm_db.ollama_deployments.insert(
        name="o", endpoint_url="http://o", fleet_backend_id=backend,
        public_id="00000000-0000-0000-0000-0000000000a1",
    )  # fmt: skip
    vllm_db.ollama_models.insert(deployment_id=ollama_id, model_name=REF)
    vllm_db.llamacpp_deployments.insert(
        name="l", model_name=REF, deployment_type="kubernetes", fleet_backend_id=backend,
        public_id="00000000-0000-0000-0000-0000000000b1",
    )  # fmt: skip
    vllm_db.commit()
    body = await (await client.delete(DEL, headers=auth_headers)).get_json()
    assert {(d["kind"], d["public_id"]) for d in body["deployments"]} == {
        (2, "00000000-0000-0000-0000-0000000000a1"),
        (3, "00000000-0000-0000-0000-0000000000b1"),
    }


async def test_delete_weights_ignores_and_never_reveals_other_orgs(
    client, vllm_db, both_flags, auth_headers, store
) -> None:
    """Another org's (or an unowned) reference neither blocks the delete nor appears anywhere."""
    root, _ = store
    path = _publish(root)
    mine = seed_backend(vllm_db)
    del mine
    other = seed_backend(vllm_db, org_id=2, name="o2")
    seed_deployment(vllm_db, other, org_id=2, model_ref=REF)
    foreign_ollama = vllm_db.ollama_deployments.insert(
        name="fo", endpoint_url="http://o", fleet_backend_id=other,
        public_id="00000000-0000-0000-0000-0000000000c1",
    )  # fmt: skip
    vllm_db.ollama_models.insert(deployment_id=foreign_ollama, model_name=REF)
    vllm_db.llamacpp_deployments.insert(
        name="orphan", model_name=REF, deployment_type="kubernetes",
        public_id="00000000-0000-0000-0000-0000000000d1",
    )  # fmt: skip
    vllm_db.commit()
    resp = await client.delete(DEL, headers=auth_headers)
    text = (await resp.get_data()).decode()
    assert resp.status_code == 200 and not path.exists()
    for foreign in ("c1", "d1", "org_id", "other"):
        assert f"0000000000{foreign}" not in text
    refs = model_store._references(vllm_db, 1, REF)
    assert refs == []


async def test_delete_weights_traversal_and_auth(
    client, vllm_db, both_flags, auth_headers, user_auth_headers, store
):
    """401 without JWT, 403 without fleet:admin, 400 for a traversal reference."""
    assert (await client.delete(DEL)).status_code == 401
    assert (await client.delete(DEL, headers=user_auth_headers)).status_code == 403
    bad = await client.delete("/api/v1/fleet/models/a/%2e%2e/b/weights", headers=auth_headers)
    assert bad.status_code in (400, 404)


# --- the Job entrypoint ------------------------------------------------------------------------


class _FakeHub:
    """A stand-in for huggingface_hub that writes files and can fail."""

    def __init__(self, fail: bool = False) -> None:
        """Choose whether the download raises."""
        self.fail = fail

    def HfApi(self) -> Any:  # noqa: N802 -- mirrors the hub API
        """A model_info provider with two sized files."""
        siblings = [SimpleNamespace(size=5), SimpleNamespace(size=7)]
        return SimpleNamespace(model_info=lambda *a, **k: SimpleNamespace(siblings=siblings))

    def snapshot_download(self, repo_id: str, local_dir: str, token: str | None) -> None:
        """Write two files (or fail)."""
        if self.fail:
            raise RuntimeError("401 for token hf_SECRET")
        Path(local_dir, "a.bin").write_bytes(b"12345")
        Path(local_dir, "b.bin").write_bytes(b"1234567")


def test_model_pull_publishes_digest_only_after_success(tmp_path, monkeypatch) -> None:
    """A good pull writes progress and the digest; the digest is a stable sha256 of the files."""
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    dest = tmp_path / "w"
    digest = model_pull.pull(REF, dest, hub=_FakeHub())
    assert (dest / ".digest").read_text() == digest and len(digest) == 64
    assert json.loads((dest / ".progress").read_text()) == {"bytes_done": 12, "bytes_total": 12}
    assert model_pull.compute_digest(dest) == digest
    assert model_store.weights_present(str(dest))
    assert model_store.read_progress(str(dest)) == (12, 12)


def test_model_pull_failure_publishes_no_digest_and_hides_token(
    tmp_path, monkeypatch, capsys
) -> None:
    """A failed pull leaves no marker (never mistaken for present) and exit code 1."""
    monkeypatch.setattr(
        model_pull, "pull", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hf_SECRET"))
    )
    assert model_pull.main(["--model-ref", REF, "--dest", str(tmp_path / "w")]) == 1
    assert "hf_SECRET" not in capsys.readouterr().out
    dest = tmp_path / "x"
    with pytest.raises(RuntimeError):
        model_pull.pull(REF, dest, hub=_FakeHub(fail=True))
    assert not (dest / ".digest").exists()


def test_model_pull_main_success(tmp_path, monkeypatch) -> None:
    """main() returns 0 when the pull publishes a digest."""
    monkeypatch.setattr(model_pull, "pull", lambda *a, **k: "d" * 64)
    assert model_pull.main(["--model-ref", REF, "--dest", str(tmp_path / "w")]) == 0


def test_read_progress_and_present_defaults(tmp_path) -> None:
    """Missing or corrupt progress reads as zero; a directory without a marker is not present."""
    assert model_store.read_progress(str(tmp_path)) == (0, 0)
    (tmp_path / ".progress").write_text("{not json")
    assert model_store.read_progress(str(tmp_path)) == (0, 0)
    assert model_store.weights_present(str(tmp_path)) is False
