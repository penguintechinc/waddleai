"""Session management for PenguinCode."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Message:
    """Chat message in a session."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Session:
    """Represents a chat session."""

    session_id: str
    created_at: str
    project_dir: str
    messages: list[Message] = field(default_factory=list)
    metadata: dict[str, any] = field(default_factory=dict)

    def add_message(self, role: str, content: str) -> None:
        """Add a message to the session."""
        self.messages.append(Message(role=role, content=content))

    def to_dict(self) -> dict:
        """Convert session to dictionary."""
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "project_dir": self.project_dir,
            "messages": [
                {"role": msg.role, "content": msg.content, "timestamp": msg.timestamp} for msg in self.messages
            ],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        """Create session from dictionary."""
        messages = [Message(**msg) for msg in data.get("messages", [])]
        return cls(
            session_id=data["session_id"],
            created_at=data["created_at"],
            project_dir=data["project_dir"],
            messages=messages,
            metadata=data.get("metadata", {}),
        )


class SessionManager:
    """Manages session persistence."""

    def __init__(self, project_dir: str):
        """
        Initialize session manager.

        Args:
            project_dir: Project directory
        """
        self.project_dir = Path(project_dir).resolve()
        self.session_dir = self.project_dir / ".penguincode" / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def create_session(self) -> Session:
        """
        Create a new session.

        Returns:
            New Session instance
        """
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        return Session(
            session_id=session_id,
            created_at=datetime.now().isoformat(),
            project_dir=str(self.project_dir),
        )

    def save_session(self, session: Session) -> None:
        """
        Save session to disk.

        Args:
            session: Session to save
        """
        session_file = self.session_dir / f"{session.session_id}.json"
        with open(session_file, "w") as f:
            json.dump(session.to_dict(), f, indent=2)

    def load_session(self, session_id: str) -> Session | None:
        """
        Load session from disk.

        Args:
            session_id: Session ID

        Returns:
            Session instance or None if not found
        """
        session_file = self.session_dir / f"{session_id}.json"
        if not session_file.exists():
            return None

        with open(session_file) as f:
            data = json.load(f)
        return Session.from_dict(data)

    def list_sessions(self, limit: int = 50) -> list[dict]:
        """
        List recent sessions.

        Args:
            limit: Maximum number of sessions to return

        Returns:
            List of session metadata dictionaries
        """
        session_files = sorted(self.session_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

        sessions = []
        for session_file in session_files[:limit]:
            try:
                with open(session_file) as f:
                    data = json.load(f)
                sessions.append(
                    {
                        "session_id": data["session_id"],
                        "created_at": data["created_at"],
                        "message_count": len(data.get("messages", [])),
                    }
                )
            except Exception:
                continue

        return sessions

    def delete_session(self, session_id: str) -> bool:
        """
        Delete a session.

        Args:
            session_id: Session ID

        Returns:
            True if deleted successfully
        """
        session_file = self.session_dir / f"{session_id}.json"
        if session_file.exists():
            session_file.unlink()
            return True
        return False
