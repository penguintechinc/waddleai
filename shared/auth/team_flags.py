"""Kill switch for the teams subsystem (opt-out pattern, ``critical-rules.md``).

``waddleai.disable-teams``: unseen/OFF (the default) leaves teams ON; ON reverts every
team-aware code path to the pre-teams, teamless behaviour -- no team permissions expanded, no
acting team resolved, the team API refusing. Kept in its own tiny module (no ``rbac`` import)
so ``shared.auth.rbac`` and the management service can both use it without an import cycle.
"""

import logging

from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

#: Opt-out kill-switch flag key; env override is ``WADDLEAI_FLAG_DISABLE_TEAMS``.
DISABLE_TEAMS_FLAG = "waddleai.disable-teams"


def teams_enabled(distinct_id: str = "server") -> bool:
    """Return True unless the ``waddleai.disable-teams`` kill switch is ON.

    ``distinct_id`` lets PostHog target the switch at one org (pass ``str(org_id)``). Any
    failure resolving the flag fails open to teams ON -- a flag-store outage must never strand
    a deployment on the legacy path or break auth -- and is logged at WARNING.
    """
    try:
        return not is_feature_enabled(DISABLE_TEAMS_FLAG, distinct_id=distinct_id, default=False)
    except Exception:  # noqa: BLE001 -- flag layer is documented never-raises; belt and braces
        logger.warning("teams: %s lookup failed; defaulting to teams ON", DISABLE_TEAMS_FLAG)
        return True
