//! Routing stage tests. The pure decision logic and the golden comparison run without external
//! services; the database source test is `#[ignore]`d and needs `WADDLEAI_TEST_DATABASE_URL`.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use pipeline::{PipelineContext, Stage};
use routing::{
    build_offers, decide, AliasRow, AssignmentRow, HeuristicRule, OrgRouting, RawModelConfig,
    RegistryCache, RegistryError, RegistrySource, RoutingMeta, RoutingPolicy, RoutingRequest,
    RoutingStage,
};
use rstest::rstest;
use serde_json::{json, Value};

fn model(name: &str, providers: Value, cost: Value, ctx: i64, caps: Value) -> RawModelConfig {
    RawModelConfig {
        model_name: name.into(),
        preferred_providers: providers,
        cost_per_token: cost,
        context_length: ctx,
        capabilities: caps,
        enabled: true,
    }
}

fn models() -> Vec<RawModelConfig> {
    vec![
        model(
            "llama3",
            json!(["ollama"]),
            json!({"ollama": 0.0}),
            8192,
            json!(["chat"]),
        ),
        model(
            "mistral-large",
            json!(["ollama"]),
            json!({"ollama": 0.0}),
            32768,
            json!(["chat"]),
        ),
        model(
            "llava",
            json!(["ollama"]),
            json!({"ollama": 0.0}),
            8192,
            json!(["chat", "vision"]),
        ),
        model(
            "gpt-4",
            json!(["openai"]),
            json!({"openai": 0.00003}),
            8192,
            json!(["chat"]),
        ),
        model(
            "claude-sonnet",
            json!(["anthropic"]),
            json!({"anthropic": 0.000003, "bedrock": 0.000004}),
            200_000,
            json!(["chat"]),
        ),
    ]
}

fn assign(tt: &str, m: &str, esc: Option<&str>) -> AssignmentRow {
    AssignmentRow {
        org: None,
        tool_type: tt.into(),
        default_model: m.into(),
        escalation_model: esc.map(Into::into),
    }
}

fn alias(src: &str, dst: &str) -> AliasRow {
    AliasRow {
        org: None,
        source_model: src.into(),
        target_model: dst.into(),
        target_provider: None,
    }
}

fn world() -> OrgRouting {
    OrgRouting::from_parts(
        &models(),
        vec![
            alias("gpt-3.5-turbo", "llama3"),
            alias("gpt-4", "claude-sonnet"),
        ],
        vec![
            assign("general", "llama3", Some("claude-sonnet")),
            assign("code-gen", "mistral-large", Some("gpt-4")),
            assign("vision-task", "llama3", None),
        ],
        RoutingPolicy::default(),
        vec![],
    )
    .unwrap()
}

fn plain() -> Value {
    json!({"messages": [{"role": "user", "content": "hello"}]})
}

fn req<'a>(body: &'a Value, model: Option<&'a str>) -> RoutingRequest<'a> {
    RoutingRequest {
        org_id: 0,
        body,
        model,
        tool_type_hint: None,
        escalate_hint: None,
        pii_detected: false,
        use_heuristics: true,
    }
}

#[rstest]
#[case("gpt-3.5-turbo", "llama3", Some(json!({"cause": "alias", "from": "gpt-3.5-turbo", "to": "llama3"})))]
#[case("gpt-4", "claude-sonnet", Some(json!({"cause": "alias", "from": "gpt-4", "to": "claude-sonnet"})))]
#[case("unknown-model", "llama3", None)]
fn alias_resolves_and_sets_routed_from(
    #[case] input: &str,
    #[case] model: &str,
    #[case] routed_from: Option<Value>,
) {
    let body = plain();
    let d = decide(&req(&body, Some(input)), &world()).unwrap();
    assert_eq!(d.model, model);
    assert_eq!(d.routed_from, routed_from);
}

#[rstest]
#[case(Some("code-gen"), None, "mistral-large")]
#[case(None, Some("waddleai/code-gen"), "mistral-large")]
#[case(Some("vision-task"), None, "llama3")]
#[case(None, None, "llama3")]
fn stage0_tool_type_cascade_uses_header_hint(
    #[case] hint: Option<&str>,
    #[case] model: Option<&str>,
    #[case] expected: &str,
) {
    let body = plain();
    let mut r = req(&body, model);
    r.tool_type_hint = hint;
    assert_eq!(decide(&r, &world()).unwrap().model, expected);
}

