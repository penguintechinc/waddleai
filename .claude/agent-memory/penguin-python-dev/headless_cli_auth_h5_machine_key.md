---
name: headless-cli-auth-h5-machine-key
description: penguincode_cli waddleai_auth.py headless/machine-key mode (H5) — precedence wiring, cache-field gotcha, mypy baseline, docs location
metadata:
  type: project
---

H5 added a headless/machine mode to `services/penguincode/penguincode_cli/client/waddleai_auth.py`'s `WaddleAITokenProvider`, extending F4 (see [[twire_wiring_knowledge_memory_code_retrieval]] sibling memory for other T-wire-adjacent wiring tasks). Service-account `wa-` key (`WADDLEAI_API_KEY`/`WADDLEAI_API_KEY_FILE`) → `POST /api/v1/auth/token` (H2's contract: no body, `Authorization: Bearer wa-<key>`) → short-lived JWT, precedence over interactive login and local-dev fallback, no TTY required.

**Precedence wiring gotcha**: the machine-key branch must be checked *before* `_should_use_dev_fallback()` in `get_access_token()`, not folded into it — dev-fallback triggers on "no issuer configured", which is also a plausible headless deployment shape (real issuer omitted, only a key set). Also had to make `_effective_dev_mode()` return `False` whenever `machine_key` is set, purely so `_cache_matches_current_config()` (which compares `is_dev`) doesn't false-mismatch a machine-obtained cache entry — the dev branch is never actually reached when a key is configured, but the cache-match helper is evaluated regardless.

**`_CachedToken.is_machine` field gotcha**: added as `is_machine: bool = False` (default, at the end, after the existing no-default fields) so old cache files and old test constructions round-trip fine. The refresh-branch guard changed from `not cached.is_dev` to `not cached.is_dev and not cached.is_machine` — a machine-issued token must never hit `/auth/refresh` (H2 has no refresh grant for it); it falls through to the unconditional `if self._config.machine_key: return await self._exchange_machine_key()` check right after, which is how renewal actually happens (re-exchange, not refresh).

**mypy --strict verification trap**: running `mypy --strict` on just this one file still drags in the whole import graph and reports 214 pre-existing errors (untyped grpc stubs in `grpc_client.py`/`knowledge_client.py`, missing generated members in `proto/__init__.py`). Confirming "0 new errors" required literally swapping the pre-change file back in via `git show HEAD:...` and re-running mypy to see the same 214 — a bare error count alone doesn't prove no regression since the number is dominated by unrelated files.

**Docs location**: `services/penguincode/docs/CONFIGURATION.md` already had an "Authentication Configuration" section, but it's for the *local* shared-key HS256 client-server gRPC auth (`penguincode_cli/auth/middleware.py`), a completely different mechanism from WaddleAI's RS256 JWT. Added a new "WaddleAI Authentication" section immediately after it rather than conflating the two.

**Verification-integrity practice used**: after implementing, injected two deliberate bugs (short-circuit the machine-key branch to `if False`; and gut `_mask_key` to return the raw key) one at a time, confirmed the relevant new tests failed (10 failures / 2 failures respectively), then restored — proved the new regression tests aren't vacuous before reporting pass.
