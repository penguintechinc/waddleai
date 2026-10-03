# WaddleAI Web UI

React 18 + Vite management console for WaddleAI. Talks only to the API
server under `/api/v1/*` — see the repo root `CLAUDE.md` for the overall
architecture.

## Running

```bash
npm ci
npm run dev        # Vite dev server
npm run build       # production build -> dist/
npm run lint
npm test             # vitest + coverage (>=90% lines/branches/functions/statements)
```

## Offline / connectivity behavior

The UI distinguishes three states when it talks to the API, instead of
collapsing "can't reach the server" into "not logged in":

| State | Trigger | What the user sees |
|---|---|---|
| **Authenticated** | `/api/v1/auth/verify` returns 2xx | Normal app |
| **Unauthenticated** | A real `401`/`403` from the server | Redirected to `/login` — this is the *only* case that clears a session |
| **Unreachable** | `fetch()` throws (offline, DNS, CORS) or the server answers with a non-auth error (5xx, etc.) | Stays on the current page with whatever data it already has; a banner reading "⚠️ Can't reach the server — retrying…" appears at the top of every page, including `/login` |

While unreachable, the app retries the `/auth/verify` probe automatically
with exponential backoff (`src/hooks/useRetryBackoff.js`, default 2s base
delay, doubling, capped at 30s) and exposes a "Retry now" button on the
banner for an immediate attempt. **The retry loop never runs for a 401/403**
— a confirmed auth decision is not treated as a connectivity problem, so it
never gets silently retried into a lockout.

A small always-visible **Online / Offline** badge
(`src/components/ConnectivityIndicator.jsx`) in the top bar reflects the
combination of `navigator.onLine` and the outcome of the most recent API
call (`src/contexts/ConnectivityContext.jsx`) — a machine the OS calls
"online" that still can't reach the backend (DNS, CORS, backend down) shows
"Offline" here.

**What still works offline today:** nothing API-backed — this UI has no
local cache or offline write queue yet (tracked as future work). The value
of the states above is purely UX: the user is told *why* nothing is loading
and given a retry path, instead of being bounced to a login screen that
makes a reachability problem look like an expired session.

### Where this lives

- `src/contexts/AuthContext.jsx` — the three-state auth probe + login/logout
- `src/contexts/ConnectivityContext.jsx` — `navigator.onLine` + last API outcome
- `src/hooks/useRetryBackoff.js` — generic exponential-backoff retry scheduler
- `src/components/ConnectivityBanner.jsx` — the "can't reach the server" banner
- `src/components/ConnectivityIndicator.jsx` — the Online/Offline badge
