#!/usr/bin/env python3
# ruff: noqa: E501, S311
"""Generate the routing golden fixture by RUNNING the Python RoutingStage.

Builds an in-memory "world" (model_configs, aliases, assignments, policy,
heuristic rules) per scenario, drives the real ``RoutingStage.__call__`` (real
``RoutingEngine``, ``AliasResolver``, ``PolicyResolver``, capability veto,
sensitivity clamp, escalation, ``RoutingStage._load_offers``) with only the
penguin-dal row fetches replaced by in-memory lookups, and records the decision
(model, fallback_chain, routed_from). The Rust ``routing`` crate asserts it
reproduces every row. Never hand-edit the output; re-run this script.

Usage (repo root, venv with tiktoken + the proxy deps):
    PYTHONPATH=. python rust/scripts/gen-routing-golden.py \
        > rust/crates/routing/tests/fixtures/python_golden.json
"""

import asyncio
import json
import random
import sys
from types import SimpleNamespace

from proxy.apps.proxy_server.pipeline.stages import PipelineContext, RoutingStage
from shared.routing import engine as engine_mod
from shared.routing.assignments import Assignment
from shared.routing.engine import RoutingEngine
from shared.routing.heuristics import HeuristicRule
from shared.routing.policy import RoutingPolicyConfig

_POLICY_FIELDS = (
    "mode",
    "escalation_threshold",
    "escalation_target",
    "classifier_prompt",
    "de_escalation",
    "idle_reset_minutes",
    "sensitivity_routing",
    "budget_pressure_enabled",
    "provider_failover",
)


def _model(name, providers, cost, ctx, caps, enabled=True):
    """One model_configs row."""
    return {
        "model_name": name,
        "preferred_providers": providers,
        "cost_per_token": cost,
        "context_length": ctx,
        "capabilities": caps,
        "enabled": enabled,
    }


MODELS = [
    _model("llama3", ["ollama"], {"ollama": 0.0}, 8192, ["chat"]),
    _model("gemma4:e4b", ["ollama"], {"ollama": 0.0}, 0, ["chat"]),
    _model(
        "mistral-large", ["ollama", "llamacpp"], {"ollama": 0.0, "llamacpp": 0.0}, 32768, ["chat"]
    ),
    _model("llava", ["ollama"], {"ollama": 0.0}, 8192, ["chat", "vision"]),
    _model("tiny", ["llamacpp"], {"llamacpp": 0.0}, 256, []),
    _model("gpt-4", ["openai"], {"openai": 0.00003}, 8192, ["chat"]),
    _model("gpt-4o", ["openai"], {"openai": 0.000005}, 128000, ["chat", "vision"]),
    _model(
        "claude-sonnet",
        ["anthropic"],
        {"anthropic": 0.000003, "bedrock": 0.000004},
        200000,
        ["chat"],
    ),
    _model("cheap-cloud", ["gemini"], {}, 65536, []),
    _model("disabled-model", ["ollama"], {"ollama": 0.0}, 8192, [], enabled=False),
]

ALIASES = [
    {"org": None, "source": "gpt-3.5-turbo", "target": "llama3", "provider": None, "enabled": True},
    {"org": None, "source": "gpt-4", "target": "gpt-4o", "provider": None, "enabled": True},
    {"org": 7, "source": "gpt-4", "target": "mistral-large", "provider": "ollama", "enabled": True},
    {
        "org": None,
        "source": "alias-to-missing",
        "target": "nope",
        "provider": None,
        "enabled": True,
    },
    {"org": None, "source": "off-alias", "target": "llama3", "provider": None, "enabled": False},
    {"org": None, "source": "llama2", "target": "llama3", "provider": "llamacpp", "enabled": True},
]

ASSIGNMENTS = [
    {
        "scope": "global",
        "ref": None,
        "tool_type": "code-gen",
        "model": "mistral-large",
        "esc": "gpt-4",
    },
    {
        "scope": "global",
        "ref": None,
        "tool_type": "general",
        "model": "llama3",
        "esc": "claude-sonnet",
    },
    {"scope": "global", "ref": None, "tool_type": "vision-task", "model": "llama3", "esc": None},
    {
        "scope": "global",
        "ref": None,
        "tool_type": "ghost",
        "model": "nonexistent-model",
        "esc": None,
    },
    {"scope": "org", "ref": 7, "tool_type": "code-gen", "model": "gemma4:e4b", "esc": None},
]

