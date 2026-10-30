//! Tier-3 NER seam glue: gating (flag AND license), content hashing, span-to-violation mapping.
//!
//! Real NER lives behind [`pipeline::NerClient`] (P1/P3); P2 ships `NoNer`, whose
//! `Unavailable` error drives the degraded fail-open/closed policy.

use std::sync::Arc;

use async_trait::async_trait;
use pipeline::{EntityKind, EntitySpan, FlagResolver, NerClient, NerError, NerRequest};
use sha2::{Digest, Sha256};

/// PostHog flag gating the NER tier (same key as the Python filter).
pub const NER_TIER_FLAG: &str = "waddleai.pii_ner";
/// License feature gating the NER tier (same key as the Python filter).
pub const NER_TIER_LICENSE_FEATURE: &str = "pii_ner_detection";
/// Composite-confidence floor below which an NER hit is noise (`_MIN_NER_CONFIDENCE`).
pub const MIN_NER_CONFIDENCE: f64 = 0.3;

/// Every kind the filter asks the NER backend to look for.
const ALL_KINDS: &[EntityKind] = &[
    EntityKind::Person,
    EntityKind::Email,
    EntityKind::Phone,
    EntityKind::Location,
    EntityKind::Organization,
    EntityKind::Other,
];

/// License entitlement seam (`has_feature`), per org.
#[async_trait]
pub trait LicenseGate: Send + Sync {
    /// True only when `feature` is positively entitled for the org.
    async fn has_feature(&self, feature: &str, org_id: Option<i64>) -> bool;
}

/// Gate that never entitles anything (the safe default).
pub struct DenyLicense;

#[async_trait]
impl LicenseGate for DenyLicense {
    async fn has_feature(&self, _feature: &str, _org_id: Option<i64>) -> bool {
        false
    }
}

/// Adapter over the process-wide P0 license client; `scope_for` maps the numeric org id to a
/// licensing [`flags::Scope`] (public tenant id).
pub struct FlagsLicenseGate {
    scope_for: Box<dyn Fn(Option<i64>) -> flags::Scope + Send + Sync>,
}

impl FlagsLicenseGate {
    /// Gate resolving scopes with `scope_for`.
    pub fn new(scope_for: impl Fn(Option<i64>) -> flags::Scope + Send + Sync + 'static) -> Self {
        Self {
            scope_for: Box::new(scope_for),
        }
    }
}

#[async_trait]
impl LicenseGate for FlagsLicenseGate {
    async fn has_feature(&self, feature: &str, org_id: Option<i64>) -> bool {
        flags::has_feature(feature, &(self.scope_for)(org_id)).await
    }
}

/// SHA-256 of the text: the only identity of the content that may be used in cache keys.
pub fn content_hash(text: &str) -> [u8; 32] {
    Sha256::digest(text.as_bytes()).into()
}

/// A tier-3 detection mapped to the filter's violation vocabulary.
pub(crate) struct NerHit {
    pub(crate) rule_name: String,
    pub(crate) redact: bool,
    pub(crate) confidence: f64,
    pub(crate) span: (usize, usize),
}

/// Maps an entity kind to `(python entity type, redact?, weight)` per `ENTITY_CONFIG`.
fn config(kind: EntityKind) -> (&'static str, bool, f64) {
    match kind {
        EntityKind::Person => ("PERSON", true, 0.82),
        EntityKind::Email => ("EMAIL_ADDRESS", true, 0.95),
        EntityKind::Phone => ("PHONE_NUMBER", true, 0.87),
        EntityKind::Location => ("LOCATION", false, 0.65),
        EntityKind::Organization => ("ORG", false, 0.50),
        EntityKind::Other => ("OTHER", false, 0.60),
    }
}

/// Tier-3 runner: gate checks plus the NER call and span mapping.
pub struct NerTier {
    client: Arc<dyn NerClient>,
    flags: Arc<dyn FlagResolver>,
    license: Arc<dyn LicenseGate>,
}

impl NerTier {
    /// Tier over a NER backend and its two gates.
    pub fn new(
        client: Arc<dyn NerClient>,
        flags: Arc<dyn FlagResolver>,
        license: Arc<dyn LicenseGate>,
    ) -> Self {
        Self {
            client,
            flags,
            license,
        }
    }

    /// True only when the PostHog flag AND the license entitlement are both ON.
    pub async fn enabled(&self, org_id: Option<i64>) -> bool {
        let distinct = org_id.map(|o| o.to_string());
        if !self.flags.enabled(NER_TIER_FLAG, distinct.as_deref()).await {
            return false;
        }
        self.license
            .has_feature(NER_TIER_LICENSE_FEATURE, org_id)
            .await
    }

    /// Calls the backend and maps spans to hits; spans off a char boundary are dropped.
    pub(crate) async fn detect(
        &self,
        text: &str,
        org_id: Option<i64>,
    ) -> Result<Vec<NerHit>, NerError> {
        let req = NerRequest {
            text,
            entities: ALL_KINDS,
            org_id: org_id.unwrap_or(0),
            content_hash: content_hash(text),
        };
        let spans: Vec<EntitySpan> = self.client.analyze(&req).await?;
        let mut hits = Vec::new();
        for span in spans {
            if text.get(span.start..span.end).is_none_or(str::is_empty) {
                tracing::warn!("ner span outside text or off a char boundary; dropped");
                continue;
            }
            let (name, redact, weight) = config(span.kind);
            let confidence = (f64::from(span.score) * weight).min(1.0);
            if confidence < MIN_NER_CONFIDENCE {
                continue;
            }
            hits.push(NerHit {
                rule_name: format!("ner:{name}"),
                redact,
                confidence,
                span: (span.start, span.end),
            });
        }
        Ok(hits)
    }
}
