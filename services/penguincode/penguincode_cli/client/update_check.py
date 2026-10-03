"""Silent, non-blocking startup version check (O8 CLI resilience, client.md Update Checks).

`REPLSession.run()` calls `maybe_notify_update()` once per session start: it compares this
CLI's installed version against the penguincode server's reported version (the `version`
field `HealthService.Check` already returns -- see `client/grpc_client.py`'s own health
check) and prints a single "update available" line if the server is ahead. Every failure
mode -- server unreachable, timeout, malformed version string, no server configured at
all -- degrades to doing nothing, never a crash, never a delay to REPL startup beyond
`update_check_timeout_seconds`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path

# Same known gap as every other `import grpc` in this package (see
# `.mypy-baseline.txt`'s `auth/middleware.py`/`client/grpc_client.py` entries) -- no
# `grpc-stubs` package is pinned, so this is suppressed inline here too.
import grpc  # type: ignore[import-untyped]

from penguincode_cli.config.settings import ClientConfig, ServerConfig
from penguincode_cli.flags.client import DISABLE_UPDATE_CHECK_FLAG, SYSTEM_SCOPE
from penguincode_cli.flags.client import is_enabled as _flag_is_enabled
from penguincode_cli.proto import HealthCheckRequest, HealthServiceStub

logger = logging.getLogger(__name__)

#: Distributed package name (see `pyproject.toml`'s `[project].name`) -- the one
#: `importlib.metadata.version()` looks up when the CLI is `pip install`-ed normally.
_PACKAGE_NAME = "penguincode"

#: Fallback when running from an uninstalled source checkout (no package metadata) --
#: never raises, just means the comparison always reports "no update known".
_UNKNOWN_VERSION = "0.0.0"

#: State file recording the last check's wall-clock time (not the result) -- the
#: `update_check_interval_hours` gate reads this file's own content, never a long-lived
#: in-process timer (a fresh CLI process every invocation has no timer to remember).
_DEFAULT_STATE_PATH = "~/.penguincode/update_check_state.json"


@dataclass(slots=True, frozen=True)
class UpdateCheckResult:
    """Outcome of one version comparison -- `update_available` is the only field a caller
    needs to decide whether to print anything.
    """

    current_version: str
    server_version: str
    update_available: bool


def _parse_version(raw: str) -> tuple[int, ...]:
    """Best-effort dotted-integer parse (``"1.2.3"`` -> ``(1, 2, 3)``).

    Any non-numeric component truncates the parse at that point rather than raising --
    `"1.2.3-beta"` parses as `(1, 2)`, which is good enough for a simple
    behind/ahead/equal comparison and never crashes on an unexpected format.
    """
    parts: list[int] = []
    for segment in raw.strip().split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


def _installed_version() -> str:
    """The CLI's own installed version, or `_UNKNOWN_VERSION` if metadata isn't available
    (e.g. running from an editable/uninstalled source checkout in dev).
    """
    try:
        return importlib_metadata.version(_PACKAGE_NAME)
    except importlib_metadata.PackageNotFoundError:
        return _UNKNOWN_VERSION


async def check_for_update(
    server_config: ServerConfig,
    *,
    current_version: str | None = None,
    timeout_seconds: float = 3.0,
) -> UpdateCheckResult | None:
    """Compare this CLI's version against the server's, via a short-lived `HealthService.Check`
    call. Returns `None` on ANY failure (unreachable server, timeout, bad response) -- never
    raises, so a caller can always safely `await` this without a try/except of its own.
    """
    resolved_current = current_version if current_version is not None else _installed_version()
    address = f"{server_config.host}:{server_config.port}"

    if server_config.tls_enabled:
        channel = grpc.aio.secure_channel(address, grpc.ssl_channel_credentials())
    else:
        channel = grpc.aio.insecure_channel(address)
    try:
        stub = HealthServiceStub(channel)  # type: ignore[no-untyped-call]
        response = await stub.Check(HealthCheckRequest(), timeout=timeout_seconds)
    except (grpc.RpcError, TimeoutError, OSError) as exc:
        logger.debug("update_check: server unreachable, skipping: %s", exc)
        return None
    finally:
        await channel.close()

    server_version = response.version or _UNKNOWN_VERSION
    update_available = _parse_version(server_version) > _parse_version(resolved_current)
    return UpdateCheckResult(
        current_version=resolved_current,
        server_version=server_version,
        update_available=update_available,
    )


def _should_run_check(state_path: Path, *, interval_hours: float, now: float) -> bool:
    """Whether enough time has passed since the last recorded check (missing/corrupt
    state file always means "yes, check now").
    """
    try:
        last_checked = state_path.stat().st_mtime
    except OSError:
        return True
    return (now - last_checked) >= interval_hours * 3600.0


def _record_check(state_path: Path) -> None:
    """Touch *state_path* so the next `_should_run_check` sees a fresh mtime. Best-effort --
    a failed write just means the next session re-checks, never a crash.
    """
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{}", encoding="utf-8")
    except OSError as exc:
        logger.debug("update_check: could not record check state: %s", exc)


async def maybe_notify_update(
    server_config: ServerConfig,
    client_config: ClientConfig,
    *,
    state_path: str | None = None,
    clock: float | None = None,
) -> str | None:
    """Run `check_for_update()` if due (kill-switch + `update_check_interval_hours` both
    checked first), returning a one-line notice string if an update is available, else
    `None`. Never blocks the caller beyond `client_config.update_check_timeout_seconds`,
    never raises -- intended to be awaited directly from `REPLSession.run()` at startup.
    """
    if _flag_is_enabled(DISABLE_UPDATE_CHECK_FLAG, SYSTEM_SCOPE):
        return None

    path = Path(state_path or _DEFAULT_STATE_PATH).expanduser()
    now = clock if clock is not None else time.time()
    if not _should_run_check(
        path, interval_hours=client_config.update_check_interval_hours, now=now
    ):
        return None

    result = await check_for_update(
        server_config, timeout_seconds=client_config.update_check_timeout_seconds
    )
    _record_check(path)
    if result is None or not result.update_available:
        return None

    return (
        f"A newer penguincode server version is available: "
        f"{result.current_version} -> {result.server_version}"
    )
