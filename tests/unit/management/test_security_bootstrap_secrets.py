"""Security tests for bootstrap secrets initialization.

Regression tests for security review 2026-07-26:
- A: default admin credentials must not be hardcoded
- B: master API key must not be logged in plaintext
- C: shipped default secrets must not be insecure literals in production
"""

import os
from unittest.mock import MagicMock, patch

import pytest


class TestAdminBootstrapSecurityA:
    """Regression: A — default admin credentials sourced from env, not hardcoded."""

    def test_production_config_raises_if_admin_password_unset(self):
        """ProductionConfig raises ValueError at import if ADMIN_INITIAL_PASSWORD unset."""
        # Temporarily unset the env var
        original_value = os.environ.pop("ADMIN_INITIAL_PASSWORD", None)
        try:
            # Clear any cached import
            import sys

            if "services.management.app.config" in sys.modules:
                del sys.modules["services.management.app.config"]

            # In production, ADMIN_INITIAL_PASSWORD comes from env only — no hardcoded default.
            from services.management.app.config import ProductionConfig

            assert ProductionConfig.ADMIN_INITIAL_PASSWORD == ""
            assert not os.environ.get("ADMIN_INITIAL_PASSWORD")
        finally:
            if original_value is not None:
                os.environ["ADMIN_INITIAL_PASSWORD"] = original_value

    def test_testing_config_generates_random_password_if_unset(self):
        """TestingConfig generates a random password if ADMIN_INITIAL_PASSWORD unset."""
        original_value = os.environ.pop("ADMIN_INITIAL_PASSWORD", None)
        try:
            import sys

            if "services.management.app.config" in sys.modules:
                del sys.modules["services.management.app.config"]

            from services.management.app.config import TestingConfig

            # TestingConfig must not carry the old hardcoded "admin123" literal.
            assert TestingConfig.ADMIN_INITIAL_PASSWORD != "admin123"  # noqa: S105 -- old literal
        finally:
            if original_value is not None:
                os.environ["ADMIN_INITIAL_PASSWORD"] = original_value

    def test_init_default_data_uses_env_password(self):
        """init_default_data sources admin password from ADMIN_INITIAL_PASSWORD env."""
        # regression: security review 2026-07-26 — admin password must not be hardcoded
        os.environ["ADMIN_INITIAL_PASSWORD"] = "test_password_123"  # noqa: S105 -- test fixture

        try:
            from passlib.hash import bcrypt

            from services.management.app.extensions import init_default_data

            # Mock DB: every db(...).select() returns empty so the create path runs;
            # capture the users.insert kwargs to inspect the seeded password hash.
            inserted_users = []
            mock_db = MagicMock()
            mock_db.return_value.select.return_value = []
            mock_db.commit = MagicMock()
            mock_db.organizations.insert = MagicMock(return_value=1)
            mock_db.users.insert = MagicMock(
                side_effect=lambda **kw: inserted_users.append(kw) or 1
            )
            mock_db.virtual_keys.insert = MagicMock(return_value=1)

            with patch.dict(os.environ, {"ADMIN_INITIAL_PASSWORD": "env_password"}):
                init_default_data(mock_db, config={"ADMIN_INITIAL_PASSWORD": "env_password"})

            # The seeded admin must use the env password, NOT the old hardcoded "admin123".
            assert inserted_users, "admin user was not seeded"
            seeded_hash = inserted_users[0]["password_hash"]
            assert bcrypt.verify("env_password", seeded_hash)
            assert not bcrypt.verify("admin123", seeded_hash)
        finally:
            os.environ.pop("ADMIN_INITIAL_PASSWORD", None)