#[rstest]
#[case(Some("auto:high"), "claude-sonnet", true)]
#[case(Some("true"), "claude-sonnet", true)]
#[case(Some("auto:low"), "llama3", false)]
#[case(Some("bogus"), "llama3", false)]
#[case(None, "llama3", false)]
fn escalate_hint_and_auto_high_low_suffix(
    #[case] hint: Option<&str>,
    #[case] expected: &str,
    #[case] escalated: bool,
) {
    let body = plain();
    let mut r = req(&body, None);
    r.escalate_hint = hint;
    let d = decide(&r, &world()).unwrap();
    assert_eq!(d.model, expected);
    let cause = d
        .routed_from
        .as_ref()
        .and_then(|v| v["cause"].as_str().map(str::to_owned));
    assert_eq!(cause.as_deref() == Some("escalation"), escalated);
}

#[test]
fn pii_detected_sensitivity_clamp_excludes_external_candidates() {
    let body = plain();
    let mut r = req(&body, Some("gpt-4"));
    r.pii_detected = true;
    let d = decide(&r, &world()).unwrap();
    // gpt-4 aliases to a commercial model; the clamp removes it, so a local model is chosen.
    assert_eq!(d.model, "llama3");
    assert!(d.executable);
    assert_eq!(d.fallback_chain, ["mistral-large", "llava"]);
    assert!(d.commercial_models.is_empty());
}

#[test]
fn capability_veto_redirects() {
    let body = json!({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "x"}}]}]});
    let mut r = req(&body, None);
    r.tool_type_hint = Some("vision-task");
    let d = decide(&r, &world()).unwrap();
    assert_eq!(d.model, "llava");
    assert_eq!(
        d.routed_from,
        Some(json!({"cause": "capability_veto", "reason": "vision_unsupported"}))
    );
}

#[test]
fn policy_sorted_fallback_chain_excludes_chosen() {
    let body = plain();
    let mut r = req(&body, None);
    r.tool_type_hint = Some("code-gen");
    let d = decide(&r, &world()).unwrap();
    assert_eq!(d.model, "mistral-large");
    // local_first: locals by cost (stable), then commercials by ascending cost; chosen excluded.
    assert_eq!(
        d.fallback_chain,
        ["llama3", "llava", "claude-sonnet", "gpt-4"]
    );
}

/// Source whose data can be swapped and whose load calls are counted.
struct MockSource {
    data: Mutex<OrgRouting>,
    loads: AtomicUsize,
    fail: Mutex<bool>,
}

impl MockSource {
    fn new(w: OrgRouting) -> Arc<Self> {
        Arc::new(Self {
            data: Mutex::new(w),
            loads: AtomicUsize::new(0),
            fail: Mutex::new(false),
        })
    }
}

#[async_trait]
impl RegistrySource for MockSource {
    async fn load(&self, _org: i64) -> Result<OrgRouting, RegistryError> {
        self.loads.fetch_add(1, Ordering::SeqCst);
        if *self.fail.lock().unwrap() {
            return Err(RegistryError::Source("down".into()));
        }
        Ok(self.data.lock().unwrap().clone())
    }
}

fn ctx(model: Option<&str>) -> PipelineContext {
    let mut c = PipelineContext::new(plain());
    c.model = model.map(Into::into);
    c
}

