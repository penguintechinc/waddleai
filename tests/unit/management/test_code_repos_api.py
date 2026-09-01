"""Tests for /api/v1/code-repos: register/list/get/delete/reindex + webhook, org isolation."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

import pytest

from tests.unit.management.conftest import make_dal_row, make_select_result


@pytest.fixture(autouse=True)
def _stub_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WADDLEAI_STUB_UPSTREAM", "1")


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")


def _repo_row(**overrides: object):
    fields = {
        "id": 1,
        "org_id": 1,
        "name": "waddleai",
        "source_url": "https://github.com/penguintechinc/waddleai.git",
        "credentials_ref": None,
        "webhook_secret": "enc:not-a-real-fernet-token",  # noqa: S105 -- test fixture
        "index_status": "pending",
        "last_commit": None,
        "created_at": datetime(2026, 1, 1, 0, 0, 0),
        "updated_at": datetime(2026, 1, 1, 0, 0, 0),
    }
    fields.update(overrides)
    return make_dal_row(**fields)


class TestCreateCodeRepo:
    """POST /api/v1/code-repos: registration + one-time webhook secret."""

    async def test_create_returns_webhook_secret_once(
        self, client, rm_auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful create returns the secret once; the stored value is Fernet-encrypted."""
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-only-encryption-key-not-real")  # noqa: S105
        app_mock_db.code_repos.insert.return_value = 1
        app_mock_db.return_value.select.return_value = make_select_result([])  # no existing repo

        resp = await client.post(
            "/api/v1/code-repos",
            headers=rm_auth_headers,
            json={
                "name": "waddleai",
                "source_url": "https://github.com/penguintechinc/waddleai.git",
            },
        )

        assert resp.status_code == 201
        body = await resp.get_json()
        assert body["name"] == "waddleai"
        assert "webhook_secret" in body and len(body["webhook_secret"]) > 20
        # The stored value passed to insert() must be Fernet-encrypted (enc: prefix),
        # never the plaintext secret returned to the caller.
        insert_kwargs = app_mock_db.code_repos.insert.call_args.kwargs
        assert insert_kwargs["webhook_secret"].startswith("enc:")
        assert insert_kwargs["webhook_secret"] != body["webhook_secret"]

    async def test_create_requires_code_repo_write_scope(self, client, user_auth_headers) -> None:
        """A caller without CODE_REPO_WRITE is refused (403)."""
        resp = await client.post(
            "/api/v1/code-repos",
            headers=user_auth_headers,
            json={"name": "x", "source_url": "https://example.com/x.git"},
        )
        assert resp.status_code == 403

    async def test_create_requires_auth(self, client) -> None:
        """No Authorization header at all -> 401."""
        resp = await client.post(
            "/api/v1/code-repos", json={"name": "x", "source_url": "https://example.com/x.git"}
        )
        assert resp.status_code == 401

    async def test_create_returns_404_when_flag_off(
        self, client, rm_auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag-off path never touches the DB -- 404, not 201/500."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "0")
        resp = await client.post(
            "/api/v1/code-repos",
            headers=rm_auth_headers,
            json={"name": "x", "source_url": "https://example.com/x.git"},
        )
        assert resp.status_code == 404

    async def test_create_requires_name_and_source_url(self, client, rm_auth_headers) -> None:
        """Missing required fields -> 400, never a 500 from a downstream KeyError."""
        resp = await client.post("/api/v1/code-repos", headers=rm_auth_headers, json={"name": "x"})
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "source_url",
        [
            "ext::sh -c id",  # git's shell-out transport -- RCE vector via CodeRagWorker's clone
            "file:///etc/passwd",
            "fd::0",
            "ftp://example.com/x.git",
        ],
    )
    async def test_create_rejects_dangerous_source_url_schemes(
        self, client, rm_auth_headers, source_url: str
    ) -> None:
        """Only https:// and SSH transports are accepted -- blocks git's ext::/file:// RCE class."""
        resp = await client.post(
            "/api/v1/code-repos",
            headers=rm_auth_headers,
            json={"name": "x", "source_url": source_url},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "source_url",
        [
            "git@github.com:penguintechinc/waddleai.git",
            "ssh://git@github.com/penguintechinc/waddleai.git",
        ],
    )
    async def test_create_accepts_ssh_source_url(
        self, client, rm_auth_headers, app_mock_db, source_url: str
    ) -> None:
        """SSH-form source URLs (both scp-like and ssh://) are accepted, not just https://."""
        app_mock_db.code_repos.insert.return_value = 1
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.post(
            "/api/v1/code-repos",
            headers=rm_auth_headers,
            json={"name": "x", "source_url": source_url},
        )
        assert resp.status_code == 201

    async def test_create_duplicate_name_in_org_returns_409(
        self, client, rm_auth_headers, app_mock_db
    ) -> None:
        """A second repo with the same name in the same org is rejected, not silently duplicated."""
        app_mock_db.return_value.select.return_value = make_select_result([_repo_row()])

        resp = await client.post(
            "/api/v1/code-repos",
            headers=rm_auth_headers,
            json={
                "name": "waddleai",
                "source_url": "https://github.com/penguintechinc/waddleai.git",
            },
        )
        assert resp.status_code == 409


