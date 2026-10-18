"""The ``waddleai.pooled_quota`` PostHog flag (spec D12): default OFF, per-org rollout."""

import logging

from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

#: Env override is ``WADDLEAI_FLAG_POOLED_QUOTA`` (tests/alpha); ``distinct_id`` = ``str(org_id)``.
POOLED_QUOTA_FLAG = "waddleai.pooled_quota"


def pooled_quota_enabled(distinct_id: str) -> bool:
    """True only when the flag is ON for this org; any failure resolving it reads as OFF."""
    try:
        return bool(is_feature_enabled(POOLED_QUOTA_FLAG, distinct_id=distinct_id, default=False))
    except Exception:  # noqa: BLE001 -- flag layer is documented never-raises; belt and braces
        logger.warning("quota: %s lookup failed; defaulting OFF", POOLED_QUOTA_FLAG)
        return False
