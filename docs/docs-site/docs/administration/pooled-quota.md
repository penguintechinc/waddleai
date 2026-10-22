# Pooled token quota

A tenant gets a pool of **credits** (WaddleAI tokens) that is split across teams and users with
elastic sharing: each user can hold a private *guaranteed reservation*; when it runs out the
proxy automatically *leases* the remainder from the team's *shared buffer* (first come, first
served — nobody's reservation is ever taken); when the buffer is empty the request is refused
with **HTTP 429**. Credits are cost-normalized: an admin sets each model's `input_rate` /
`output_rate` (divisors of LLM tokens), so an expensive model burns more credits per token.

The feature is behind the PostHog flag `waddleai.pooled_quota` (default **off**, rolled out per
organization). With the flag off, or with no pool configured, nothing changes.

## Model

| Level | Setting | Who sets it |
|---|---|---|
| Organization | pool size, **cycle anchor date**, enabled | org admin (`quota:org_update`) |
| Team | pool (carved from the org pool), shared buffer | pool: org admin; buffer: team Admin/Owner (`team:quota_admin`) |
| User | guaranteed credits, optional daily sub-cap | team Admin/Owner |
| Override | hard cap on a user and/or model and/or provider | org admin |
| Conversion rate | per provider:model credit cost | platform admin only (`quota:admin`, global table) |

Invariants (violations are HTTP 409): `sum(user guaranteed) + team buffer <= team pool` and
`sum(team pools) <= org pool`. Teams are isolated: a team never borrows from another team or tenant.

## Billing cycle

Cycles are **anchored 30-day windows**, not calendar months: `cycle = floor((today - anchor) / 30 days)`
in UTC. Counters reset by moving to the next cycle index. Before the anchor date nothing is enforced.

## What happens to a request

1. **Reserve** (before dispatch): the proxy estimates credits from the requested model (provider is
   not known yet) and runs one atomic Valkey script: reservation first, then buffer, else 429.
   Cache hits are free and never reserve.
2. **Reconcile** (after the response): actual credits come from `token_usage.waddleai_tokens`;
   the counters are corrected in the **cycle the request was reserved in**, override caps are
   enforced with the real provider, and a durable row is added to `quota_usage_ledger`.
3. A request blocked after reserving (for example by output filtering) is refunded immediately; a
   request that never reconciles (crash, disconnect) is refunded by the expiry sweeper after 1 hour.

Override caps naming a provider are enforced at reserve only when the requested model is served by
exactly that one provider; otherwise they are enforced at reconcile (the overage is recorded and
the next request is refused).

## Failure behaviour

| Condition | Behaviour |
|---|---|
| Valkey unreachable | Requests are **allowed** (fail-open) and the failure is logged; usage during the outage is not counted until reconcile succeeds |
| Quota config unreadable | Fail-open, logged |
| Valkey flushed | Run `POST /api/v1/pooled-quota/rebuild` to restore counters from the ledger (never overwrites live counters) |

## API

| Route | Needs |
|---|---|
| `GET/PUT /api/v1/pooled-quota/org-pool` | `quota:org_update` |
| `PUT /api/v1/pooled-quota/teams/<id>/pool` | `quota:org_update` and `team:quota_admin` on that team |
| `GET/POST /api/v1/pooled-quota/overrides`, `DELETE .../overrides/<id>` | `quota:org_update` |
| `GET/PUT /api/v1/pooled-quota/conversion-rates` | `quota:admin` |
| `GET /api/v1/pooled-quota/usage?cycle=`, `POST /api/v1/pooled-quota/rebuild` | `quota:org_update` |
| `GET /api/v1/teams/<id>/pooled-quota` | `team:quota_read` (any team member) |
| `PUT /api/v1/teams/<id>/pooled-quota/buffer` | `team:quota_admin` |
| `PUT/DELETE /api/v1/teams/<id>/pooled-quota/reservations/<user_id>` | `team:quota_admin` |

Responses carry ids and credit counts only. Another tenant's team or override is always `404`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `WADDLEAI_RATE_REFRESH_SECONDS` | 60 | how soon a conversion-rate edit reaches the proxy |
| `PROXY_QUOTA_CONFIG_TTL_SECONDS` | 300 | proxy cache of org/team quota config (management deletes the entry on every edit) |
| `PROXY_QUOTA_SWEEP_INTERVAL_SECONDS` | 60 | how often expired reservations are refunded |

## Observability

OpenTelemetry metrics (no user/team ids in attributes): `waddleai.quota.decisions`,
`waddleai.quota.rejections` (429s), `waddleai.quota.buffer_leases`, `waddleai.quota.reconciles`,
`waddleai.quota.overages`, and histograms `waddleai.quota.credits_reserved`,
`waddleai.quota.credits_actual`, `waddleai.quota.buffer_lease_credits`,
`waddleai.quota.reserve_duration`; spans `quota.reserve`.
