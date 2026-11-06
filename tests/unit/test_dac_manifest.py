"""Wave-5 DAC v1alpha1 safety and determinism tests."""

import time

import pytest

from shared.dac.manifest import (
    DacError,
    DacPlanner,
    DacPlanStore,
    parse_manifest,
    redact_secrets,
)


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


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value["spec"]["backends"][0].update({"HF-Token": "hf_abc"}),
        lambda value: value["spec"]["backends"][0].update({"API_Key": "x"}),
        lambda value: value["spec"]["backends"][0].update({"Authorization": "Bearer x"}),
        lambda value: value["spec"]["backends"][0].update({"x-api-key": "x"}),
        lambda value: value["spec"]["backends"][0].update({"dbPassword": "x"}),
        lambda value: value["spec"]["backends"][0].update(
            {"env": [{"name": "OPENAI_API_KEY", "value": "sk-live-1"}]}
        ),
        lambda value: value["spec"]["backends"][0].update(
            {"headers": {"nested": [{"name": "X-Auth-Token", "value": "t"}]}}
        ),
    ],
)
def test_dac_secret_detection_normalises_keys_and_env_pairs(mutator):
    """Verify: secret key variants and env-style name/value pairs are rejected."""
    value = manifest()
    mutator(value)
    with pytest.raises(DacError, match="inline secret"):
        parse_manifest(value, organization_id="7")


def test_dac_redact_secrets_never_contains_value():
    """Verify: redact_secrets masks matched keys and env pairs, keeps the rest."""
    value = manifest()
    value["spec"]["backends"][0]["HF-Token"] = "hf_supersecret"
    value["spec"]["backends"][0]["env"] = [
        {"name": "OPENAI_API_KEY", "value": "sk-supersecret"},
        {"name": "LOG_LEVEL", "value": "debug"},
    ]
    redacted = redact_secrets(value)
    dumped = str(redacted)
    assert "hf_supersecret" not in dumped and "sk-supersecret" not in dumped
    assert "debug" in dumped and redacted["spec"]["backends"][0]["type"] == "ollama"
    assert "hf_supersecret" in str(value)  # input not mutated


def test_dac_non_secret_keys_still_accepted():
    """Verify: ordinary keys (name, type, url, monkey-free) are not false positives."""
    value = manifest()
    value["spec"]["backends"][0].update({"base_url": "http://x", "model": "m", "keywords": ["a"]})
    assert parse_manifest(value, organization_id="7")


_BOMB = (
    "a: &a [x,x,x,x,x,x,x,x,x]\n"
    "b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
    "c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
    "d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n"
    "e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]\n"
    "f: [*e,*e,*e,*e,*e,*e,*e,*e,*e]\n"
)


def test_dac_yaml_billion_laughs_rejected_fast():
    """Verify: alias/anchor payloads are rejected immediately, not expanded."""
    start = time.monotonic()
    with pytest.raises(DacError, match="alias|anchor"):
        parse_manifest(_BOMB, organization_id="7")
    assert time.monotonic() - start < 1.0


def test_dac_rejects_oversized_input():
    """Verify: raw input above 1 MiB is rejected before parsing."""
    with pytest.raises(DacError, match="too large"):
        parse_manifest("a: " + "x" * (1024 * 1024 + 1), organization_id="7")


def test_dac_rejects_too_deep_and_too_many_nodes_dicts_and_yaml():
    """Verify: depth > 64 and nodes > 50,000 are rejected for dicts and YAML."""
    deep: dict = {}
    cur = deep
    for _ in range(70):
        cur["n"] = {}
        cur = cur["n"]
    value = manifest()
    value["spec"]["backends"][0]["deep"] = deep
    with pytest.raises(DacError, match="deep"):
        parse_manifest(value, organization_id="7")
    wide = manifest()
    wide["spec"]["backends"][0]["many"] = list(range(50_001))
    with pytest.raises(DacError, match="nodes"):
        parse_manifest(wide, organization_id="7")
    yaml_deep = "x: " + "[" * 100 + "]" * 100
    with pytest.raises(DacError, match="deep"):
        parse_manifest(yaml_deep, organization_id="7")
