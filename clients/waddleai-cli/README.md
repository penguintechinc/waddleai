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
| `waddleai fleet vllm inventory [--vendor nvidia\|amd\|intel] [--schedulable true\|false] [--json]` | List discovered GPUs (GPU Fleet; needs the `waddleai.fleet_v2` and `waddleai.fleet-vllm-ray` flags). Example: `waddleai fleet vllm inventory --vendor amd`. |
| `waddleai fleet vllm fit --params-b N [--quantization Q] [--dtype D] [--max-model-len L] [--allow-mixed-class] [--json]` | Check whether a model fits the warm GPU pools; a refusal prints the server's text and exits 1. Example: `waddleai fleet vllm fit --params-b 12`. |
| `waddleai fleet vllm deploy -f <request.json> [--json]` | Create a deployment from a JSON request file (the `POST /api/v1/fleet/vllm/deployments` body). Example: `waddleai fleet vllm deploy -f qwen.json`. |
| `waddleai fleet vllm list [--status N] [--json]` | List deployments. Example: `waddleai fleet vllm list --status 5`. |
| `waddleai fleet vllm scale <id> --min N --max M` | Set the replica range. Example: `waddleai fleet vllm scale 0b0f1f3e-... --min 1 --max 3`. |
| `waddleai fleet vllm stop <id>` / `resume <id>` | Stop or resume a deployment. Example: `waddleai fleet vllm stop 0b0f1f3e-...`. |
| `waddleai fleet vllm delete <id> [--yes]` | Delete a deployment; without `--yes` a terminal asks first and a script is refused. Example: `waddleai fleet vllm delete 0b0f1f3e-... --yes`. |
| `waddleai fleet vllm rotate-token <id> [--yes]` | Rotate a deployment's bearer token (prints the new fingerprint, never the token). Example: `waddleai fleet vllm rotate-token 0b0f1f3e-... --yes`. |
| `waddleai fleet llamacpp deploy --name N --model-name M [--topology single\|rpc] [--model-url U --model-filename F] [--gguf-bytes B] [--allow-mixed-class] [--n-ctx T] [--comparison-model P/M] [--json]` | Create a llama.cpp deployment. `--topology rpc` spreads one GGUF model over several nodes (slower across nodes; use when you need GGUF or llama.cpp-only features) and needs `--model-url`, `--model-filename` and `--gguf-bytes`. The server refuses with the reason when it does not fit, fits on one node, or the name is taken (409). Example: `waddleai fleet llamacpp deploy --name big --model-name qwen --topology rpc --model-url https://host/q.gguf --model-filename q.gguf --gguf-bytes 69793218560`. |
| `waddleai fleet llamacpp list [--topology single\|rpc] [--json]` | List your organization's llama.cpp deployments. Example: `waddleai fleet llamacpp list --topology rpc`. |
| `waddleai fleet secrets list [--json]` | List cluster keys: name, source, fingerprint, last rotation (never values; platform operator only). Example: `waddleai fleet secrets list`. |
| `waddleai fleet secrets rotate <name> [--yes]` | Rotate a generated cluster key and restart Ray; a key supplied by env var or `existingSecret` exits **2** (`managed externally`). Example: `waddleai fleet secrets rotate ray-auth --yes`. |
| `waddleai harness sync` | Preview a default-disabled MCP configuration fragment; `--output <path>` creates a new explicit fragment file. |
| `waddleai apply -f <manifest>` | Validate and dry-run a DAC v1alpha1 manifest; use `--confirm <plan-id>` to apply the reviewed plan. |
| `waddleai mcp` | Stdio MCP shim -- forwards stdin/stdout JSON-RPC to `/mcp`. |

Every subcommand accepts `--api-url <url>`, falling back to
`$WADDLEAI_API_URL`, then the saved config, then `http://localhost:8000`.

`harness sync` never guesses a client configuration path or overwrites one.
Without `--output`, it previews a fragment using `${WADDLEAI_API_KEY}` and
`enabled: false`. With `--output ./waddleai-mcp.json`, it creates only that
new file; copy it deliberately into the client-specific configuration
described in `docs/integrations/`.

`apply` never mutates state by default. Its dry run returns a short-lived
`plan_id`; pass that exact value with `--confirm` only after reviewing the
create/update/no-op plan. Manifests may reference credentials, but cannot
contain tokens or other inline secrets.

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
