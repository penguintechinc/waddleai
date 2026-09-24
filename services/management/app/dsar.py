"""Data-subject-rights (DSAR) support for the management service.

Implements the storage-agnostic logic behind the two statutory rights the
management API must offer at every licence tier (GDPR Art. 15 right of access /
export and Art. 17 right to erasure) over the single ``users`` identity table.

Per the product's PII-tokenization design all personal data lives in ``users``;
every other table references the data subject by integer id only. Erasure
therefore *anonymizes* the identity row in place rather than hard-deleting it --
foreign keys from ``api_keys``, ``token_usage``, ``usage_logs`` and friends must
keep resolving, so removing the row would break referential integrity while
adding nothing (those rows already hold no PII).

Every helper here is pure (it operates on a row-like object and returns plain
data) so the rights logic is unit-testable without a database and is reused by
both the self-service and the admin-initiated route handlers in
``api/v1/users.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# The complete set of columns in the ``users`` identity table that hold direct
# personal data. Single source of truth for both the export view and the
# erasure anonymizer: adding a PII column to the model without extending this
# tuple is exactly the drift this centralization is meant to surface.
PII_FIELDS: tuple[str, ...] = (
    "username",
    "email",
    "password_hash",
    "last_login_ip",
    "current_login_ip",
)

# Tombstone sentinels used when anonymizing. The username/email variants embed
# the row id so the ``unique`` constraints on both columns still hold after
# erasure, and use RFC 2606's reserved ``.invalid`` TLD so the address can
# never be routable. The password sentinel is not a valid bcrypt digest, so
# ``bcrypt.verify`` can never succeed against it (the account is also disabled).
ERASED_USERNAME_PREFIX = "erased-user-"
ERASED_EMAIL_DOMAIN = "deleted.invalid"
# Not a valid bcrypt digest -> bcrypt.verify can never succeed; account is also
# disabled. A sentinel, never a real credential.
ERASED_PASSWORD_SENTINEL = "!erased"  # noqa: S105 # nosec B105


def anonymized_values(user_id: int) -> dict[str, Any]:
    """Return the ``users`` column updates that erase a subject's PII in place.

    Replaces every direct-PII column with a non-reversible tombstone, clears the
    login-tracking IP fields, and disables the account. The row itself is kept
    so UUID/id references from other tables keep resolving.
    """
    return {
        "username": f"{ERASED_USERNAME_PREFIX}{user_id}",
        "email": f"erased-{user_id}@{ERASED_EMAIL_DOMAIN}",
        "password_hash": ERASED_PASSWORD_SENTINEL,
        "last_login_ip": None,
        "current_login_ip": None,
        "enabled": False,
    }


def is_erased(row: Any) -> bool:
    """True when a ``users`` row already carries the erasure tombstone."""
    username = getattr(row, "username", None) or ""
    return username.startswith(ERASED_USERNAME_PREFIX)


def _iso(value: Any) -> str | None:
    """ISO-8601 for a datetime column, ``None`` for an empty one."""
    return value.isoformat() if isinstance(value, datetime) else None


def export_identity(row: Any) -> dict[str, Any]:
    """Serialize a ``users`` row into the DSAR access payload.

    Discloses every field held about the subject in the identity table *except*
    the password hash: a data subject is entitled to know a credential digest is
    stored (the manifest states this), but returning the digest itself is a
    security risk and adds no access-right value.
    """
    return {
        "id": row.id,
        "username": row.username,
        "email": row.email,
        "role": row.role,
        "organization_id": row.organization_id,
        "enabled": row.enabled,
        "default_model": row.default_model,
        "token_quota_daily": row.token_quota_daily,
        "token_quota_monthly": row.token_quota_monthly,
        "created_at": _iso(row.created_at),
        "last_login_at": _iso(row.last_login_at),
        "current_login_at": _iso(getattr(row, "current_login_at", None)),
        "last_login_ip": getattr(row, "last_login_ip", None),
        "current_login_ip": getattr(row, "current_login_ip", None),
        "login_count": row.login_count,
    }


def data_holding_manifest() -> list[dict[str, Any]]:
    """Describe every place personal data about a user is held (GDPR Art. 15).

    The identity table holds the PII; all other tables reference the subject by
    integer id only, per the PII-tokenization design. Curated by hand (not
    reflected from the schema) so each entry carries an accurate
    ``contains_pii`` judgement and a plain-language description a data subject
    can read.
    """
    return [
        {
            "table": "users",
            "description": (
                "Identity record: username, email, password hash, role, "
                "organization, token quotas and login-tracking metadata "
                "(timestamps and IP addresses)."
            ),
            "contains_pii": True,
            "reference": "primary identity row (subject of this export/erasure)",
        },
        {
            "table": "api_keys",
            "description": "API credentials issued to the user; keys are stored hashed.",
            "contains_pii": False,
            "reference": "api_keys.user_id -> users.id",
        },
        {
            "table": "virtual_keys",
            "description": "Virtual/proxy keys issued to the user; stored hashed.",
            "contains_pii": False,
            "reference": "virtual_keys.user_id -> users.id",
        },
        {
            "table": "token_usage",
            "description": "Per-request token and cost accounting.",
            "contains_pii": False,
            "reference": "token_usage.user_id -> users.id",
        },
        {
            "table": "usage_logs",
            "description": "Historical request/usage log entries.",
            "contains_pii": False,
            "reference": "usage_logs.user_id -> users.id",
        },
        {
            "table": "security_logs",
            "description": "Authentication and security events attributed to the user.",
            "contains_pii": False,
            "reference": "security_logs.user_id -> users.id",
        },
        {
            "table": "content_filter_audit_log",
            "description": (
                "Content-filter decisions attributed to the user; may retain a "
                "short sampled excerpt of request content for allow/block events."
            ),
            "contains_pii": False,
            "reference": "content_filter_audit_log.user_id -> users.id",
        },
    ]
