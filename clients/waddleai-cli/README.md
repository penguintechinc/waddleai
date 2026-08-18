# waddleai-cli

A thin Rust static binary that is both the `waddleai` CLI and the
`waddleai mcp` stdio-to-streamable-HTTP MCP shim (plan §11.2, spec §11.1).
No business logic lives here -- every command is a direct call to a
WaddleAI `/api/v1`, `/v1`, or `/mcp` endpoint; `mcp` is a pure transport
bridge.

## Install

Static binaries are built by CI for `x86_64-unknown-linux-musl`,
`aarch64-unknown-linux-musl`, `x86_64-apple-darwin`, and
`aarch64-apple-darwin` (see `.github/workflows/rust-build.yml`). Build
locally with:

```bash
cd clients/waddleai-cli
cargo build --release
# binary at target/release/waddleai
```

## Commands

| Command | Purpose |
|---|---|
| `waddleai login [--basic [--username U]]` | Log in. Default: browser OAuth2 (Authorization Code + PKCE). `--basic`: username/password prompt against `/api/v1/auth/login`. |
| `waddleai logout` | Clear the stored session. |
| `waddleai link <endpoint-id>` | Start the per-user account-link flow for a registered external MCP endpoint. |
| `waddleai keys` | List your virtual keys. |
| `waddleai usage [--days N]` | Show token usage. |
| `waddleai models` | List models available to your key. |
| `waddleai knowledge upload <path>` | Upload a PDF/Markdown document. |
| `waddleai fleet status` | Show inference fleet backend status. |
| `waddleai mcp` | Stdio MCP shim -- forwards stdin/stdout JSON-RPC to `/mcp`. |

Every subcommand accepts `--api-url <url>`, falling back to
`$WADDLEAI_API_URL`, then the saved config, then `http://localhost:8000`.

## Authentication

- **Interactive use**: `waddleai login` opens your browser for OAuth2; the
  resulting token is stored in the OS-native credential store (macOS
  Keychain / Windows Credential Manager / Linux Secret Service via the
  `keyring` crate) -- never a plaintext file. Tokens refresh automatically
  ahead of expiry.
- **Headless/CI/MCP-client-config use**: set `WADDLEAI_API_KEY` and it
  takes precedence over any stored session -- no login step needed. This is
  the pattern used in the [Claude Code](../../docs/integrations/claude-code.md)
  MCP config example.
- **`--basic`**: username/password against the confirmed-live
  `/api/v1/auth/login` endpoint, for deployments that haven't mounted OIDC
  discovery yet.

### Browser-launch safety

`login` and `link` both auto-open a URL that comes from an HTTP response
(the OIDC discovery document's `authorization_endpoint`, and the
`mcp-endpoints/{id}/link` response, respectively) -- a compromised or
MITM'd server could otherwise use that to launch the user's browser at an
arbitrary phishing URL. Before opening anything, this CLI requires:
- scheme is exactly `https`
- the host matches (or is a subdomain of) an explicitly expected host --
  `api_url`'s own host for `login`, and the specific endpoint's own
  admin-registered URL (fetched separately) for `link`

A rejected URL is never opened; it's still printed beforehand so you can
copy it manually if you trust it. Set `WADDLEAI_NO_BROWSER=1` to always
skip auto-opening (headless/CI use) -- the URL is still printed.

The OAuth2 path performs standard OIDC discovery
(`GET {api-url}/.well-known/openid-configuration`) rather than hardcoding
WaddleAI-specific paths -- it works against any spec-compliant provider,
and will work against a WaddleAI deployment as soon as that deployment
mounts its discovery/authorize/token routes (tracked with the Management
`/api/v1/integrations/*` work). Until then, use `--basic` or
`WADDLEAI_API_KEY`.

## Using `waddleai mcp` as an MCP client's stdio command

```json
{
  "mcpServers": {
    "waddleai": {
      "command": "waddleai",
      "args": ["mcp"],
      "env": {
        "WADDLEAI_API_URL": "https://your-waddleai-host",
        "WADDLEAI_API_KEY": "wa-your-key-here"
      }
    }
  }
}
```

The shim forwards each JSON-RPC message on stdin to the deployment's `/mcp`
streamable-HTTP endpoint and writes the response back to stdout unchanged,
so any stdio-only MCP client can reach WaddleAI without needing an HTTP MCP
transport itself.

## Development

```bash
cd clients/waddleai-cli
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo audit
cargo deny check
cargo llvm-cov --fail-under-lines 90
```

Dependencies are pinned in `Cargo.toml` (no bare `*`) and locked in
`Cargo.lock` (committed, cryptographically verified). `rust-toolchain.toml`
pins the exact Rust channel + required components.
