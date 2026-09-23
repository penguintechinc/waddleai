---
name: quart-schema-validation-behaviors
description: Hard-won quart-schema @validate_request/@validate_response behaviors in the management service (pinned version) — what 500s, what strips, what 400s
metadata:
  type: project
---

Empirically probed against the management service's pinned quart-schema (Python 3.13, pydantic v2 dataclass path). These determine how to write validated Quart handlers here. See `services/management/app/api/v1/keys.py` for the canonical pattern.

**@validate_response(Model, status):**
- Returning `jsonify(...)` under @validate_response → **500 `ResponseHeadersValidationError`**. Success bodies MUST return a plain `dict` (or dataclass), NOT `jsonify`. Quart auto-jsonifies the dict.
- Extra keys in the returned dict are **silently stripped** to the model (200, not error) — at top level AND nested. So the wire response == the model's field set exactly.
- A **missing** model field in the returned dict → **500 `ResponseSchemaValidationError`** (unless the model field has a default, then it's filled).
- Only the registered status code is validated. Error returns at other statuses (404/403/400 via `jsonify`) pass through untouched — so keep error returns as `jsonify(...), 4xx`.
- Stacking `@validate_response(M, 200)` + `@validate_response(M, 201)` works — needed for create/upsert returning 200-or-201 with one shape.
- **Falsifiability**: drop a field from a response model → @validate_response strips it → an exact-field-set test (`assert set(body["data"].keys()) == {...}`) fails. This is how the response-schema regression tests are proven.

**@validate_request(Model):**
- Make every field `Optional` with the handler's old default, so quart-schema only 400s on malformed/mistyped body — the handler keeps its own presence/value checks (and their exact 400 messages: `if data.x is None: return ...`).
- null/empty/missing body → 400 (generic `{"error":"Bad Request"}` via the app's 400 errorhandler). Existing `test_no_body`/`test_requires_body` that only assert status 400 still pass.
- Extra request fields are ignored → the model must include EVERY body field the handler reads, else it's dropped.
- Wrong-typed field (e.g. `match` typed `dict` but sent a list; `priority` typed `int` sent a string) → 400.
- **Gotcha**: an empty `{}` PUT body is now VALID input (all-optional), so the handler runs past the old "body required" guard to the row lookup — tests asserting empty-body→400 must stub a row and assert the `no_fields` 400 instead.

**Decorator order** (top→bottom = outer→inner): `@route`, `@require_auth`, `@require_scope(...)`, `@validate_response(...)`, `@validate_request(...)`, handler. Auth/scope run before body validation; `require_scope`'s `_required_scopes` attr still propagates up through `@wraps` so `test_scope_authz` stays green.

**Reserved-word JSON keys**: a response with a `from` key (routing_decisions summary meta) can't be a dataclass field → type that `meta` as `dict[str, Any]` and pin its exact keys in the route's regression test instead.

Related: [[management_route_gates_and_test_traps]], [[management_venv_and_requirements]].
