"""Render assertions for the migration Job, six untemplated env vars, and image digests.

Covers gaps documented in docs/deployment/kubernetes.md's "Env vars the chart does
not template at all": the migration Job renders only when migrations.enabled,
carries the pre-install,pre-upgrade hook + weight ordering that must sort after the
(also-hook) Secret, uses the management image via python3 -m alembic (never the bare
alembic console-script binary, so it stays inside the Tetragon exec-allowlist), and
only ever needs DATABASE_URL — not the full app env; each of the six env vars lands
in exactly the container(s) whose Python code reads it; management.image.digest and
webui.image.digest render an `@sha256:` image reference when set, `:tag` otherwise,
matching the pre-existing proxy/postgres/valkey/ollama/llamacpp helpers.
"""

from tests.helm.conftest import find, render


def _env_map(container: dict) -> dict:
    """Flatten a container's env list into {name: value-or-valueFrom} for easy lookup."""
    out = {}
    for e in container.get("env", []):
        out[e["name"]] = e.get("value", e.get("valueFrom"))
    return out


class TestMigrationJobGating:
    """The Job is opt-out (migrations.enabled defaults true) but must be fully removable."""

    def test_job_renders_by_default(self):
        """values-alpha.yaml leaves migrations.enabled untouched, so the base default holds."""
        docs = render("values-alpha.yaml")
        job = find(docs, "Job", "waddleai-migration")
        assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"

    def test_job_absent_when_disabled(self):
        """migrations.enabled: false renders zero Job objects, not just an empty spec."""
        docs = render("values-alpha.yaml", {"migrations.enabled": "false"})
        assert not any(d["kind"] == "Job" for d in docs)


class TestMigrationJobHookOrdering:
    """The Job and the Secret it depends on are both pre-install,pre-upgrade hooks.

    The Secret's weight must sort strictly before the Job's — otherwise a fresh
    `helm install` dangles the Job's DATABASE_URL secretKeyRef (plain/non-hook
    resources like Secrets are only applied *after* all pre-install hooks finish).
    """

    def test_job_has_pre_install_pre_upgrade_hook(self):
        """The Job carries the pre-install,pre-upgrade hook with a delete policy."""
        docs = render("values-alpha.yaml")
        job = find(docs, "Job", "waddleai-migration")
        ann = job["metadata"]["annotations"]
        assert ann["helm.sh/hook"] == "pre-install,pre-upgrade"
        assert "hook-succeeded" in ann["helm.sh/hook-delete-policy"]
        assert "before-hook-creation" in ann["helm.sh/hook-delete-policy"]

    def test_secret_is_also_a_hook_with_earlier_weight(self):
        """The Secret is a hook too, survives past hook cleanup, and sorts before the Job."""
        docs = render("values-alpha.yaml")
        secret = find(docs, "Secret", "waddleai-secrets")
        job = find(docs, "Job", "waddleai-migration")
        secret_ann = secret["metadata"]["annotations"]
        job_ann = job["metadata"]["annotations"]
        assert secret_ann["helm.sh/hook"] == "pre-install,pre-upgrade"
        # hook-succeeded must NOT be on the Secret — it has to survive past the
        # hook phase for the (non-hook) Deployments to reference.
        assert "hook-succeeded" not in secret_ann["helm.sh/hook-delete-policy"]
        assert int(secret_ann["helm.sh/hook-weight"]) < int(job_ann["helm.sh/hook-weight"])


