"""Quota config rules: validation, allocation invariants, upserts, tenancy, cleanup."""

from datetime import date

import pytest

from services.management.app.services import quota_pools as qp
from services.management.app.services import teams as team_svc
from services.management.app.services.quota_pools import QuotaError


def _team(w, key="a1"):
    """The ``teams`` row for a team_world team key."""
    return w.db(w.db.teams.id == w.t[key]).select().first()


def _org_ready(w, pool=1000):
    """Org a with an anchor and an enabled pool of ``pool`` credits."""
    qp.set_org_pool(
        w.db, w.org["a"], cycle_anchor_date=date(2026, 1, 1), pool_credits=pool, enabled=True
    )


@pytest.mark.parametrize("bad", [True, -1, "5", 1.5, 10**12 + 1])
def test_credit_values_must_be_bounded_non_negative_integers(team_world, bad):
    """bool/str/float/negative/huge are all refused with 400 (and never persisted)."""
    w = team_world
    with pytest.raises(QuotaError) as exc:
        qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=bad, enabled=None)
    assert exc.value.status == 400
    assert int(qp.get_org_pool_row(w.db, w.org["a"]).pool_credits) == 0


def test_enabling_requires_an_anchor_and_all_none_is_a_noop(team_world):
    """Enable needs an anchor (400); anchor+enable works; an empty call is a no-op."""
    w = team_world
    with pytest.raises(QuotaError) as exc:
        qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=10, enabled=True)
    assert exc.value.status == 400
    _org_ready(w)
    row = qp.get_org_pool_row(w.db, w.org["a"])
    assert (bool(row.enabled), int(row.pool_credits)) == (True, 1000)
    qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=None, enabled=None)
    assert int(qp.get_org_pool_row(w.db, w.org["a"]).pool_credits) == 1000


def test_org_pool_cannot_drop_below_allocated_team_pools(team_world):
    """Lowering the org pool under the sum of team pools is a 409."""
    w = team_world
    _org_ready(w)
    qp.set_team_pool(w.db, w.org["a"], _team(w), 600)
    with pytest.raises(QuotaError) as exc:
        qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=500, enabled=None)
    assert exc.value.status == 409


def test_team_pool_needs_org_pool_and_respects_org_total(team_world):
    """Team pools: org pool must exist & be configured; the sum of team pools stays within it."""
    w = team_world
    with pytest.raises(QuotaError) as exc:
        qp.set_team_pool(w.db, w.org["a"], _team(w), 10)
    assert exc.value.status == 409
    _org_ready(w, 1000)
    qp.set_team_pool(w.db, w.org["a"], _team(w, "a1"), 700)
    with pytest.raises(QuotaError) as exc:
        qp.set_team_pool(w.db, w.org["a"], _team(w, "a2"), 400)
    assert exc.value.status == 409
    qp.set_team_pool(w.db, w.org["a"], _team(w, "a2"), 300)


def test_buffer_and_reservations_share_the_team_pool(team_world):
    """sum(guaranteed) + buffer <= team pool; a team pool cannot drop below what is committed."""
    w = team_world
    _org_ready(w)
    team = _team(w)
    with pytest.raises(QuotaError) as exc:
        qp.set_team_buffer(w.db, team, 10)  # no team pool yet
    assert exc.value.status == 409
    qp.set_team_pool(w.db, w.org["a"], team, 100)
    qp.set_team_buffer(w.db, team, 30)
    qp.set_user_reservation(w.db, team, w.u["mia"], 50, 20)
    with pytest.raises(QuotaError) as exc:
        qp.set_user_reservation(w.db, team, w.u["adam"], 30, None)  # 50+30+30 > 100
    assert exc.value.status == 409
    with pytest.raises(QuotaError) as exc:
        qp.set_team_buffer(w.db, team, 60)  # 50+60 > 100
    assert exc.value.status == 409
    with pytest.raises(QuotaError) as exc:
        qp.set_team_pool(w.db, w.org["a"], team, 70)  # below 50+30 committed
    assert exc.value.status == 409
    assert qp.team_reserved(w.db, team.id) == 50


def test_reservation_upserts_validates_membership_and_deletes(team_world):
    """Second PUT updates the row; non-member 404; subcap >= 1; delete is idempotent."""
    w = team_world
    _org_ready(w)
    team = _team(w)
    qp.set_team_pool(w.db, w.org["a"], team, 100)
    qp.set_user_reservation(w.db, team, w.u["mia"], 10, None)
    qp.set_user_reservation(w.db, team, w.u["mia"], 25, 5)
    rows = list(w.db(w.db.quota_user_reservation.user_id == w.u["mia"]).select())
    assert [(int(r.guaranteed_credits), int(r.daily_subcap)) for r in rows] == [(25, 5)]
    with pytest.raises(QuotaError) as exc:
        qp.set_user_reservation(w.db, team, w.u["nat"], 1, None)  # nat is in team a2
    assert exc.value.status == 404
    with pytest.raises(QuotaError) as exc:
        qp.set_user_reservation(w.db, team, w.u["mia"], 1, 0)
    assert exc.value.status == 400
    assert qp.delete_user_reservation(w.db, team, w.u["mia"]) is True
    assert qp.delete_user_reservation(w.db, team, w.u["mia"]) is False


