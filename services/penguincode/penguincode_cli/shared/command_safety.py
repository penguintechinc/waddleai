"""Destructive-command classification shared by every bash execution path.

Lives in ``shared`` (not ``tools``) so the thin client-side executor can reach it
without pulling in the heavy tool package. Both ``tools.bash.BashTool`` and
``client.tool_executor.LocalToolExecutor`` gate on this module.
"""

from collections.abc import Awaitable, Callable

# Substrings that mark a command as potentially destructive. Deliberately broad:
# an agent driven by untrusted repository content is the threat model, so a false
# positive costs a confirmation while a false negative costs the filesystem.
DESTRUCTIVE_KEYWORDS: tuple[str, ...] = (
    "rm ",
    "rmdir",
    "del ",
    "format",
    "mkfs",
    "dd ",
    ">",  # Redirect (overwrite)
    "sudo",
    "su ",
    "chmod",
    "chown",
    "kill",
    "pkill",
    "shutdown",
    "reboot",
    "halt",
)

# Async predicate asked to approve a destructive command: (command, keyword) -> bool.
ConfirmCallback = Callable[[str, str], Awaitable[bool]]


def classify_destructive(command: str) -> str | None:
    """Return the destructive keyword a command matches, or None if it looks benign.

    Returning the matched keyword rather than a bool lets callers explain the
    refusal to the agent and the user instead of denying without a reason.
    """
    cmd_lower = command.lower()
    for keyword in DESTRUCTIVE_KEYWORDS:
        if keyword in cmd_lower:
            return keyword
    return None


async def approve_destructive(
    command: str,
    *,
    allow_destructive: bool = False,
    confirm: ConfirmCallback | None = None,
) -> tuple[bool, str | None]:
    """Decide whether a command may run, returning (approved, matched_keyword).

    A confirmation callback wins when one is wired up; otherwise the caller's
    ``allow_destructive`` opt-in decides. With neither, destructive commands are
    refused — the gate fails closed.
    """
    matched = classify_destructive(command)
    if matched is None:
        return True, None
    if confirm is not None:
        return bool(await confirm(command, matched)), matched
    return bool(allow_destructive), matched


def refusal_message(command: str, keyword: str) -> str:
    """Build the message returned to the agent when a destructive command is refused."""
    return (
        f"Refused: command matches the destructive pattern {keyword!r} and was not "
        f"approved. Ask the user to run it manually, or re-run this tool with "
        f"explicit approval. Command: {command}"
    )
