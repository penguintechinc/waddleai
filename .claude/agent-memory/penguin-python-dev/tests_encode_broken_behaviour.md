---
name: tests-encode-broken-behaviour
description: In this repo a test failing right after a security fix usually means the test pinned the vulnerability as expected. Three confirmed instances in the 2026-09-14 audit pass. Verify which side is wrong before "fixing" the test or reverting.
metadata:
  type: feedback
---

**When a test fails immediately after a security fix, suspect the test before
the fix.** Confirm from first principles which side encodes correct behaviour,
then fix the wrong one — never reconcile by reverting the fix or by blindly
re-recording a snapshot.

**Why:** three separate instances surfaced in the 2026-09-14 security-audit
pass alone, so this is the local norm, not bad luck:

| Test | What it pinned |
|---|---|
| `test_set_key_quota_regular_user_own_key` | Asserted **200** for a self-escalation — the privilege escalation itself was the expected result |
| 7 management tests | Depended on the **plaintext-credential fallback** being present |
| `mgmt_auth_login.json` / `mgmt_auth_refresh.json` | Pinned `expires_in: 86400` against a token whose real `exp` was always 3600s — the contract suite was defending a client-facing lie |

**How to apply:**

- A golden/snapshot file is an *assertion*, not evidence. `CONTRACT_RECORD=1`
  re-records whatever the code currently does, so it happily launders a
  regression into the contract. Read the value and reason about it first.
- **Record *why* next to any corrected expected value.** A future reader
  seeing `86400 -> 3600` in `git log` will reasonably conclude someone
  shortened the session. JSON has no comments, so put the rationale in the
  test docstring *and* a sibling notes file (`tests/contract/snapshots/NOTES.md`)
  *and* the commit message — the diff alone carries none of it.
- The inverse trap also exists: a test that passes with the fix reverted is
  proving nothing. Always revert-in-place and confirm the test actually fails
  before claiming it is a regression test. This caught a vacuous
  `test_successful_login_resets_the_counter` (every assertion expected 401,
  which an unthrottled login also returns) and a mocked-DB isolation test that
  passed with the tenant filter removed — see
  [[sql-only-tenant-filters-untestable]].

Related: [[management-route-gates-and-test-traps]],
[[penguin-aaa-token-ttl-and-jti]].