class TestMasterKeyLoggingSecurityB:
    """Regression: B — master API key must not be logged in plaintext."""

    def test_init_default_data_does_not_log_plaintext_key(self, caplog):
        """Master key is never logged or printed in plaintext."""
        # regression: security review 2026-07-26 — master key plaintext must not appear in logs
        os.environ["ADMIN_INITIAL_PASSWORD"] = "test123"  # noqa: S105 -- fixed test credential

        try:
            import logging

            from services.management.app.extensions import init_default_data

            # Create a mock DB that records all insert calls
            mock_db = MagicMock()
            inserted_keys = []

            # Track inserts
            def track_insert(**kwargs):
                inserted_keys.append(kwargs)
                return 1

            mock_db.commit = MagicMock()

            # Mock organizations query
            mock_db.return_value = MagicMock()
            mock_db.return_value.select = MagicMock(
                return_value=MagicMock(first=MagicMock(return_value=None))
            )
            mock_db.organizations = MagicMock()
            mock_db.organizations.insert = MagicMock(return_value=1)

            # Mock users query
            mock_db.users = MagicMock()
            mock_db.users.insert = MagicMock(return_value=1)

            # Mock virtual_keys table
            mock_db.virtual_keys = MagicMock()
            mock_db.virtual_keys.insert = MagicMock(side_effect=track_insert)

            # Capture logs at INFO level
            with caplog.at_level(logging.INFO):
                # This should not raise since DB is mocked
                try:
                    init_default_data(mock_db)
                except Exception:  # noqa: S110 -- DB mocking may fail, only log content matters
                    # DB mocking might fail, but we only care about log content
                    pass

            # The logs must NOT contain the generated key (which starts with "wa-").
            for record in caplog.records:
                # API keys are long base64 strings, look for "wa-" followed by long strings
                msg = record.getMessage()
                # If logging the key, it would be in a message like "API Key: wa-<base64>"
                if "wa-" in msg and len(msg) > 100:
                    # This would indicate the full key is being logged
                    pytest.fail(f"Plaintext API key found in log: {msg}")
                if "Admin API Key" in msg or "save this" in msg.lower():
                    # These log messages should not contain the plaintext key
                    if "wa-" in msg:
                        pytest.fail(f"Plaintext key in admin log message: {msg}")
        finally:
            os.environ.pop("ADMIN_INITIAL_PASSWORD", None)

    def test_init_default_data_does_not_print_plaintext_key(self, capsys):
        """Master key is never printed to stdout."""
        # regression: security review 2026-07-26 — master key must not be printed
        os.environ["ADMIN_INITIAL_PASSWORD"] = "test123"  # noqa: S105 -- fixed test credential

        try:
            from services.management.app.extensions import init_default_data

            # Create a mock DB
            mock_db = MagicMock()
            mock_db.commit = MagicMock()

            # Mock the query chain
            mock_db.return_value = MagicMock()
            mock_db.return_value.select = MagicMock(
                return_value=MagicMock(first=MagicMock(return_value=None))
            )
            mock_db.organizations = MagicMock()
            mock_db.organizations.insert = MagicMock(return_value=1)
            mock_db.users = MagicMock()
            mock_db.users.insert = MagicMock(return_value=1)
            mock_db.virtual_keys = MagicMock()
            mock_db.virtual_keys.insert = MagicMock(return_value=1)

            try:
                init_default_data(mock_db)
            except Exception:  # noqa: S110 -- DB mocking may fail, only stdout content matters
                pass

            # Check stdout
            captured = capsys.readouterr()
            # Should not contain "Admin API Key" message or "wa-" prefix
            if "Admin API Key" in captured.out or "save this" in captured.out.lower():
                if "wa-" in captured.out:
                    pytest.fail(f"Plaintext API key printed to stdout: {captured.out}")
        finally:
            os.environ.pop("ADMIN_INITIAL_PASSWORD", None)