#[tokio::test]
async fn cold_cache_lazy_loads_never_empty_fail_closed() {
    let src = MockSource::new(world());
    let cache = Arc::new(RegistryCache::new(src.clone()));
    let stage = RoutingStage::new(Arc::clone(&cache));
    assert_eq!(cache.cached_scopes(), 0);
    let mut c = ctx(Some("gpt-3.5-turbo"));
    stage.call(&mut c).await.unwrap();
    assert_eq!(c.model.as_deref(), Some("llama3"));
    assert!(!c.fallback_chain.is_empty());
    assert_eq!(c.requested_model.as_deref(), Some("gpt-3.5-turbo"));
    assert_eq!(src.loads.load(Ordering::SeqCst), 1);
    stage.call(&mut ctx(None)).await.unwrap();
    assert_eq!(
        src.loads.load(Ordering::SeqCst),
        1,
        "second request served from L1"
    );

    // Seed failure lazy-loads later; a dead source on a cold cache leaves the model unchanged.
    let dead = MockSource::new(world());
    *dead.fail.lock().unwrap() = true;
    let cache = Arc::new(RegistryCache::new(dead.clone()));
    let report = cache.seed(&[7]).await;
    assert_eq!((report.loaded, report.failed), (0, 2));
    let stage = RoutingStage::new(Arc::clone(&cache));
    let mut c = ctx(Some("gpt-4"));
    stage.call(&mut c).await.unwrap();
    assert_eq!(c.model.as_deref(), Some("gpt-4"));
    assert!(c.fallback_chain.is_empty());
    *dead.fail.lock().unwrap() = false;
    stage.call(&mut c).await.unwrap();
    assert_eq!(c.model.as_deref(), Some("claude-sonnet"));
}

#[tokio::test]
async fn invalidation_refreshes() {
    let src = MockSource::new(world());
    let cache = Arc::new(RegistryCache::new(src.clone()));
    let stage = RoutingStage::new(Arc::clone(&cache));
    let mut c = ctx(None);
    stage.call(&mut c).await.unwrap();
    assert_eq!(c.model.as_deref(), Some("llama3"));

    src.data.lock().unwrap().assignments = vec![assign("general", "mistral-large", None)];
    cache.on_invalidation("waddleai:cf:rules:global").await;
    assert_eq!(
        src.loads.load(Ordering::SeqCst),
        1,
        "unrelated key must not reload"
    );
    let mut c = ctx(None);
    stage.call(&mut c).await.unwrap();
    assert_eq!(
        c.model.as_deref(),
        Some("llama3"),
        "still the cached snapshot"
    );

    cache
        .on_invalidation("waddleai:route:assign:global:general")
        .await;
    assert_eq!(src.loads.load(Ordering::SeqCst), 2);
    let mut c = ctx(None);
    stage.call(&mut c).await.unwrap();
    assert_eq!(c.model.as_deref(), Some("mistral-large"));

    src.data.lock().unwrap().policy.mode = "cost".into();
    cache.on_invalidation("waddleai:route:policy:0").await;
    assert_eq!(src.loads.load(Ordering::SeqCst), 3);
}

#[tokio::test]
async fn explicit_provider_prefix_ollama_llama2_routes() {
    let src = MockSource::new(world());
    let stage = RoutingStage::new(Arc::new(RegistryCache::new(src)));
    let mut c = ctx(Some("ollama:llama2"));
    stage.call(&mut c).await.unwrap();
    // Python ignores the unaliased bare model and routes via the tool-type assignment. The pin
    // is advisory (RoutingMeta); ctx.provider is a post-dispatch result and stays untouched.
    assert_eq!(c.model.as_deref(), Some("llama3"));
    assert_eq!(c.provider, None);
    let meta = c.extensions.get::<RoutingMeta>().unwrap();
    assert_eq!(meta.provider_hint.as_deref(), Some("ollama"));
    assert_eq!(c.requested_model.as_deref(), Some("ollama:llama2"));
}

#[tokio::test]
async fn local_only_filter_p2() {
    let src = MockSource::new(world());
    let stage = RoutingStage::new(Arc::new(RegistryCache::new(src)));
    let mut c = ctx(Some("gpt-4"));
    stage.call(&mut c).await.unwrap();
    // Commercial pick stays in the decision (parity) but is marked non-executable.
    assert_eq!(c.model.as_deref(), Some("claude-sonnet"));
    assert!(c.fallback_chain.contains(&"gpt-4".to_string()));
    let meta = c.extensions.get::<RoutingMeta>().unwrap();
    assert!(!meta.executable);
    assert_eq!(meta.commercial_models, ["claude-sonnet", "gpt-4"]);
    assert_eq!(
        meta.executable_fallbacks,
        ["llama3", "mistral-large", "llava"]
    );

    let mut c = ctx(Some("gpt-3.5-turbo"));
    stage.call(&mut c).await.unwrap();
    assert!(c.extensions.get::<RoutingMeta>().unwrap().executable);
}

