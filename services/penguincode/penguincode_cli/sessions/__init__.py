"""Cross-pod chat-session persistence (security audit finding O4-a High).

`ChatServiceImpl` (``server/services/chat.py``) used to keep
``sessions: dict[str, SessionState]`` as in-process state while
CreateSession/Chat/GetHistory/CloseSession are separate gRPC RPCs and prod
runs ``replicas=3`` -- a session created on one pod 404s on every other
pod, and any rollout drops every in-flight session. This package is the
shared-Postgres replacement: see `store.py`'s module docstring for the
full design and the `penguincode.disable-shared-sessions` kill switch.
"""

from penguincode_cli.sessions.store import (
    DISABLE_SHARED_SESSIONS_FLAG,
    InMemorySessionStore,
    PostgresSessionStore,
    SessionRecord,
    SessionStore,
    create_session_store,
)

__all__ = [
    "DISABLE_SHARED_SESSIONS_FLAG",
    "InMemorySessionStore",
    "PostgresSessionStore",
    "SessionRecord",
    "SessionStore",
    "create_session_store",
]
