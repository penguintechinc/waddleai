---
name: connection-links-credential-encryption
description: Finding #33 fix — connection_links.api_key (proxy-side PyDAL) had no app write path; encrypt-on-write helpers + credential_encryption gotchas
metadata:
  type: project
---

Finding #33 (release-audit-2026-09-23, Critical): proxy-side `connection_links.api_key` (`shared/database/models.py`) stored BYOK provider creds PLAINTEXT — PyDAL `"password"` type is display-masking only, not encryption. #236 covered management `provider_credentials` only.

**Why:** at-rest encryption is a mandatory baseline; the proxy table was missed by #236.

**How to apply / key facts for future work here:**
- NO application code writes `connection_links.api_key`. Only writers: the backfill `scripts/migrate_encrypt_credentials.py`, the e2e seed `tests/e2e/test_real_upstream.py::_seed_real_org` (ollama, no key), and the migration regression test (deliberate plaintext). Recon confirmed: zero writes in `proxy/` or `services/`. Column is deprecated (docstring notes migration 004 will drop it in favor of `provider_credentials` pool).
- Sanctioned write path now = `insert_connection_link(db, *, api_key=None, **fields)` and `update_connection_link_api_key(db, link_id, api_key)` in `shared/database/models.py`. Both route through `encrypt_credential`, idempotent via `is_encrypted` guard (no double-encrypt), leave `api_key=None` unset. Any future writer MUST use them — a bare `db.connection_links.insert(api_key=...)` persists plaintext.
- READ/decrypt side is already done in `shared/utils/llm_connectors.py::_select_credential` (calls `decrypt_credential` on both pool creds and the `link.api_key` fallback). The proxy provider-dispatch site under `proxy/apps/proxy_server/**` was a sibling branch's concern (fix/proxy-dataplane-hardening); coordinate via the `enc:` prefix contract.
- GOTCHA: `encrypt_credential("")` RAISES `CredentialEncryptionNotConfiguredError` when no key set — `get_encryption_config()` runs BEFORE the `if not plaintext` check. So helpers must guard `if api_key and not is_encrypted(api_key)` before calling; never call encrypt on empty/None or the no-auth (ollama) path breaks with no key configured.
- `credential_encryption` fails closed: no `CREDENTIAL_ENCRYPTION_KEY` → write raises unless `WADDLEAI_ALLOW_PLAINTEXT_CREDENTIALS=1` (dev opt-in). Decrypt of a non-`enc:` value passes through unchanged (legacy-plaintext compatible), so a decrypt round-trip test does NOT discriminate the vuln — assert `is_encrypted(stored)` on the WRITE for the pre-fix confirmation.
- See [[e2e_suite_and_penguin_dal_gotchas]] for the `get_db()` reflect=True 2nd-call collision (call get_db once per test with a unique sqlite file) and the migration's `.update()`-not-`.update_record()` / `id > 0`-not-`db(table)` fixes.
- mypy-gate: `mypy-baseline.txt` is EMPTY; gate scans `proxy services shared scripts tests`; any new error fails. Adding typed helpers to the untyped `models.py` was fine (lenient mypy, `db: DAL` = Any under ignore_missing_imports).
