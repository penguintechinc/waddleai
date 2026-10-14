# Teams

A **team** is a group of users inside one organization (tenant). Teams sit between the
organization and the user in WaddleAI's scope cascade (Global, Tenant, Team, User): a team
role delegates administration below the organization admin, and every API key acts in exactly
one team.

## Model

| Concept | Rule |
|---|---|
| Nesting | A team belongs to exactly one organization; teams are flat (no nesting). |
| Membership | A user is in **at most one team per organization** (a user who manages two organizations may have one team in each). |
| Acting team | `api_keys.team_id` names the team a request acts in. A key with no team acts teamless. |
| Seating | Every organization has a **Default** team. New organizations get one on creation; a new user joins it as a member (as team admin if they are an organization admin). |
| Scope | Provider keys, model access, routing and RAG collections stay organization-scoped. |

## Team roles

| Role | Can do (within its own team) |
|---|---|
| **Owner** | Everything an Admin can, plus delete the team and transfer ownership. |
| **Admin** | Rename or disable the team, add/remove members, change roles, issue keys for teammates. |
| **Member** | Read the team, use it, create their own team-bound keys. |
| **Viewer** | Read-only: view the team and its members. |

Only an Owner (or an organization admin) can grant, edit or remove `owner`, and a team that has
an owner always keeps one. The organization admin holds `team:org_admin` and can act on any
team in its own organization.

## API

All routes are under `/api/v1/teams` and answer with ids and roles only (no names or emails).

| Route | Needs |
|---|---|
| `POST /teams` | `team:org_admin` |
| `GET /teams` | any user (org admin sees all teams in the tenant, others only their own) |
| `GET /teams/<id>`, `GET /teams/<id>/members` | any member of the team |
| `PUT /teams/<id>` | Admin or above |
| `DELETE /teams/<id>` | Owner |
| `POST /teams/<id>/members`, `PUT`/`DELETE /teams/<id>/members/<user_id>` | Admin or above |
| `POST /teams/<id>/transfer-ownership` | Owner |

`POST /api/v1/keys` accepts an optional `team_id`. Omitted, the key is bound to the owner's team;
named, the caller must be allowed to issue keys into that team and the key owner must be a member.
Every key acts in a team: creating a key for a user who is in no team is a 409 until they are added
to one (with teams disabled, keys are created teamless).

## Upgrading

Migration `024_teams` creates a **Default** team in every organization, seats every existing user
in it (organization admins as team `admin`, everyone else as `member`; **no owner is assigned**,
an organization admin designates one afterwards) and binds each existing proxy key to its
owner's team. It is idempotent and reversible.

The migration only covers data that exists at upgrade time. From then on the API seats new
organizations and users itself, and the first-boot bootstrap seeds the default organization's
Default team, its admin and the admin master key.

## Operating notes

- **Kill switch:** set the feature flag `waddleai.disable-teams` (env override
  `WADDLEAI_FLAG_DISABLE_TEAMS=1`) to turn the subsystem off: the team API answers 404, and the
  proxy acts teamless. Cached proxy entries pick it up when they expire or are invalidated.
- **Freshness:** a membership or role change invalidates the proxy's API-key auth cache for the
  affected users immediately. Without a reachable Valkey the proxy's cache TTL
  (`PROXY_AUTH_CACHE_TTL_SECONDS`, default 60s) bounds the delay. Team scopes already minted into
  an access token last until that token expires (at most its TTL); management re-reads live
  membership on every team call, so they only matter for the proxy's JWT path.
- **Keys created before 024** through the management API use the legacy `wa-{secret}` format and
  never authenticated at the proxy. They keep working against the management API; create a new
  key to get a credential the proxy accepts.
- **DSAR:** `team_members` (ids and role only, no personal data) is covered by the data-subject
  access export and erasure flows like every other user-referencing table.