class TestListAndGetCodeRepo:
    """GET /api/v1/code-repos and GET /api/v1/code-repos/<id>: org-scoped, secret never exposed."""

    async def test_list_never_exposes_webhook_secret(
        self, client, rm_auth_headers, app_mock_db
    ) -> None:
        """The list response's serialized fields never include webhook_secret or credentials_ref."""
        app_mock_db.return_value.select.return_value = make_select_result([_repo_row()])

        resp = await client.get("/api/v1/code-repos", headers=rm_auth_headers)

        assert resp.status_code == 200
        body = await resp.get_json()
        assert len(body["repos"]) == 1
        assert "webhook_secret" not in body["repos"][0]
        assert "credentials_ref" not in body["repos"][0]

    async def test_get_own_org_repo_succeeds_and_hides_secret(
        self, client, rm_auth_headers, app_mock_db
    ) -> None:
        """A repo within the caller's org is returned; the secret is never present on GET."""
        app_mock_db.return_value.select.return_value = make_select_result(
            [_repo_row(id=1, org_id=1)]
        )

        resp = await client.get("/api/v1/code-repos/1", headers=rm_auth_headers)

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["id"] == 1
        assert "webhook_secret" not in body
        assert "credentials_ref" not in body

    async def test_get_outside_org_returns_404_not_403(
        self, client, rm_auth_headers, app_mock_db
    ) -> None:
        """A repo id belonging to a different org resolves to 404 -- never leaks existence."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/code-repos/999", headers=rm_auth_headers)

        assert resp.status_code == 404

    async def test_list_returns_404_when_flag_off(
        self, client, rm_auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag-gated: list is unreachable with the flag off."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "0")
        resp = await client.get("/api/v1/code-repos", headers=rm_auth_headers)
        assert resp.status_code == 404

    async def test_get_single_returns_404_when_flag_off(
        self, client, rm_auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag-gated: get-by-id is unreachable with the flag off, even for an existing repo."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "0")
        resp = await client.get("/api/v1/code-repos/1", headers=rm_auth_headers)
        assert resp.status_code == 404


class TestDeleteCodeRepoIDORSafe:
    """DELETE /api/v1/code-repos/<id>: scope-gated, org-scoped, IDOR-safe."""

    async def test_delete_requires_code_repo_write_scope(self, client, user_auth_headers) -> None:
        """A caller without CODE_REPO_WRITE is refused (403)."""
        resp = await client.delete("/api/v1/code-repos/1", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_delete_returns_404_when_flag_off(
        self, client, rm_auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag-gated: delete is unreachable with the flag off, even for an existing repo."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "0")
        app_mock_db.return_value.select.return_value = make_select_result(
            [_repo_row(id=1, org_id=1)]
        )

        resp = await client.delete("/api/v1/code-repos/1", headers=rm_auth_headers)

        assert resp.status_code == 404
        app_mock_db.return_value.delete.assert_not_called()

    async def test_delete_outside_org_returns_404(
        self, client, rm_auth_headers, app_mock_db
    ) -> None:
        """A caller cannot delete another org's repo -- 404, not 403/200."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.delete("/api/v1/code-repos/999", headers=rm_auth_headers)

        assert resp.status_code == 404
        app_mock_db.return_value.delete.assert_not_called()

    async def test_delete_own_org_repo_succeeds(self, client, rm_auth_headers, app_mock_db) -> None:
        """A repo within the caller's own org is deleted successfully."""
        app_mock_db.return_value.select.return_value = make_select_result(
            [_repo_row(id=1, org_id=1)]
        )

        resp = await client.delete("/api/v1/code-repos/1", headers=rm_auth_headers)

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "deleted"


