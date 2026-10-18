"""QuotaLimiter wrapper behaviour with a scripted fake client: args, fail-open, NOSCRIPT retry."""

import pytest
from redis.exceptions import NoScriptError

from shared.quota import keys
from shared.quota.lease import QuotaLimiter
from shared.quota.types import OverrideRule


class FakeValkey:
    """Scripted async client: evalsha pops responses (exceptions are raised)."""

    def __init__(self, *responses):
        """Queue the scripted evalsha responses."""
        self.responses, self.calls, self.loads = list(responses), [], 0

    async def script_load(self, script):
        """Return a fresh fake sha and count the load."""
        self.loads += 1
        return f"sha{self.loads}"

    async def evalsha(self, sha, numkeys, *args):
        """Record the call and replay the next scripted response."""
        self.calls.append((sha, numkeys, args))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _kw(**over):
    """Default reserve() kwargs."""
    base = {
        "user_id": 1, "team_id": 2, "cycle": 3, "day": "20260203", "credits": 5,
        "guaranteed": 10, "buffer_cap": 7, "daily_subcap": None, "overrides": (),
    }  # fmt: skip
    return {**base, **over}


async def test_allowed_result_is_parsed_and_args_are_positioned():
    """Keys: user, buffer, day, resv, index, overrides; ARGV carries caps after fixed args."""
    fake = FakeValkey([1, "", 4, 1])
    rule = OverrideRule(id=8, cap_credits=99, user_id=1)
    d = await QuotaLimiter(fake, clock=lambda: 50.0).reserve(
        **_kw(overrides=(rule,), daily_subcap=6)
    )
    assert (d.allowed, d.from_reservation, d.from_buffer, d.degraded) == (True, 4, 1, False)
    _sha, numkeys, args = fake.calls[0]
    assert numkeys == 6
    assert args[:2] == (keys.user_counter(1, 3), keys.buffer_counter(2, 3))
    assert args[2] == keys.daily_counter(1, "20260203") and args[4] == keys.RESERVATION_INDEX
    assert args[5] == keys.override_counter(8, 3)
    argv = args[6:]
    assert argv[:4] == ("5", "10", "7", "6") and argv[9] == "1" and argv[-1] == "99"


async def test_denied_result_with_bytes_reason_is_decoded():
    """A client without decode_responses returns bytes; the reason must still be a str."""
    d = await QuotaLimiter(FakeValkey([0, b"quota_exhausted", 0, 0])).reserve(**_kw())
    assert (d.allowed, d.reason, d.reservation_id) == (False, "quota_exhausted", None)


@pytest.mark.parametrize("bad", [ConnectionError("down"), None, [1], "nope"])
async def test_any_failure_or_malformed_reply_fails_open(bad, caplog):
    """D10: outage / garbage => allowed + degraded, never an exception."""
    with caplog.at_level("WARNING"):
        d = await QuotaLimiter(FakeValkey(bad)).reserve(**_kw())
    assert (d.allowed, d.degraded, d.reservation_id) == (True, True, None)


async def test_noscript_reloads_and_retries_once():
    """A NOSCRIPT reply triggers one script_load + retry, then succeeds."""
    fake = FakeValkey(NoScriptError("NOSCRIPT"), [1, "", 5, 0])
    d = await QuotaLimiter(fake).reserve(**_kw())
    assert d.allowed and not d.degraded and fake.loads == 2 and len(fake.calls) == 2
