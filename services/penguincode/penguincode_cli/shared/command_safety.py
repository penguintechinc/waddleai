"""Destructive-command classification shared by every bash execution path.

Lives in ``shared`` (not ``tools``) so the thin client-side executor can reach it
without pulling in the heavy tool package. Both ``tools.bash.BashTool`` and
``client.tool_executor.LocalToolExecutor`` gate on this module.

Scope and honest limits
-----------------------
This is a **keyword gate on the executable name**, i.e. defence in depth against
an agent that has been talked into running something obviously destructive. It is
NOT a sandbox and must never be described as one. It reads the command the way a
shell would split it and checks which program each segment invokes; it does not
execute, expand or interpret anything.

Caught: an absolute or relative path to a dangerous program (``/bin/rm``), shell
escaping that hides the name from a naive substring match (``\\rm``, ``r""m``),
every segment of a pipeline or ``&&`` chain, ``env VAR=1 rm``, wrapper commands
(``xargs rm``, ``timeout 30 rm``), and ``find ... -exec rm``.

NOT caught, by construction: indirection through an interpreter
(``python -c "os.system('rm -rf /')"``), encoded payloads piped to a shell
(``echo ... | base64 -d | sh``), variable or alias expansion (``$RM -rf /``), a
script file that is itself destructive, and destructive *flags* on otherwise
benign programs (``find . -delete``). Anything that needs a real boundary needs
OS-level confinement, not this function.

Precision matters more than breadth here: no confirmation UI exists in the CLI
yet, so "destructive" currently means "refused outright". A gate that fires on
``pytest tests/test_skill_system.py`` (because "skill" contains "kill") or
``ruff format --check .`` gets switched off wholesale, taking the real protection
with it. Hence: match command *names*, never substrings of the command line.
"""

import os
import re
import shlex
from collections.abc import Awaitable, Callable

# Programs whose invocation is treated as destructive. Matched against the
# basename of a segment's executable, never as a substring of the command line.
DANGEROUS_COMMANDS: frozenset[str] = frozenset(
    {
        "rm",
        "rmdir",
        "shred",
        "dd",
        "chmod",
        "chown",
        "kill",
        "pkill",
        "killall",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "su",
        "sudo",
        "doas",
        # Windows command names — only ever as a command, never as a --format flag
        "del",
        "format",
    }
)

# Executables matched by prefix: mkfs, mkfs.ext4, mkfs.xfs, ...
DANGEROUS_COMMAND_PREFIXES: tuple[str, ...] = ("mkfs",)

# Commands that wrap another command; step past them to reach the real executable.
# sudo/doas are deliberately absent — they are dangerous in their own right and
# match before we would ever need to look past them.
WRAPPER_COMMANDS: frozenset[str] = frozenset(
    {"env", "nohup", "time", "nice", "ionice", "timeout", "command", "builtin", "exec", "xargs"}
)

# Redirecting into these trees overwrites the system itself. Bare redirects are
# NOT destructive: `pytest -q > out.txt` is ordinary, and with no confirmation
# path a rule that broad would be disabled within a day.
SYSTEM_PATH_PREFIXES: tuple[str, ...] = ("/dev/", "/etc/", "/proc/", "/sys/", "/boot/")

# find(1) flags whose following token is itself a command to run.
EXEC_FLAGS: frozenset[str] = frozenset({"-exec", "-execdir", "-ok", "-okdir"})

_SEGMENT_SEPARATORS = re.compile(r"\|\||&&|[;|&\n]")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_NUMERIC_ARG = re.compile(r"^\d+[smhd]?$")
_REDIRECT_OPERATORS = frozenset({">", ">>", ">|", "1>", "2>", "&>", "1>>", "2>>", "&>>"})

# Async predicate asked to approve a destructive command: (command, keyword) -> bool.
ConfirmCallback = Callable[[str, str], Awaitable[bool]]


def _tokenize(segment: str) -> list[str]:
    """Split one command segment into tokens the way a shell would.

    Falls back to a whitespace split when shlex cannot parse the segment (an
    unbalanced quote, say) — failing open on a malformed command would be the
    wrong direction for a safety check.
    """
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.replace("\\", "").split()


def _is_dangerous_name(token: str) -> str | None:
    """Return the dangerous command name a token invokes, or None.

    Compares the basename, so /bin/rm and ./rm are recognised as rm.
    """
    name = os.path.basename(token.strip()).lower()
    if not name:
        return None
    if name in DANGEROUS_COMMANDS:
        return name
    if any(name.startswith(prefix) for prefix in DANGEROUS_COMMAND_PREFIXES):
        return name
    return None


def _segment_executable(tokens: list[str]) -> str | None:
    """Find the executable a token list actually invokes.

    Steps past leading VAR=VALUE assignments and wrapper commands (env, xargs,
    timeout and friends) together with their flags and numeric arguments.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT.match(token):
            index += 1
            continue
        if os.path.basename(token).lower() in WRAPPER_COMMANDS:
            index += 1
            while index < len(tokens) and (
                tokens[index].startswith("-") or _NUMERIC_ARG.match(tokens[index])
            ):
                index += 1
            continue
        return token
    return None


def _redirect_into_system_path(tokens: list[str]) -> bool:
    """Report whether any redirect in this segment targets a system tree."""
    for index, token in enumerate(tokens):
        target: str | None = None
        if token in _REDIRECT_OPERATORS:
            if index + 1 < len(tokens):
                target = tokens[index + 1]
        elif token.startswith(">"):
            target = token.lstrip(">")
        if target and target.startswith(SYSTEM_PATH_PREFIXES):
            return True
    return False


def classify_destructive(command: str) -> str | None:
    """Return the dangerous command a shell line would run, or None if it looks benign.

    Returning the matched name rather than a bool lets callers explain the refusal
    to the agent and the user instead of denying without a reason. See the module
    docstring for what this does and does not detect.
    """
    for segment in _SEGMENT_SEPARATORS.split(command):
        if not segment.strip():
            continue
        tokens = _tokenize(segment)
        if not tokens:
            continue

        executable = _segment_executable(tokens)
        if executable is not None:
            matched = _is_dangerous_name(executable)
            if matched:
                return matched

        # find -exec rm {} \; runs rm without rm ever being a segment's argv[0]
        for index, token in enumerate(tokens):
            if token in EXEC_FLAGS and index + 1 < len(tokens):
                matched = _is_dangerous_name(tokens[index + 1])
                if matched:
                    return matched

        if _redirect_into_system_path(tokens):
            return ">"

    return None


async def approve_destructive(
    command: str,
    *,
    allow_destructive: bool = False,
    confirm: ConfirmCallback | None = None,
) -> tuple[bool, str | None]:
    """Decide whether a command may run, returning (approved, matched_command).

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
        f"Refused: command invokes the destructive operation {keyword!r} and was not "
        f"approved. Ask the user to run it manually, or re-run this tool with "
        f"explicit approval. Command: {command}"
    )