RULES = [
    {"priority": 20, "match": {"model_prefix": "claude"}, "action": {"tool_type": "vision-task"}},
    {"priority": 10, "match": {"model_prefix": "gpt-4"}, "action": {"tool_type": "code-gen"}},
    {"priority": 5, "match": {"has_image": True}, "action": {"tool_type": "ghost"}},
]


def _world(name, policy, assignments=True, rules=False, aliases=True):
    """Compose a named world (policy None = no routing_policies row)."""
    return {
        "name": name,
        "models": MODELS,
        "aliases": ALIASES if aliases else [],
        "assignments": ASSIGNMENTS if assignments else [],
        "policy": policy,
        "rules": RULES if rules else [],
    }


WORLDS = [
    _world("default_policy", None),
    _world("local_only", {"mode": "local_only"}),
    _world("commercial_only", {"mode": "commercial_only"}),
    _world("cost", {"mode": "cost"}),
    _world("latency", {"mode": "latency"}),
    _world("weird_mode", {"mode": "weird", "sensitivity_routing": "weird"}),
    _world("redact_then_any", {"mode": "local_first", "sensitivity_routing": "redact_then_any"}),
    _world("sensitivity_ignore", {"mode": "cost", "sensitivity_routing": "ignore"}),
    _world(
        "escalation_target",
        {"mode": "local_first", "escalation_target": "gpt-4o"},
        assignments=False,
    ),
    _world("no_assignments", None, assignments=False),
    _world("heuristic_rules", None, rules=True),
    _world("no_aliases", {"mode": "cost"}, aliases=False),
]

MODEL_INPUTS = [
    None,
    "gpt-4",
    "gpt-3.5-turbo",
    "gemma4:e4b",
    "ollama:llama2",
    "ollama:gpt-4",
    "openai:gpt-4",
    "waddleai/code-gen",
    "waddleai/ghost",
    "claude-sonnet",
    "alias-to-missing",
    "unknown-model",
    "llama3",
    "off-alias",
    "llama2",
]
HINTS = [None, None, "vision-task", "code-gen", "general", "ghost"]
ESCALATES = [None, None, None, "true", "auto:high", "auto:low", "bogus"]
LONG = "word " * 3000
BODIES = {
    "plain": {"messages": [{"role": "user", "content": "hello there"}]},
    "tools": {"messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function"}]},
    "tool_choice_none": {"messages": [{"role": "user", "content": "hi"}], "tool_choice": "none"},
    "tool_choice_auto": {"messages": [{"role": "user", "content": "hi"}], "tool_choice": "auto"},
    "vision": {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "x"}},
                ],
            }
        ]
    },
    "long": {"messages": [{"role": "user", "content": LONG}], "max_tokens": 2000},
    "huge": {"messages": [{"role": "user", "content": LONG * 4}]},
    "max_tokens_float": {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 9000.9},
    "special_token": {"messages": [{"role": "user", "content": "x <|endoftext|> y"}]},
    "empty": {},
}


class _Query:
    """Records the single predicate ``db(<expr>)`` the stage builds."""

    def __eq__(self, other):
        return self


class _Table:
    """Attribute sink so ``db.model_configs.enabled == True`` is accepted."""

    def __getattr__(self, _):
        return _Query()


class _Select:
    """``db(expr).select()`` returning the enabled model rows."""

    def __init__(self, rows):
        self._rows = rows

    def select(self):
        return [r for r in self._rows if r.enabled]


class _FakeDb:
    """Minimal penguin-dal stand-in for RoutingStage._load_offers."""

    def __init__(self, models):
        self.model_configs = _Table()
        self._rows = [SimpleNamespace(**m) for m in models]

    def __call__(self, _expr):
        return _Select(self._rows)


async def _noop_persist(_db, _trace):
    """Trace persistence is out of scope for the decision golden."""