class TestMigrationJobCommandAndEnv:
    """Command must stay inside the management Tetragon exec-allowlist.

    Env must be scoped to exactly what alembic/env.py reads (DATABASE_URL), nothing
    else.
    """

    def test_command_execs_the_allowlisted_python3_not_bare_alembic(self):
        """Runs `python3 -m alembic`, not the /opt/venv/bin/alembic console script.

        The console script is a second, unlisted binary under
        tetragon.exec.components.management.allowedExecs.
        """
        docs = render("values-alpha.yaml")
        job = find(docs, "Job", "waddleai-migration")
        container = job["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == [
            "/opt/venv/bin/python3",
            "-m",
            "alembic",
            "upgrade",
            "head",
        ]

    def test_uses_management_image(self):
        """The Job's container image is the same tagged image as the management Deployment."""
        docs = render("values-alpha.yaml", {"management.image.tag": "test-tag"})
        job = find(docs, "Job", "waddleai-migration")
        mgmt = find(docs, "Deployment", "waddleai-management")
        job_image = job["spec"]["template"]["spec"]["containers"][0]["image"]
        mgmt_image = mgmt["spec"]["template"]["spec"]["containers"][0]["image"]
        assert job_image == mgmt_image
        assert "test-tag" in job_image

    def test_env_is_database_url_only(self):
        """The Job pulls in only DATABASE_URL.

        Not CACHE_HOST/CILIUM_TOPOLOGY/etc. — those would add a second
        hook-ordering dependency (the cilium-topology ConfigMap) for zero migration
        benefit.
        """
        docs = render("values-alpha.yaml")
        job = find(docs, "Job", "waddleai-migration")
        container = job["spec"]["template"]["spec"]["containers"][0]
        env = _env_map(container)
        assert set(env) == {"DATABASE_URL"}
        assert env["DATABASE_URL"]["secretKeyRef"] == {
            "name": "waddleai-secrets",
            "key": "database-url",
        }

    def test_job_container_securitycontext_matches_management_deployment(self):
        """The Job's container securityContext is identical to the management Deployment's."""
        docs = render("values-alpha.yaml")
        job = find(docs, "Job", "waddleai-migration")
        mgmt = find(docs, "Deployment", "waddleai-management")
        job_sc = job["spec"]["template"]["spec"]["containers"][0]["securityContext"]
        mgmt_sc = mgmt["spec"]["template"]["spec"]["containers"][0]["securityContext"]
        assert job_sc == mgmt_sc
        assert job_sc["runAsNonRoot"] is True
        assert job_sc["capabilities"]["drop"] == ["ALL"]