fn world_from_json(w: &Value) -> OrgRouting {
    let raw: Vec<RawModelConfig> = w["models"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| RawModelConfig {
            model_name: m["model_name"].as_str().unwrap().into(),
            preferred_providers: m["preferred_providers"].clone(),
            cost_per_token: m["cost_per_token"].clone(),
            context_length: m["context_length"].as_i64().unwrap(),
            capabilities: m["capabilities"].clone(),
            enabled: m["enabled"].as_bool().unwrap(),
        })
        .collect();
    let aliases = w["aliases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|a| a["enabled"].as_bool().unwrap())
        .map(|a| AliasRow {
            org: a["org"].as_i64(),
            source_model: a["source"].as_str().unwrap().into(),
            target_model: a["target"].as_str().unwrap().into(),
            target_provider: a["provider"].as_str().map(Into::into),
        })
        .collect();
    let assignments = w["assignments"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|a| a.get("enabled").and_then(Value::as_bool).unwrap_or(true))
        .map(|a| AssignmentRow {
            org: if a["scope"] == "org" {
                a["ref"].as_i64()
            } else {
                None
            },
            tool_type: a["tool_type"].as_str().unwrap().into(),
            default_model: a["model"].as_str().unwrap().into(),
            escalation_model: a["esc"].as_str().map(Into::into),
        })
        .collect();
    let mut policy = RoutingPolicy::default();
    if let Some(p) = w["policy"].as_object() {
        if let Some(v) = p.get("mode").and_then(Value::as_str) {
            policy.mode = v.into();
        }
        if let Some(v) = p.get("sensitivity_routing").and_then(Value::as_str) {
            policy.sensitivity_routing = v.into();
        }
        if let Some(v) = p.get("escalation_target").and_then(Value::as_str) {
            policy.escalation_target = Some(v.into());
        }
    }
    let rules = w["rules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| HeuristicRule {
            priority: r["priority"].as_i64().unwrap(),
            match_: r["match"].clone(),
            action: r["action"].clone(),
        })
        .collect();
    OrgRouting::from_parts(&raw, aliases, assignments, policy, rules).unwrap()
}

