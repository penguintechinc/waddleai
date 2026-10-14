"""``waddleai.disable-teams`` opt-out kill switch (the core-mechanism pattern).

Unseen/OFF = teams ON; flag ON = legacy teamless behaviour. Resolved through the shared
feature-flag layer so ``WADDLEAI_FLAG_DISABLE_TEAMS`` is the env override, exactly like every
other ``waddleai.disable-*`` switch.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from shared.auth.team_flags import DISABLE_TEAMS_FLAG, teams_enabled
from shared.utils import feature_flags


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start every test with no env override and a clean flag cache."""
    monkeypatch.delenv("WADDLEAI_FLAG_DISABLE_TEAMS", raising=False)
    feature_flags.reset_for_testing()
    yield
    feature_flags.reset_for_testing()


def test_flag_key_follows_the_waddleai_disable_convention() -> None:
    """The kill switch is named ``waddleai.disable-teams`` (opt-out pattern)."""
    assert DISABLE_TEAMS_FLAG == "waddleai.disable-teams"


def test_teams_are_on_when_the_flag_is_unseen() -> None:
    """Unseen/OFF flag -> mechanism ON (the default for every deployment)."""
    assert teams_enabled() is True


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_env_override_turns_teams_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """``WADDLEAI_FLAG_DISABLE_TEAMS`` truthy -> kill switch ON -> teams disabled."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", value)
    assert teams_enabled() is False


def test_env_override_false_keeps_teams_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit falsy override leaves the mechanism on."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "0")
    assert teams_enabled() is True


def test_flag_layer_failure_fails_open_to_teams_on() -> None:
    """If flag resolution itself blows up, teams stay ON (never strand a deployment) and it logs."""
    with patch(
        "shared.auth.team_flags.is_feature_enabled", side_effect=RuntimeError("flag store down")
    ):
        assert teams_enabled() is True
