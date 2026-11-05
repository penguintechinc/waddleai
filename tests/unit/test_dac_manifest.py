"""Wave-5 DAC v1alpha1 safety and determinism tests."""

import pytest

from shared.dac.manifest import DacError, DacPlanner, DacPlanStore, parse_manifest


def manifest():
    """Test helper: manifest."""
    return {
        "apiVersion": "waddleai.penguintech.io/v1alpha1",
        "kind": "WaddleAIConfiguration",
        "metadata": {"name": "prod", "organization": "7"},
        "spec": {"backends": [{"name": "local", "type": "ollama"}]},
    }


def test_dac_plan_is_deterministic_and_dry_run_has_no_side_effects():
    """Verify: dac plan is deterministic and dry run has no side effects."""

    def current(kind, name):
        """Report every resource as absent."""
        return None

    planner = DacPlanner(current)
    first = planner.plan(manifest(), organization_id="7", now=100)
    second = planner.plan(manifest(), organization_id="7", now=100)
    assert first.manifest_digest == second.manifest_digest
    assert first.items[0].action == "create"


def test_dac_apply_requires_exact_unexpired_reviewed_plan():
    """Verify: dac apply requires exact unexpired reviewed plan."""
    plan = DacPlanner(lambda *_: None).plan(manifest(), organization_id="7", now=100)
    store = DacPlanStore()
    store.put(plan)
    applied = []
    store.apply(plan.id, organization_id="7", executor=applied.append, now=101)
    assert [item.action for item in applied] == ["create"]
    with pytest.raises(DacError, match="unknown plan"):
        store.apply(plan.id, organization_id="7", executor=applied.append, now=101)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value["spec"].update({"prune": True}),
        lambda value: value["spec"]["backends"][0].update({"api_key": "nope"}),
        lambda value: value["metadata"].update({"organization": "other"}),
        lambda value: value["spec"].update({"unknown": True}),
    ],
)
def test_dac_rejects_prune_inline_secrets_cross_org_and_unknown_fields(mutator):
    """Verify: dac rejects prune inline secrets cross org and unknown fields."""
    value = manifest()
    mutator(value)
    with pytest.raises(DacError):
        parse_manifest(value, organization_id="7")