#[test]
fn golden_decisions_equal_python() {
    let golden: Value =
        serde_json::from_str(include_str!("fixtures/python_golden.json")).expect("golden parses");
    let worlds = golden["worlds"].as_object().unwrap();
    let bodies = &golden["bodies"];
    let snapshots: std::collections::HashMap<&str, OrgRouting> = worlds
        .iter()
        .map(|(k, w)| (k.as_str(), world_from_json(w)))
        .collect();
    let rows = golden["rows"].as_array().unwrap();
    let mut examined = 0usize;
    let mut mismatches = Vec::new();
    let (mut divergent_errored, mut divergent_violation) = (0usize, 0usize);
    for row in rows {
        let r = &row["request"];
        let model = r["model"].as_str();
        let body = &bodies[r["body"].as_str().unwrap()];
        let request = RoutingRequest {
            org_id: r["org_id"].as_i64().unwrap(),
            body,
            model,
            tool_type_hint: r["hint"].as_str(),
            escalate_hint: r["escalate"].as_str(),
            pii_detected: r["pii"].as_bool().unwrap(),
            use_heuristics: true,
        };
        let world = &worlds[row["world"].as_str().unwrap()];
        let result = decide(&request, &snapshots[row["world"].as_str().unwrap()]);
        // Data-residency invariant (R20), checked on EVERY row, whatever Python did: a request
        // that must stay local never lands on a non-local final model.
        let policy = world["policy"].as_object();
        let sens = policy
            .and_then(|p| p.get("sensitivity_routing"))
            .and_then(Value::as_str)
            .unwrap_or("local_only");
        let must_local = (request.pii_detected && !matches!(sens, "ignore" | "redact_then_any"))
            || policy.and_then(|p| p.get("mode")).and_then(Value::as_str) == Some("local_only");
        if let Ok(d) = &result {
            let local = world["models"].as_array().unwrap().iter().any(|m| {
                m["model_name"] == d.model.as_str()
                    && m["enabled"] == true
                    && m["preferred_providers"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|p| p == "ollama" || p == "llamacpp")
            });
            assert!(
                !must_local || local,
                "residency escape: {} {r} -> {}",
                row["world"],
                d.model
            );
        }
        examined += 1;
        if row.get("python_errored").is_some() {
            // Python fails open on these client inputs; Rust must route normally instead.
            assert!(result.is_ok(), "client input must not error: {r}");
            divergent_errored += 1;
            continue;
        }
        if row.get("python_violation").is_some() {
            // Python lets escalation/veto escape the clamp; Rust stays local or fails closed.
            divergent_violation += 1;
            continue;
        }
        let got = match result {
            Ok(d) => (Some(d.model), d.fallback_chain, d.routed_from),
            Err(e) => {
                mismatches.push(format!("{} {r}: unexpected {e}", row["world"]));
                continue;
            }
        };
        let e = &row["expected"];
        let want = (
            e["model"].as_str().map(String::from),
            e["fallback_chain"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
                .collect::<Vec<_>>(),
            if e["routed_from"].is_null() {
                None
            } else {
                Some(e["routed_from"].clone())
            },
        );
        if got != want {
            mismatches.push(format!("{} {r}: got {got:?} want {want:?}", row["world"]));
        }
    }
    println!(
        "golden rows examined: {examined} (equal to Python: {}, intentional divergences: {divergent_violation} residency, {divergent_errored} client-input)",
        examined - divergent_violation - divergent_errored
    );
    assert!(examined > 0, "0 golden rows examined = FAIL");
    assert!(
        mismatches.is_empty(),
        "{} mismatches, first: {:?}",
        mismatches.len(),
        &mismatches[..mismatches.len().min(3)]
    );
}

#[test]
fn offers_derivation_matches_load_offers() {
    let offers = build_offers(&models()).unwrap();
    assert_eq!(offers.len(), 5);
    assert!(offers[2].supports_vision && !offers[0].supports_vision);
    assert!((offers[4].cost_per_token - 0.0000035).abs() < 1e-12);
}

#[tokio::test]
#[ignore = "needs a migrated Postgres (WADDLEAI_TEST_DATABASE_URL)"]
async fn db_source_loads_registry() {
    let url = std::env::var("WADDLEAI_TEST_DATABASE_URL").expect("WADDLEAI_TEST_DATABASE_URL");
    let conn = db::connect_with(&url, &db::DbOptions::from_env().expect("opts"))
        .await
        .expect("connect");
    let src = routing::DbRegistrySource::new(conn);
    let snap = src.load(0).await.expect("db load");
    assert!(
        !snap.offers.is_empty(),
        "seeded model_configs must yield offers"
    );
}

#[rstest]
#[case(json!({"messages": [], "max_tokens": "50"}), Some(50))]
#[case(json!({"messages": [], "max_tokens": 9000.9}), Some(9000))]
#[case(json!({"messages": [], "max_tokens": true}), Some(1))]
#[case(json!({"messages": [], "max_tokens": null}), Some(0))]
#[case(json!({"messages": [], "max_tokens": "abc"}), Some(0))]
#[case(json!({"messages": [], "max_tokens": [1]}), Some(0))]
fn max_tokens_coercion_matches_python_int(#[case] body: Value, #[case] want: Option<i64>) {
    // Python raises on the last two (fail-open); Rust counts them as 0 so no client input errors.
    assert_eq!(Some(routing::derive_requirements(&body).min_context), want);
}

#[rstest]
#[case(json!({"tools": [{"a": 1}]}), true)]
#[case(json!({"tools": []}), false)]
#[case(json!({"tool_choice": "none"}), false)]
#[case(json!({"tool_choice": "auto"}), true)]
#[case(json!({"tool_choice": ""}), true)]
#[case(json!({}), false)]
fn needs_tools_matches_python(#[case] body: Value, #[case] want: bool) {
    assert_eq!(routing::derive_requirements(&body).needs_tools, want);
}

#[test]
fn special_token_counts_as_plain_text() {
    let body = json!({"messages": [{"role": "user", "content": "a <|endoftext|> b"}]});
    assert!(routing::derive_requirements(&body).min_context > 0);
}

#[test]
fn heuristic_rules_fire_in_priority_order_and_unknown_keys_are_ignored() {
    let rules = vec![
        HeuristicRule {
            priority: 20,
            match_: json!({"model_prefix": "gpt"}),
            action: json!({"tool_type": "general"}),
        },
        HeuristicRule {
            priority: 10,
            match_: json!({"model_prefix": "gpt", "future_key": 1}),
            action: json!({"tool_type": "code-gen"}),
        },
        HeuristicRule {
            priority: 1,
            match_: json!({}),
            action: json!({"tool_type": "ghost"}),
        },
        HeuristicRule {
            priority: 2,
            match_: json!({"tool_name_present": ["x"]}),
            action: json!({"tool_type": "ghost"}),
        },
    ];
    let sig = routing::RequestSignals {
        model: "gpt-9".into(),
    };
    let d = routing::determine_tool_type(None, &sig, &rules);
    assert_eq!(
        (d.tool_type.as_str(), d.source),
        ("code-gen", routing::ToolTypeSource::Heuristic)
    );
    let d = routing::determine_tool_type(Some("vision-task"), &sig, &rules);
    assert_eq!(
        (d.tool_type.as_str(), d.source),
        ("vision-task", routing::ToolTypeSource::Explicit)
    );
    let d = routing::determine_tool_type(Some(""), &sig, &[]);
    assert_eq!(d.tool_type, routing::DEFAULT_TOOL_TYPE);
    assert_eq!(d.source, routing::ToolTypeSource::DefaultNoClassifier);
}

#[test]
fn heuristics_are_off_by_default_in_the_stage() {
    let mut w = world();
    w.rules = vec![HeuristicRule {
        priority: 1,
        match_: json!({"model_prefix": "zzz"}),
        action: json!({"tool_type": "code-gen"}),
    }];
    let body = plain();
    let mut r = req(&body, Some("zzz-model"));
    assert_eq!(decide(&r, &w).unwrap().model, "mistral-large");
    r.use_heuristics = false;
    assert_eq!(decide(&r, &w).unwrap().model, "llama3");
}

#[rstest]
#[case("ollama:llama2", Some("ollama"), "llama2")]
#[case("gemma4:e4b", None, "gemma4:e4b")]
#[case("openai:gpt-4", Some("openai"), "gpt-4")]
#[case("plain", None, "plain")]
fn provider_prefix_splits_on_first_colon_only_for_known_providers(
    #[case] input: &str,
    #[case] provider: Option<&str>,
    #[case] bare: &str,
) {
    assert_eq!(routing::split_provider_prefix(input), (provider, bare));
}

#[rstest]
#[case(Some("code-gen"), Some("waddleai/x"), Some("code-gen"))]
#[case(None, Some("waddleai/vision-task"), Some("vision-task"))]
#[case(None, Some("gpt-4"), None)]
#[case(Some(""), None, None)]
fn explicit_tool_type_priority(
    #[case] header: Option<&str>,
    #[case] model: Option<&str>,
    #[case] want: Option<&str>,
) {
    assert_eq!(routing::explicit_tool_type(header, model).as_deref(), want);
}

#[test]
fn escalation_target_prefers_assignment_over_policy() {
    assert_eq!(
        routing::escalation_target(Some("a"), Some("b")).as_deref(),
        Some("a")
    );
    assert_eq!(
        routing::escalation_target(Some(""), Some("b")).as_deref(),
        Some("b")
    );
    assert_eq!(routing::escalation_target(None, None), None);
    assert!(routing::should_escalate(Some(4), 3, None).escalate);
    assert!(!routing::should_escalate(Some(2), 3, None).escalate);
}

#[rstest]
#[case("local_only", &["llama3", "mistral-large", "llava"])]
#[case("commercial_only", &["claude-sonnet", "gpt-4"])]
#[case("cost", &["llama3", "mistral-large", "llava", "claude-sonnet", "gpt-4"])]
#[case("latency", &["llama3", "mistral-large", "llava", "gpt-4", "claude-sonnet"])]
#[case("weird", &["llama3", "mistral-large", "llava", "claude-sonnet", "gpt-4"])]
fn policy_modes_filter_and_sort(#[case] mode: &str, #[case] want: &[&str]) {
    let policy = RoutingPolicy {
        mode: mode.into(),
        ..RoutingPolicy::default()
    };
    let got: Vec<String> = routing::filter_and_sort(&build_offers(&models()).unwrap(), &policy)
        .into_iter()
        .map(|o| o.model_name)
        .collect();
    assert_eq!(got, want);
}

#[rstest]
#[case("local_only", 3)]
#[case("redact_then_any", 5)]
#[case("ignore", 5)]
#[case("weird", 3)]
fn sensitivity_modes(#[case] mode: &str, #[case] kept: usize) {
    let chain = build_offers(&models()).unwrap();
    assert_eq!(routing::apply_sensitivity(chain, true, mode).len(), kept);
}

#[tokio::test]
async fn global_assignment_invalidation_refreshes_every_cached_scope() {
    let src = MockSource::new(world());
    let cache = Arc::new(RegistryCache::new(src.clone()));
    let report = cache.seed(&[7, 9]).await;
    assert_eq!((report.loaded, report.failed), (3, 0));
    assert_eq!(cache.cached_scopes(), 3);
    cache
        .on_invalidation("waddleai:route:assign:global:general")
        .await;
    assert_eq!(src.loads.load(Ordering::SeqCst), 6);
    cache
        .on_invalidation("waddleai:route:assign:7:general")
        .await;
    assert_eq!(src.loads.load(Ordering::SeqCst), 7);
    cache
        .on_invalidation("waddleai:route:assign:555:general")
        .await;
    assert_eq!(
        src.loads.load(Ordering::SeqCst),
        7,
        "uncached org is not refreshed"
    );
}

#[tokio::test]
async fn stage_identity_flag_and_client_input_never_errors() {
    let src = MockSource::new(world());
    let stage = RoutingStage::new(Arc::new(RegistryCache::new(src)));
    assert_eq!(stage.name(), "routing");
    assert_eq!(stage.flag(), Some("waddleai.smart_routing"));
    let mut c = PipelineContext::new(json!({
        "messages": [{"role": "user", "content": "<|endoftext|>"}], "max_tokens": "abc"}));
    c.model = Some("gpt-3.5-turbo".into());
    stage.call(&mut c).await.unwrap();
    assert!(!c.blocked);
    assert_eq!(c.model.as_deref(), Some("llama3"));
}

fn is_external(m: &str) -> bool {
    ["gpt-4", "claude-sonnet"].contains(&m)
}

#[rstest]
#[case("true", "general")]
#[case("auto:high", "code-gen")]
fn pii_with_escalate_hint_stays_local(#[case] hint: &str, #[case] tool: &str) {
    // Both assignments escalate to a commercial model; PII must keep the request local.
    let body = plain();
    let mut r = req(&body, None);
    r.pii_detected = true;
    r.escalate_hint = Some(hint);
    r.tool_type_hint = Some(tool);
    let d = decide(&r, &world()).unwrap();
    assert!(!is_external(&d.model), "escaped to {}", d.model);
    assert!(d.executable);
    assert!(d.fallback_chain.iter().all(|m| !is_external(m)));
}

#[test]
fn pii_with_capability_veto_stays_local() {
    // The assigned local model lacks vision; an external vision model comes first in the
    // registry, so an unclamped veto would pick it.
    let mut m = models();
    m.insert(
        0,
        model(
            "cloud-vision",
            json!(["openai"]),
            json!({"openai": 0.00001}),
            128_000,
            json!(["vision"]),
        ),
    );
    let w = OrgRouting::from_parts(
        &m,
        vec![],
        vec![assign("vision-task", "llama3", None)],
        RoutingPolicy::default(),
        vec![],
    )
    .unwrap();
    let body = json!({"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "x"}}]}]});
    let mut r = req(&body, None);
    r.tool_type_hint = Some("vision-task");
    assert_eq!(decide(&r, &w).unwrap().model, "cloud-vision");
    r.pii_detected = true;
    assert_eq!(decide(&r, &w).unwrap().model, "llava");
}

#[test]
fn local_only_policy_blocks_escalation_to_external() {
    let mut w = world();
    w.policy.mode = "local_only".into();
    let body = plain();
    let mut r = req(&body, None);
    r.escalate_hint = Some("true");
    let d = decide(&r, &w).unwrap();
    assert_eq!(d.model, "llama3");
    assert!(d.executable);
}

#[tokio::test]
async fn clamp_with_no_local_model_fails_closed_and_blocks() {
    let ext: Vec<_> = models()
        .into_iter()
        .filter(|m| is_external(&m.model_name))
        .collect();
    let w = OrgRouting::from_parts(
        &ext,
        vec![],
        vec![assign("general", "gpt-4", Some("claude-sonnet"))],
        RoutingPolicy::default(),
        vec![],
    )
    .unwrap();
    let body = plain();
    let mut r = req(&body, None);
    r.pii_detected = true;
    assert_eq!(
        decide(&r, &w).unwrap_err(),
        routing::RoutingError::NoLocalCandidate
    );
    let stage = RoutingStage::new(Arc::new(RegistryCache::new(MockSource::new(w))));
    let mut c = PipelineContext::new(plain());
    c.pii_detected = true;
    c.model = Some("gpt-4".into());
    stage.call(&mut c).await.unwrap();
    assert!(c.blocked && c.status_code == 503);
    assert_eq!(
        c.model.as_deref(),
        Some("gpt-4"),
        "model not rewritten to an external pick"
    );
}

#[tokio::test]
async fn registry_down_with_pii_blocks_without_pii_passes_through() {
    let dead = MockSource::new(world());
    *dead.fail.lock().unwrap() = true;
    let stage = RoutingStage::new(Arc::new(RegistryCache::new(dead)));
    let mut c = ctx(Some("gpt-4"));
    c.pii_detected = true;
    stage.call(&mut c).await.unwrap();
    assert!(c.blocked && c.status_code == 503);
    let mut c = ctx(Some("gpt-4"));
    stage.call(&mut c).await.unwrap();
    assert!(!c.blocked);
}

#[test]
fn unknown_escalation_target_is_non_executable() {
    let mut w = world();
    w.assignments = vec![assign("general", "llama3", Some("not-in-registry"))];
    let body = plain();
    let mut r = req(&body, None);
    r.escalate_hint = Some("true");
    let d = decide(&r, &w).unwrap();
    assert_eq!(d.model, "not-in-registry");
    assert!(!d.executable);
    assert!(d.commercial_models.contains(&"not-in-registry".to_string()));
}

#[tokio::test]
async fn unclassified_default_is_flagged_visibly() {
    let stage = RoutingStage::new(Arc::new(RegistryCache::new(MockSource::new(world()))));
    let mut c = ctx(None);
    stage.call(&mut c).await.unwrap();
    assert!(
        c.extensions
            .get::<RoutingMeta>()
            .unwrap()
            .unclassified_default
    );
    let mut c = ctx(None);
    c.explicit_tool_type_hint = Some("general".into());
    stage.call(&mut c).await.unwrap();
    assert!(
        !c.extensions
            .get::<RoutingMeta>()
            .unwrap()
            .unclassified_default
    );
}

#[test]
fn non_numeric_cost_fails_loudly() {
    let mut m = models();
    m[0].cost_per_token = json!({"ollama": "free"});
    assert_eq!(
        build_offers(&m),
        Err(routing::RoutingError::BadCost("llama3".into()))
    );
}

#[test]
fn cost_mean_uses_python_compensated_sum() {
    // Naive 0.1 + 0.2 + 0.3 = 0.6000000000000001; Python's sum() gives exactly 0.6.
    let m = vec![model(
        "tri",
        json!(["ollama"]),
        json!({"ccc": 0.3, "a": 0.1, "bb": 0.2}),
        8192,
        json!([]),
    )];
    let offers = build_offers(&m).unwrap();
    assert_eq!(offers[0].cost_per_token, 0.6_f64 / 3.0);
    assert_ne!(offers[0].cost_per_token, (0.1_f64 + 0.2 + 0.3) / 3.0);
}