class TestReindexCodeRepo:
    """POST /api/v1/code-repos/<id>/reindex: scope-gated, IDOR-guarded before the worker runs."""

    async def test_reindex_requires_code_repo_write_scope(self, client, user_auth_headers) -> None:
        """A caller without CODE_REPO_WRITE is refused (403)."""
        resp = await client.post("/api/v1/code-repos/1/reindex", headers=user_auth_headers, json={})
        assert resp.status_code == 403

    async def test_reindex_returns_404_when_flag_off(
        self, client, rm_auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag off -> 404 for contract consistency with every sibling route (never a 200 skip)."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "0")
        app_mock_db.return_value.select.return_value = make_select_result(
            [_repo_row(id=1, org_id=1)]
        )
        called = False

        def _fail_if_called(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("create_coderag_worker must not be called with the flag off")

        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.create_coderag_worker", _fail_if_called
        )

        resp = await client.post("/api/v1/code-repos/1/reindex", headers=rm_auth_headers, json={})

        assert resp.status_code == 404
        assert called is False

    async def test_reindex_outside_org_returns_404_never_triggers_worker(
        self, client, rm_auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repo_id outside the caller's org 404s before create_coderag_worker() is ever called."""
        app_mock_db.return_value.select.return_value = make_select_result([])
        called = False

        def _fail_if_called(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("create_coderag_worker must not be called for an out-of-org repo")

        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.create_coderag_worker", _fail_if_called
        )

        resp = await client.post("/api/v1/code-repos/999/reindex", headers=rm_auth_headers, json={})

        assert resp.status_code == 404
        assert called is False

    async def test_reindex_own_org_repo_triggers_worker(
        self, client, rm_auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repo within the caller's org triggers the worker and returns its index result."""
        app_mock_db.return_value.select.return_value = make_select_result(
            [_repo_row(id=1, org_id=1)]
        )

        class _FakeResult:
            repo_id = 1
            branch_ref = "main"
            index_status = "indexed"
            last_commit = "abc123"
            files_changed: list[str] = []
            files_deleted: list[str] = []
            error = None

        class _FakeWorker:
            async def index(self, repo_id, branch=None, trigger="manual"):
                assert repo_id == 1
                return _FakeResult()

        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.create_coderag_worker",
            lambda *a, **k: _FakeWorker(),
        )

        resp = await client.post("/api/v1/code-repos/1/reindex", headers=rm_auth_headers, json={})

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["index_status"] == "indexed"


class TestReindexAll:
    """POST /api/v1/code-repos/reindex-all: scope-gated, org-scoped bulk reindex, not cross-org."""

    async def test_reindex_all_requires_code_repo_write_scope(
        self, client, user_auth_headers
    ) -> None:
        """A caller without CODE_REPO_WRITE is refused (403)."""
        resp = await client.post(
            "/api/v1/code-repos/reindex-all", headers=user_auth_headers, json={}
        )
        assert resp.status_code == 403

    async def test_reindex_all_returns_404_when_flag_off(
        self, client, rm_auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag off -> 404 for contract consistency with every sibling route (never a 200 skip)."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "0")
        called = False

        def _fail_if_called(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("create_coderag_worker must not be called with the flag off")

        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.create_coderag_worker", _fail_if_called
        )

        resp = await client.post("/api/v1/code-repos/reindex-all", headers=rm_auth_headers, json={})

        assert resp.status_code == 404
        assert called is False

    def test_reindex_all_never_calls_unscoped_run_scheduled(self) -> None:
        """Static regression guard: this route must never invoke CodeRagWorker.run_scheduled().

        run_scheduled() has no org filter -- it's the genuine cross-tenant
        cron sweep (reserved for a system/cron-authed internal path, not
        this per-org CODE_REPO_WRITE-scoped route). Parses the module's AST
        and asserts no ``Call`` node's attribute is ``run_scheduled`` --
        immune to the name merely appearing in a docstring/comment (as it
        does here, explaining why it's avoided), unlike a plain text/grep
        check. Proven via AST rather than the mock DB, which cannot
        distinguish an org-filtered query from an unfiltered one -- a hard
        source-level guard closes that gap regardless of mock fidelity.
        """
        import ast
        import inspect

        from services.management.app.api.v1 import code_repos

        tree = ast.parse(inspect.getsource(code_repos))
        call_attrs = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "run_scheduled" not in call_attrs

    async def test_reindex_all_indexes_only_the_callers_org_repos(
        self, client, rm_auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only repo ids from the org-filtered query are indexed -- never a system-wide sweep."""
        app_mock_db.return_value.select.return_value = make_select_result(
            [_repo_row(id=1, org_id=1), _repo_row(id=2, org_id=1)]
        )
        indexed_repo_ids: list[int] = []

        class _FakeResult:
            def __init__(self, repo_id: int) -> None:
                self.repo_id = repo_id
                self.branch_ref = "main"
                self.index_status = "indexed"
                self.error = None

        class _FakeWorker:
            # Deliberately has no run_scheduled() -- calling it would raise
            # AttributeError, failing this test loudly (belt-and-suspenders
            # alongside the static guard above).
            async def index(self, repo_id, branch=None, trigger="manual"):
                indexed_repo_ids.append(repo_id)
                return _FakeResult(repo_id)

        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.create_coderag_worker",
            lambda *a, **k: _FakeWorker(),
        )

        resp = await client.post("/api/v1/code-repos/reindex-all", headers=rm_auth_headers, json={})

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["indexed"] == 2
        assert sorted(indexed_repo_ids) == [1, 2]

    async def test_reindex_all_handles_empty_org_without_touching_worker_index(
        self, client, rm_auth_headers, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller whose org has no repos gets an empty summary, not an error."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        class _FakeWorker:
            pass  # index()/run_scheduled() must never be called -- no repo ids to index

        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.create_coderag_worker",
            lambda *a, **k: _FakeWorker(),
        )

        resp = await client.post("/api/v1/code-repos/reindex-all", headers=rm_auth_headers, json={})

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["indexed"] == 0


class TestWebhook:
    """POST /api/v1/code-repos/<id>/webhook: HMAC-verified per repo_id, fails closed, unscoped."""

    def _sign(self, secret: str, body: bytes) -> str:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    async def test_webhook_rejects_bad_signature(self, client, app_mock_db) -> None:
        """An invalid HMAC signature is rejected (401), never triggers a re-index."""
        row = _repo_row(
            id=1,
            org_id=7,
            webhook_secret="whs_test_not_the_real_secret",  # noqa: S106 -- test fixture, not a real secret
        )
        app_mock_db.return_value.select.return_value = make_select_result([row])
        body = json.dumps({"ref": "refs/heads/main"}).encode()

        resp = await client.post(
            "/api/v1/code-repos/1/webhook",
            data=body,
            headers={"X-Hub-Signature-256": "sha256=wrong", "Content-Type": "application/json"},
        )

        assert resp.status_code == 401

    async def test_webhook_unknown_repo_returns_404(self, client, app_mock_db) -> None:
        """A repo_id with no matching registration is rejected (404), before any signature check."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.post(
            "/api/v1/code-repos/999/webhook",
            data=json.dumps({"ref": "refs/heads/main"}).encode(),
            headers={"Content-Type": "application/json"},
        )

        assert resp.status_code == 404

    async def test_webhook_missing_secret_fails_closed(self, client, app_mock_db) -> None:
        """A repo with no webhook_secret configured must reject every signature, never accept."""
        row = _repo_row(webhook_secret=None)
        app_mock_db.return_value.select.return_value = make_select_result([row])
        body = json.dumps({"ref": "refs/heads/main"}).encode()

        resp = await client.post(
            "/api/v1/code-repos/1/webhook",
            data=body,
            headers={"X-Hub-Signature-256": "sha256=anything", "Content-Type": "application/json"},
        )

        assert resp.status_code == 401

    async def test_webhook_undecryptable_secret_fails_closed(
        self, client, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A corrupted/undecryptable stored secret must 401, never 500."""
        row = _repo_row(webhook_secret="enc:corrupted-ciphertext")  # noqa: S106 -- ciphertext, not a real secret
        app_mock_db.return_value.select.return_value = make_select_result([row])

        def _raise(*_args, **_kwargs):
            raise ValueError("Failed to decrypt credential — wrong encryption key?")

        monkeypatch.setattr("services.management.app.api.v1.code_repos.decrypt_credential", _raise)

        body = json.dumps({"ref": "refs/heads/main"}).encode()

        resp = await client.post(
            "/api/v1/code-repos/1/webhook",
            data=body,
            headers={"X-Hub-Signature-256": "sha256=anything", "Content-Type": "application/json"},
        )

        assert resp.status_code == 401

    async def test_webhook_valid_signature_accepted(
        self, client, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A correctly-signed payload for the path's repo_id is accepted (202) and indexes it."""
        secret = "the-real-plaintext-secret"  # noqa: S105 -- test fixture
        row = _repo_row(id=1, org_id=1, webhook_secret=f"enc:{secret}")
        app_mock_db.return_value.select.return_value = make_select_result([row])
        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.decrypt_credential", lambda *_a, **_k: secret
        )

        triggered: list[tuple[int, str | None]] = []

        class _FakeWorker:
            async def index(self, repo_id, branch=None, trigger="manual"):
                triggered.append((repo_id, branch))

        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.create_coderag_worker",
            lambda *a, **k: _FakeWorker(),
        )

        payload = {
            "repository": {"clone_url": "https://github.com/penguintechinc/waddleai.git"},
            "ref": "refs/heads/main",
        }
        body = json.dumps(payload).encode()
        signature = self._sign(secret, body)

        resp = await client.post(
            "/api/v1/code-repos/1/webhook",
            data=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )

        assert resp.status_code == 202
        resp_body = await resp.get_json()
        assert resp_body["repo_id"] == 1
        assert resp_body["branch"] == "main"
        assert triggered == [(1, "main")]

    async def test_webhook_no_repository_or_ref_still_indexes_default_branch(
        self, client, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """repository.clone_url is no longer required -- repo identity comes from the path."""
        secret = "the-real-plaintext-secret"  # noqa: S105 -- test fixture
        row = _repo_row(id=1, org_id=1, webhook_secret=f"enc:{secret}")
        app_mock_db.return_value.select.return_value = make_select_result([row])
        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.decrypt_credential", lambda *_a, **_k: secret
        )

        triggered: list[tuple[int, str | None]] = []

        class _FakeWorker:
            async def index(self, repo_id, branch=None, trigger="manual"):
                triggered.append((repo_id, branch))

        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.create_coderag_worker",
            lambda *a, **k: _FakeWorker(),
        )

        body = json.dumps({}).encode()  # no repository key, no ref -- both optional now
        signature = self._sign(secret, body)

        resp = await client.post(
            "/api/v1/code-repos/1/webhook",
            data=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )

        assert resp.status_code == 202
        resp_body = await resp.get_json()
        assert resp_body["branch"] == "main"
        # branch=None passed to worker.index() -- index() applies its own None -> "main" fallback.
        assert triggered == [(1, None)]

    async def test_webhook_malformed_json_returns_400(self, client) -> None:
        """A non-JSON body never reaches the DB lookup or signature check -- 400."""
        resp = await client.post(
            "/api/v1/code-repos/1/webhook",
            data=b"not json{{{",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    async def test_webhook_returns_404_when_flag_off(
        self, client, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A verified signature for a flag-off org still 404s -- the webhook never indexes."""
        secret = "the-real-plaintext-secret"  # noqa: S105 -- test fixture
        row = _repo_row(id=1, org_id=1, webhook_secret=f"enc:{secret}")
        app_mock_db.return_value.select.return_value = make_select_result([row])
        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.decrypt_credential", lambda *_a, **_k: secret
        )
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "0")

        body = json.dumps({"ref": "refs/heads/main"}).encode()
        signature = self._sign(secret, body)

        resp = await client.post(
            "/api/v1/code-repos/1/webhook",
            data=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )

        assert resp.status_code == 404

    async def test_webhook_requires_no_auth_header(self, client, app_mock_db) -> None:
        """The webhook route is reachable with no Authorization header (HMAC-verified, not JWT)."""
        app_mock_db.return_value.select.return_value = make_select_result([])
        resp = await client.post(
            "/api/v1/code-repos/999/webhook",
            data=json.dumps({"ref": "refs/heads/main"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        # Reaches route logic (404 for unknown repo id), not blocked at 401 for missing auth.
        assert resp.status_code == 404


class TestWebhookMultiOrgSameSourceURL:
    """Regression: two orgs registering the same source_url must never cross-verify or cross-index.

    Prior to the per-repo-path fix, the webhook route resolved the target
    repo by ``db.code_repos.source_url == clone_url`` with ``.first()`` --
    but ``source_url`` is not unique across orgs (only ``(org_id, name)``
    is). Two tenants registering the same clone URL meant the webhook
    always matched one arbitrary (first-registering) org's row, verified
    against that org's secret, and silently 401'd every other org sharing
    the URL -- their push-triggered reindexing never fired, with no signal
    why. Identifying the repo by path param (``/code-repos/<id>/webhook``)
    removes the ambiguity: each org configures its push provider with its
    own webhook URL, so there is no shared resolution step left to collide.
    """

    def _sign(self, secret: str, body: bytes) -> str:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    async def test_webhook_to_org_b_path_verifies_against_org_b_secret_and_indexes_org_b(
        self, client, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same clone URL registered by both orgs; org B's webhook path indexes org B's repo id."""
        shared_url = "https://github.com/penguintechinc/waddleai.git"
        secret_b = "org-b-secret"  # noqa: S105 -- test fixture
        row_b = _repo_row(id=2, org_id=2, source_url=shared_url, webhook_secret=f"enc:{secret_b}")
        # The route now fetches strictly by db.code_repos.id == repo_id (the
        # path param) -- returning org B's row here stands in for "id=2
        # resolves to org B's registration" regardless of org A also having
        # registered the identical shared_url under a different id. That
        # source_url collision is exactly the ambiguity the fix removes.
        app_mock_db.return_value.select.return_value = make_select_result([row_b])
        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.decrypt_credential",
            lambda *_a, **_k: secret_b,
        )

        indexed: list[int] = []

        class _FakeWorker:
            async def index(self, repo_id, branch=None, trigger="manual"):
                indexed.append(repo_id)

        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.create_coderag_worker",
            lambda *a, **k: _FakeWorker(),
        )

        payload = {"repository": {"clone_url": shared_url}, "ref": "refs/heads/main"}
        body = json.dumps(payload).encode()
        signature = self._sign(secret_b, body)

        resp = await client.post(
            "/api/v1/code-repos/2/webhook",
            data=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )

        assert resp.status_code == 202
        resp_body = await resp.get_json()
        assert resp_body["repo_id"] == 2
        assert indexed == [2]  # never org A's id, even though both share source_url

    async def test_webhook_org_a_secret_against_org_b_path_fails_closed(
        self, client, app_mock_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Signing with org A's secret against org B's repo id never verifies -- 401, no index."""
        shared_url = "https://github.com/penguintechinc/waddleai.git"
        secret_a = "org-a-secret"  # noqa: S105 -- test fixture
        secret_b = "org-b-secret"  # noqa: S105 -- test fixture
        row_b = _repo_row(id=2, org_id=2, source_url=shared_url, webhook_secret=f"enc:{secret_b}")
        app_mock_db.return_value.select.return_value = make_select_result([row_b])
        # Org B's row always decrypts to org B's own secret -- there is no
        # code path where a caller's chosen signing key changes what the
        # server decrypts for a given repo_id.
        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.decrypt_credential",
            lambda *_a, **_k: secret_b,
        )

        indexed: list[int] = []

        class _FakeWorker:
            async def index(self, repo_id, branch=None, trigger="manual"):
                indexed.append(repo_id)

        monkeypatch.setattr(
            "services.management.app.api.v1.code_repos.create_coderag_worker",
            lambda *a, **k: _FakeWorker(),
        )

        payload = {"repository": {"clone_url": shared_url}, "ref": "refs/heads/main"}
        body = json.dumps(payload).encode()
        signature = self._sign(secret_a, body)  # signed with the WRONG org's secret

        resp = await client.post(
            "/api/v1/code-repos/2/webhook",
            data=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )

        assert resp.status_code == 401
        assert indexed == []