class TestSixPreviouslyUntemplatedEnvVars:
    """Each var lands in exactly the container(s) whose code reads it.

    Per docs/deployment/kubernetes.md's "Env vars the chart does not template at
    all".
    """

    def test_admin_initial_password_management_only(self):
        """services/management/app/config.py + extensions.py — management-only."""
        docs = render("values-alpha.yaml")
        mgmt = find(docs, "Deployment", "waddleai-management")
        proxy = find(docs, "Deployment", "waddleai-proxy")
        mgmt_env = _env_map(mgmt["spec"]["template"]["spec"]["containers"][0])
        proxy_env = _env_map(proxy["spec"]["template"]["spec"]["containers"][0])
        assert mgmt_env["ADMIN_INITIAL_PASSWORD"]["secretKeyRef"] == {
            "name": "waddleai-secrets",
            "key": "admin-initial-password",
        }
        assert "ADMIN_INITIAL_PASSWORD" not in proxy_env

    def test_credential_encryption_key_management_only(self):
        """shared/security/credential_encryption.py is only imported by management code."""
        docs = render("values-alpha.yaml")
        mgmt = find(docs, "Deployment", "waddleai-management")
        proxy = find(docs, "Deployment", "waddleai-proxy")
        mgmt_env = _env_map(mgmt["spec"]["template"]["spec"]["containers"][0])
        proxy_env = _env_map(proxy["spec"]["template"]["spec"]["containers"][0])
        assert mgmt_env["CREDENTIAL_ENCRYPTION_KEY"]["secretKeyRef"] == {
            "name": "waddleai-secrets",
            "key": "credential-encryption-key",
        }
        assert "CREDENTIAL_ENCRYPTION_KEY" not in proxy_env

    def test_license_key_reaches_both_management_and_proxy(self):
        """shared/licensing/python_client.py is read by both services."""
        docs = render("values-alpha.yaml")
        mgmt = find(docs, "Deployment", "waddleai-management")
        proxy = find(docs, "Deployment", "waddleai-proxy")
        mgmt_env = _env_map(mgmt["spec"]["template"]["spec"]["containers"][0])
        proxy_env = _env_map(proxy["spec"]["template"]["spec"]["containers"][0])
        for env in (mgmt_env, proxy_env):
            assert env["LICENSE_KEY"]["secretKeyRef"] == {
                "name": "waddleai-secrets",
                "key": "license-key",
            }

    def test_license_server_url_reaches_both_with_default(self):
        """LICENSE_SERVER_URL renders as a plain value (not a secretKeyRef) on both containers."""
        docs = render("values-alpha.yaml")
        mgmt = find(docs, "Deployment", "waddleai-management")
        proxy = find(docs, "Deployment", "waddleai-proxy")
        mgmt_env = _env_map(mgmt["spec"]["template"]["spec"]["containers"][0])
        proxy_env = _env_map(proxy["spec"]["template"]["spec"]["containers"][0])
        for env in (mgmt_env, proxy_env):
            assert env["LICENSE_SERVER_URL"] == "https://license.penguintech.io"

    def test_ner_spacy_model_reaches_both_with_lg_default(self):
        """NER_SPACY_MODEL defaults to en_core_web_lg on both management and proxy."""
        docs = render("values-alpha.yaml")
        mgmt = find(docs, "Deployment", "waddleai-management")
        proxy = find(docs, "Deployment", "waddleai-proxy")
        mgmt_env = _env_map(mgmt["spec"]["template"]["spec"]["containers"][0])
        proxy_env = _env_map(proxy["spec"]["template"]["spec"]["containers"][0])
        for env in (mgmt_env, proxy_env):
            assert env["NER_SPACY_MODEL"] == "en_core_web_lg"

    def test_ner_allow_download_defaults_false_on_both(self):
        """Images must never download models at runtime by default."""
        docs = render("values-alpha.yaml")
        mgmt = find(docs, "Deployment", "waddleai-management")
        proxy = find(docs, "Deployment", "waddleai-proxy")
        mgmt_env = _env_map(mgmt["spec"]["template"]["spec"]["containers"][0])
        proxy_env = _env_map(proxy["spec"]["template"]["spec"]["containers"][0])
        for env in (mgmt_env, proxy_env):
            assert env["WADDLEAI_NER_ALLOW_DOWNLOAD"] == "false"


class TestImageDigestHelpers:
    """management/webui previously had no `.image.digest` branch.

    Only proxy/postgres/valkey/ollama/llamacpp did — both must now render
    `@sha256:` when set, `:tag` otherwise, matching that pattern.
    """

    def test_management_digest_set_renders_at_sha256(self):
        """management.image.digest set renders the image as name@sha256:..., not :tag."""
        digest = "sha256:" + "a" * 64
        docs = render("values-alpha.yaml", {"management.image.digest": digest})
        mgmt = find(docs, "Deployment", "waddleai-management")
        image = mgmt["spec"]["template"]["spec"]["containers"][0]["image"]
        assert image.endswith(f"@{digest}")
        assert ":alpha-latest" not in image

    def test_management_digest_unset_renders_tag(self):
        """management.image.digest unset falls back to the :tag form."""
        docs = render("values-alpha.yaml")
        mgmt = find(docs, "Deployment", "waddleai-management")
        image = mgmt["spec"]["template"]["spec"]["containers"][0]["image"]
        assert "@sha256:" not in image
        assert image.endswith(":alpha-latest")

    def test_webui_digest_set_renders_at_sha256(self):
        """webui.image.digest set renders the image as name@sha256:..., not :tag."""
        digest = "sha256:" + "b" * 64
        docs = render("values-alpha.yaml", {"webui.image.digest": digest})
        webui = find(docs, "Deployment", "waddleai-webui")
        image = webui["spec"]["template"]["spec"]["containers"][0]["image"]
        assert image.endswith(f"@{digest}")

    def test_webui_digest_unset_renders_tag(self):
        """webui.image.digest unset falls back to the :tag form."""
        docs = render("values-alpha.yaml")
        webui = find(docs, "Deployment", "waddleai-webui")
        image = webui["spec"]["template"]["spec"]["containers"][0]["image"]
        assert "@sha256:" not in image
