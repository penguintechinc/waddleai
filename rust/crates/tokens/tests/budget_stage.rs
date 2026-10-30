//! `TokenBudgetStage` behaviour against a fake `BudgetGate` (no Valkey needed).

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use pipeline::{PipelineContext, Stage};
use serde_json::json;
use tokens::{BudgetGate, GateDecision, KeyLimits, TokenBudgetStage, VkeyBudget};

/// Records reserve calls and returns a canned decision.
struct FakeGate {
    decision: GateDecision,
    calls: Mutex<Vec<(i64, u64)>>,
}

impl FakeGate {
    fn new(decision: GateDecision) -> Arc<Self> {
        Arc::new(Self {
            decision,
            calls: Mutex::new(Vec::new()),
        })
    }
}

#[async_trait]
impl BudgetGate for FakeGate {
    async fn reserve(
        &self,
        vkey_id: i64,
        estimated_tokens: u64,
        _estimated_usd: f64,
        _limits: &KeyLimits,
    ) -> GateDecision {
        self.calls.lock().unwrap().push((vkey_id, estimated_tokens));
        self.decision.clone()
    }
    async fn reconcile(&self, _id: &str, _tokens: u64, _usd: f64) {}
}

fn limits() -> KeyLimits {
    KeyLimits {
        tpm_limit: Some(1000),
        monthly_token_limit: None,
        monthly_usd_limit: None,
    }
}

fn ctx_with(budget: Option<VkeyBudget>) -> PipelineContext {
    let mut ctx = PipelineContext {
        messages: vec![json!({"role": "user", "content": "hello there general kenobi"})],
        ..PipelineContext::default()
    };
    if let Some(b) = budget {
        ctx.extensions.insert(b);
    }
    ctx
}

#[tokio::test]
async fn skips_without_vkey() {
    let gate = FakeGate::new(GateDecision::allow(Some("r".into())));
    let stage = TokenBudgetStage::new(gate.clone());
    let mut ctx = ctx_with(None);
    stage.call(&mut ctx).await.unwrap();
    assert!(gate.calls.lock().unwrap().is_empty());
    assert!(!ctx.blocked);
    assert_eq!(ctx.reservation_id, None);
}

#[tokio::test]
async fn skips_without_limits() {
    let gate = FakeGate::new(GateDecision::allow(Some("r".into())));
    let stage = TokenBudgetStage::new(gate.clone());
    let mut ctx = ctx_with(Some(VkeyBudget {
        vkey_id: 7,
        limits: None,
    }));
    stage.call(&mut ctx).await.unwrap();
    assert!(gate.calls.lock().unwrap().is_empty());
    assert!(!ctx.blocked);
    assert_eq!(ctx.reservation_id, None);
}

#[tokio::test]
async fn blocks_429_with_decision_reason() {
    let gate = FakeGate::new(GateDecision::deny("tpm_exceeded"));
    let stage = TokenBudgetStage::new(gate);
    let mut ctx = ctx_with(Some(VkeyBudget {
        vkey_id: 7,
        limits: Some(limits()),
    }));
    stage.call(&mut ctx).await.unwrap();
    assert!(ctx.blocked);
    assert_eq!(ctx.status_code, 429);
    assert_eq!(ctx.block_reason.as_deref(), Some("tpm_exceeded"));
    assert_eq!(ctx.reservation_id, None);
}

#[tokio::test]
async fn stashes_reservation_id_on_allow() {
    let gate = FakeGate::new(GateDecision::allow(Some("resv-123".into())));
    let stage = TokenBudgetStage::new(gate.clone());
    let mut ctx = ctx_with(Some(VkeyBudget {
        vkey_id: 7,
        limits: Some(limits()),
    }));
    stage.call(&mut ctx).await.unwrap();
    assert!(!ctx.blocked);
    assert_eq!(ctx.status_code, 200);
    assert_eq!(ctx.reservation_id.as_deref(), Some("resv-123"));
    // "hello there general kenobi" = 26 chars -> 6 in, 12 out -> 18 total reserved.
    assert_eq!(*gate.calls.lock().unwrap(), vec![(7, 18)]);
}

#[test]
fn stage_name_is_token_budget() {
    let stage = TokenBudgetStage::new(FakeGate::new(GateDecision::allow(None)));
    assert_eq!(stage.name(), "token_budget");
    assert_eq!(stage.flag(), None);
}