def _install(engine, world):
    """Swap only the row-fetch seams of the real resolvers for in-memory lookups."""

    def alias_fetch(bare_model, org_id):
        rows = [a for a in world["aliases"] if a["enabled"] and a["source"] == bare_model]
        pick = None
        if org_id is not None:
            pick = next((a for a in rows if a["org"] == org_id), None)
        if pick is None:
            pick = next((a for a in rows if a["org"] is None), None)
        if pick is None:
            return None
        return SimpleNamespace(target_model=pick["target"], target_provider=pick["provider"])

    def assign_fetch(tool_type, org_id):
        rows = [a for a in world["assignments"] if a["tool_type"] == tool_type]
        pick = None
        if org_id is not None:
            pick = next((a for a in rows if a["scope"] == "org" and a["ref"] == org_id), None)
        if pick is None:
            pick = next((a for a in rows if a["scope"] == "global"), None)
        if pick is None:
            return None
        return Assignment(
            tool_type=tool_type, default_model=pick["model"], escalation_model=pick["esc"]
        )

    def policy_fetch(_org_id):
        if world["policy"] is None:
            return RoutingPolicyConfig()
        return RoutingPolicyConfig(
            **{k: v for k, v in world["policy"].items() if k in _POLICY_FIELDS}
        )

    engine.aliases._fetch = alias_fetch
    engine.assignments._fetch = assign_fetch
    engine.policies._fetch = policy_fetch


async def run_row(world, req):
    """Run the real Python RoutingStage for one request and return its decision."""
    engine = RoutingEngine(db=None, valkey=None, classifier_client=None)
    _install(engine, world)
    rules = [
        HeuristicRule(priority=r["priority"], match=r["match"], action=r["action"])
        for r in world["rules"]
    ]
    stage = RoutingStage("routing", engine, _FakeDb(world["models"]), rules=rules)
    org = req["org_id"]
    ctx = PipelineContext(
        user=SimpleNamespace(tenant_id=org, request_id="golden"),
        body=BODIES[req["body"]],
        model=req["model"],
    )
    ctx.explicit_tool_type_hint = req["hint"]
    ctx.escalate_hint = req["escalate"]
    ctx.pii_detected = req["pii"]
    before = ctx.model
    await stage(ctx)
    return {
        "model": ctx.model,
        "fallback_chain": ctx.fallback_chain,
        "routed_from": ctx.routed_from,
        "unchanged": ctx.model == before and ctx.routed_from is None and not ctx.fallback_chain,
    }


def main():
    """Emit the golden JSON (worlds + rows with Python-computed decisions)."""
    engine_mod.persist_trace = _noop_persist
    rng = random.Random(20261030)
    rows = []
    for world in WORLDS:
        seen = set()
        reqs = []
        # Deterministic stratified sample plus the corner cases every world must carry.
        for model in MODEL_INPUTS:
            reqs.append(
                {
                    "model": model,
                    "hint": None,
                    "escalate": None,
                    "pii": False,
                    "body": "plain",
                    "org_id": 0,
                }
            )
            reqs.append(
                {
                    "model": model,
                    "hint": None,
                    "escalate": None,
                    "pii": True,
                    "body": "plain",
                    "org_id": 7,
                }
            )
        for body in BODIES:
            reqs.append(
                {
                    "model": "llama3",
                    "hint": None,
                    "escalate": None,
                    "pii": False,
                    "body": body,
                    "org_id": 0,
                }
            )
            reqs.append(
                {
                    "model": None,
                    "hint": "general",
                    "escalate": None,
                    "pii": False,
                    "body": body,
                    "org_id": 7,
                }
            )
        while len(reqs) < 90:
            reqs.append(
                {
                    "model": rng.choice(MODEL_INPUTS),
                    "hint": rng.choice(HINTS),
                    "escalate": rng.choice(ESCALATES),
                    "pii": rng.random() < 0.4,
                    "body": rng.choice(list(BODIES)),
                    "org_id": rng.choice([0, 7]),
                }
            )
        for req in reqs:
            key = json.dumps(req, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "world": world["name"],
                    "request": req,
                    "expected": asyncio.run(run_row(world, req)),
                }
            )
    out = {
        "bodies": BODIES,
        "worlds": {w["name"]: w for w in WORLDS},
        "rows": rows,
    }
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    print(f"rows={len(rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