def test_overrides_need_a_dimension_same_tenant_user_and_are_org_scoped(team_world):
    """No dimension 400; other-org user 404; other-org delete finds nothing."""
    w = team_world
    with pytest.raises(QuotaError) as exc:
        qp.create_override(w.db, w.org["a"], user_id=None, model=" ", provider=None, cap_credits=10)
    assert exc.value.status == 400
    with pytest.raises(QuotaError) as exc:
        qp.create_override(
            w.db, w.org["a"], user_id=w.u["carl"], model=None, provider=None, cap_credits=10
        )
    assert exc.value.status == 404
    oid = qp.create_override(
        w.db, w.org["a"], user_id=w.u["mia"], model=None, provider="openai", cap_credits=10
    )
    assert [int(r.id) for r in qp.list_overrides(w.db, w.org["a"])] == [oid]
    assert qp.list_overrides(w.db, w.org["b"]) == []
    assert qp.delete_override(w.db, w.org["b"], oid) is False
    assert qp.delete_override(w.db, w.org["a"], oid) is True


def test_removing_a_member_or_deleting_a_team_cleans_quota_rows(team_world):
    """Removed member keeps no reservation; deleted team leaves no pool rows."""
    w = team_world
    _org_ready(w)
    team = _team(w)
    qp.set_team_pool(w.db, w.org["a"], team, 100)
    qp.set_user_reservation(w.db, team, w.u["mia"], 10, None)
    team_svc.remove_member(w.db, team, w.u["mia"], can_manage_owner=True)
    assert list(w.db(w.db.quota_user_reservation.user_id == w.u["mia"]).select()) == []
    team_svc.delete_team(w.db, team)
    assert list(w.db(w.db.quota_team_pool.team_id == w.t["a1"]).select()) == []


def _pool_team(w, org_pool=1000, team_pool=100):
    """Org a ready with ``org_pool`` and team a1 allocated ``team_pool``."""
    _org_ready(w, org_pool)
    team = _team(w)
    qp.set_team_pool(w.db, w.org["a"], team, team_pool)
    return team


def test_org_pool_shrink_boundary(team_world):
    """Shrink to exactly allocated is allowed; one below is 409."""
    w = team_world
    _org_ready(w)
    qp.set_team_pool(w.db, w.org["a"], _team(w), 600)
    with pytest.raises(QuotaError) as exc:
        qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=599, enabled=None)
    assert exc.value.status == 409
    qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=600, enabled=None)
    assert int(qp.get_org_pool_row(w.db, w.org["a"]).pool_credits) == 600


def test_team_pool_vs_org_boundary_over_by_one(team_world):
    """Team pools summing to org pool + 1 are 409; exactly the org pool is allowed."""
    w = team_world
    _org_ready(w, 1000)
    qp.set_team_pool(w.db, w.org["a"], _team(w, "a1"), 700)
    with pytest.raises(QuotaError) as exc:
        qp.set_team_pool(w.db, w.org["a"], _team(w, "a2"), 301)
    assert exc.value.status == 409
    qp.set_team_pool(w.db, w.org["a"], _team(w, "a2"), 300)


def test_team_pool_shrink_boundary(team_world):
    """Shrink to exactly reserved+buffer allowed; one below is 409."""
    w = team_world
    team = _pool_team(w)
    qp.set_team_buffer(w.db, team, 30)
    qp.set_user_reservation(w.db, team, w.u["mia"], 50, None)
    with pytest.raises(QuotaError) as exc:
        qp.set_team_pool(w.db, w.org["a"], team, 79)
    assert exc.value.status == 409
    qp.set_team_pool(w.db, w.org["a"], team, 80)


def test_buffer_boundary(team_world):
    """Buffer filling the pool exactly is allowed; pool + 1 is 409."""
    w = team_world
    team = _pool_team(w)
    qp.set_user_reservation(w.db, team, w.u["mia"], 40, None)
    with pytest.raises(QuotaError) as exc:
        qp.set_team_buffer(w.db, team, 61)
    assert exc.value.status == 409
    qp.set_team_buffer(w.db, team, 60)


def test_reservation_boundary(team_world):
    """Reservation filling the pool exactly is allowed; pool + 1 is 409."""
    w = team_world
    team = _pool_team(w)
    qp.set_team_buffer(w.db, team, 40)
    with pytest.raises(QuotaError) as exc:
        qp.set_user_reservation(w.db, team, w.u["mia"], 61, None)
    assert exc.value.status == 409
    qp.set_user_reservation(w.db, team, w.u["mia"], 60, None)


def test_set_team_pool_rejects_org_team_mismatch(team_world):
    """A team of org b passed with org a's id is 404 and writes nothing."""
    w = team_world
    _org_ready(w)
    with pytest.raises(QuotaError) as exc:
        qp.set_team_pool(w.db, w.org["a"], _team(w, "b1"), 10)
    assert exc.value.status == 404
    assert list(w.db(w.db.quota_team_pool.team_id == w.t["b1"]).select()) == []


def test_org_pool_first_insert_integrity_error_is_409(team_world, monkeypatch):
    """A lost first-insert race maps to 409."""
    from sqlalchemy.exc import IntegrityError

    w = team_world

    def boom(self, **_kw):
        raise IntegrityError("insert", {}, Exception("dup"))

    monkeypatch.setattr(type(w.db.quota_org_pool), "insert", boom)
    with pytest.raises(QuotaError) as exc:
        qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=1, enabled=None)
    assert exc.value.status == 409


def test_other_orgs_team_pools_do_not_count_toward_sum(team_world):
    """Org b's team pool is excluded from org a's sum(team) <= org pool check."""
    w = team_world
    _org_ready(w, 1000)
    w.db.quota_team_pool.insert(
        team_id=w.t["b1"], organization_id=w.org["b"], pool_credits=900, shared_buffer_credits=0
    )
    assert qp.allocated_to_teams(w.db, w.org["a"]) == 0
    qp.set_team_pool(w.db, w.org["a"], _team(w, "a1"), 1000)