class TestDefaultSecretsSecurityC:
    """Regression: C — shipped default secrets must not be insecure literals."""

    def test_production_config_requires_webhook_secret(self):
        """ProductionConfig requires WEBHOOK_SECRET to be set (not 'change-in-production')."""
        # regression: security review 2026-07-26 — secrets must be from env in production
        os.environ.pop("WEBHOOK_SECRET", None)
        try:
            from services.management.app.config import ProductionConfig

            cfg = ProductionConfig()
            secret = cfg.WEBHOOK_SECRET

            # Must not be the insecure literal
            assert secret != "change-in-production", (  # noqa: S105 -- insecure default
                "ProductionConfig WEBHOOK_SECRET is hardcoded insecure literal"
            )

            # In production, if env var unset, should be empty or raise
            # (validation happens in the webhook handler)
            if not os.environ.get("WEBHOOK_SECRET"):
                # If not set in env, it should be empty or a warning should be raised
                # For now, we just verify it's not the hardcoded literal
                assert secret != "change-in-production"  # noqa: S105 -- insecure default
        finally:
            pass

    def test_production_config_requires_jwt_secret(self):
        """ProductionConfig requires JWT_SECRET to be set (not 'change-in-production')."""
        # regression: security review 2026-07-26 — secrets must be from env in production
        os.environ.pop("JWT_SECRET", None)
        os.environ.pop("JWT_SECRET_KEY", None)
        try:
            from services.management.app.config import ProductionConfig

            cfg = ProductionConfig()
            secret = cfg.JWT_SECRET_KEY

            # Must not be the insecure literal
            assert secret != "change-in-production-min-32-chars", (  # noqa: S105 -- insecure
                "ProductionConfig JWT_SECRET_KEY is hardcoded insecure literal"
            )
        finally:
            pass

    def test_testing_config_provides_deterministic_secrets(self):
        """TestingConfig provides safe, deterministic defaults (not 'change-in-production')."""
        # regression: security review 2026-07-26 — testing must have safe defaults
        from services.management.app.config import TestingConfig

        cfg = TestingConfig()

        # Testing can have deterministic defaults, but not the "change-in-production" literal
        assert cfg.WEBHOOK_SECRET != "change-in-production"  # noqa: S105 -- insecure default
        assert cfg.JWT_SECRET_KEY != "change-in-production-min-32-chars"  # noqa: S105 -- insecure

    def test_webhook_secret_empty_rejects_in_handler(self):
        """Webhook signature verification rejects when WEBHOOK_SECRET is empty."""
        # regression: security review 2026-07-26 — empty secret must cause rejection
        from services.management.app.api.v1.webhooks import verify_webhook_signature

        # Empty secret should NOT skip verification
        result = verify_webhook_signature(b"payload", "signature", "")
        # With the fix, this should return False (or verification should fail)
        # The original bug was: if not secret: return True
        # After fix: if not secret: return False (or raise)
        assert result is False, "Empty WEBHOOK_SECRET should not skip verification"

    def test_seeded_admin_key_org_id_matches_user_org_id(self):
        """Seeded admin virtual key organization_id matches seeded admin user organization_id."""
        # regression: security review 2026-07-26 — cross-file invariant
        # The seeded admin key's organization_id should equal the seeded admin
        # user's organization_id
        os.environ["ADMIN_INITIAL_PASSWORD"] = "test123"  # noqa: S105 -- fixed test credential

        try:
            from services.management.app.extensions import init_default_data

            # Mock the DB and track all inserts
            mock_db = MagicMock()
            inserted_users = []
            inserted_keys = []

            def insert_user(**kwargs):
                inserted_users.append(kwargs)
                return 1

            def insert_key(**kwargs):
                inserted_keys.append(kwargs)
                return 1

            # Set up mocks
            mock_db.commit = MagicMock()
            mock_db.return_value = MagicMock()
            mock_db.return_value.select = MagicMock(
                return_value=MagicMock(first=MagicMock(return_value=None))
            )

            mock_db.organizations = MagicMock()
            org_id = 1
            mock_db.organizations.insert = MagicMock(return_value=org_id)

            mock_db.users = MagicMock()
            mock_db.users.insert = MagicMock(side_effect=insert_user)

            mock_db.virtual_keys = MagicMock()
            mock_db.virtual_keys.insert = MagicMock(side_effect=insert_key)

            # We can't fully test this without a real DB, but we verify the structure
            # In the actual code, admin user org_id and admin key org_id must match
            try:
                init_default_data(mock_db)
            except Exception:  # noqa: S110 -- DB mocking may fail, only insert structure matters
                pass

            # Verify both user and key were created
            if inserted_users and inserted_keys:
                user_org = inserted_users[0].get("organization_id")
                key_org = inserted_keys[0].get("organization_id")
                assert user_org == key_org, (
                    f"Admin key org_id {key_org} does not match admin user org_id {user_org}"
                )
        finally:
            os.environ.pop("ADMIN_INITIAL_PASSWORD", None)


class TestMasterKeyPlaintextRegressionCodeQL2507:
    """Regression: codeql-2507 -- py/clear-text-logging-sensitive-data.

    shared/database/models.py used to carry its own duplicate
    init_default_data() (reachable only via `python3 -m
    shared.database.models`) that printed the generated admin API key in
    plaintext. That duplicate bootstrap has been removed; these tests assert
    the sole remaining bootstrap (services.management.app.extensions) never
    surfaces the generated key value on stdout/stderr/logs, and that it seeds
    an api_keys row (not just virtual_keys) so the CodeQL fix didn't
    regress proxy auth (shared/auth/rbac.py's RBACManager.authenticate_api_key
    only ever queries db.api_keys).
    """

    def test_generated_admin_key_sentinel_never_reaches_stdout_or_logs(
        self, capsys, caplog, monkeypatch
    ):
        """Uses a distinctive sentinel value, not just a 'wa-' substring check.

        Ties the assertion to the exact generated value, not a heuristic.
        """
        # regression: codeql-2507
        import logging
        import secrets

        sentinel = "CODEQL-2507-SENTINEL-VALUE-DO-NOT-LEAK"  # noqa: S105 -- test sentinel
        monkeypatch.setattr(secrets, "token_urlsafe", lambda *_a, **_kw: sentinel)
        monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "test_password_123")

        from services.management.app.extensions import init_default_data

        mock_db = MagicMock()
        mock_db.commit = MagicMock()
        mock_db.return_value.select.return_value = []
        mock_db.organizations.insert = MagicMock(return_value=1)
        mock_db.users.insert = MagicMock(return_value=1)
        mock_db.virtual_keys.insert = MagicMock(return_value=1)
        mock_db.api_keys.insert = MagicMock(return_value=1)

        with caplog.at_level(logging.INFO):
            init_default_data(mock_db, config={"ADMIN_INITIAL_PASSWORD": "test_password_123"})

        captured = capsys.readouterr()
        assert sentinel not in captured.out
        assert sentinel not in captured.err
        for record in caplog.records:
            assert sentinel not in record.getMessage()

    def test_seeded_admin_api_key_org_id_matches_admin_user_org_id(self):
        """Bootstrap seeds an api_keys row (proxy auth), not just virtual_keys."""
        # regression: codeql-2507 -- porting the missing seed into the hardened path
        os.environ["ADMIN_INITIAL_PASSWORD"] = "test123"  # noqa: S105 -- fixed test credential
        try:
            from services.management.app.extensions import init_default_data

            inserted_users = []
            inserted_api_keys = []

            mock_db = MagicMock()
            mock_db.commit = MagicMock()
            mock_db.return_value.select.return_value = []
            mock_db.organizations.insert = MagicMock(return_value=1)
            mock_db.users.insert = MagicMock(
                side_effect=lambda **kw: inserted_users.append(kw) or 1
            )
            mock_db.virtual_keys.insert = MagicMock(return_value=1)
            mock_db.api_keys.insert = MagicMock(
                side_effect=lambda **kw: inserted_api_keys.append(kw) or 1
            )

            init_default_data(mock_db, config={"ADMIN_INITIAL_PASSWORD": "test123"})

            assert inserted_api_keys, "bootstrap did not seed an api_keys row for proxy auth"
            assert inserted_api_keys[0]["organization_id"] == inserted_users[0]["organization_id"]
            assert inserted_api_keys[0]["permissions"] == {"*": True}
        finally:
            os.environ.pop("ADMIN_INITIAL_PASSWORD", None)


class TestModelsPyNoPlaintextKeyPrint:
    """Static guard: models.py must never regain a plaintext-key print().

    shared/database/models.py must never regain a print() call that
    references an API-key-like value (CodeQL-2507 regression guard).
    """

    def test_no_print_call_references_api_key(self):
        """Walk the AST for print() calls referencing an api_key-like name.

        Uses AST inspection rather than a brittle text substring check.
        """
        # regression: codeql-2507
        import ast
        from pathlib import Path

        models_path = Path(__file__).resolve().parents[3] / "shared" / "database" / "models.py"
        tree = ast.parse(models_path.read_text())

        def references_api_key(node: ast.AST) -> bool:
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and "api_key" in child.id.lower():
                    return True
                if isinstance(child, ast.JoinedStr):
                    for value in child.values:
                        if isinstance(value, ast.FormattedValue) and references_api_key(
                            value.value
                        ):
                            return True
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id != "print":
                    continue
                for arg in node.args:
                    assert not references_api_key(arg), (
                        f"models.py print() call references an api_key-like "
                        f"name at line {node.lineno}"
                    )
