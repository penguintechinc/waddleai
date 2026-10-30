//! `AuthStage`: authenticates the request, selects virtual vs passthrough credential mode,
//! enforces tenant presence, and publishes the key's budget limits for the budget stages.
//!
//! Port of Python `AuthStage` (block reasons verbatim) extended with spec 5.1.1 auth modes.

use std::sync::Arc;

use async_trait::async_trait;
use auth::{AuthError, UserContext};
use http::HeaderMap;
use secrecy::ExposeSecret;

use crate::budget::{KeyLimits, VkeyBudget};
use crate::context::{AuthMode, PipelineContext};
use crate::stage::{Stage, StageError};
use crate::stages::auth_mode::{detect_auth_mode, CredentialKind};

/// Inbound request headers, placed in `ctx.extensions` by the HTTP layer before the pipeline runs.
#[derive(Debug, Clone, Default)]
pub struct RequestHeaders(pub HeaderMap);

/// Resolves a `wa-` key to a [`UserContext`]; production wiring wraps P0 `authenticate_api_key`.
#[async_trait]
pub trait Authenticator: Send + Sync {
    /// Authenticates the raw `wa-` key; every failure must be fail-closed.
    async fn authenticate(&self, wa_key: &str) -> Result<UserContext, AuthError>;
}

/// Per-key/org passthrough opt-in. Implementations read an explicit enable flag.
#[async_trait]
pub trait PassthroughPolicy: Send + Sync {
    /// True only when `key` has explicitly opted in to passthrough.
    async fn enabled(&self, key: &UserContext) -> bool;
}

/// Fail-closed policy: passthrough is never enabled. Used until a per-key enable column exists
/// (none does today -- needs a Python-side Alembic column), so provider keys are rejected 401.
#[derive(Debug, Clone, Copy, Default)]
pub struct DenyPassthrough;

#[async_trait]
impl PassthroughPolicy for DenyPassthrough {
    async fn enabled(&self, _key: &UserContext) -> bool {
        false
    }
}

/// Loads the budget subject (virtual key id + limits) for an authenticated key.
#[async_trait]
pub trait KeyLimitsSource: Send + Sync {
    /// `Some((vkey_id, limits))` when the key has a budget subject, else `None`.
    async fn limits_for(&self, key: &UserContext) -> Option<(i64, Option<KeyLimits>)>;
}

/// Source that never yields limits, so the budget stages skip.
#[derive(Debug, Clone, Copy, Default)]
pub struct NoKeyLimits;

#[async_trait]
impl KeyLimitsSource for NoKeyLimits {
    async fn limits_for(&self, _key: &UserContext) -> Option<(i64, Option<KeyLimits>)> {
        None
    }
}

/// Authenticates, picks the credential mode, checks tenant, and seeds budget limits.
pub struct AuthStage {
    authenticator: Arc<dyn Authenticator>,
    policy: Arc<dyn PassthroughPolicy>,
    limits: Arc<dyn KeyLimitsSource>,
}

impl AuthStage {
    /// Builds the stage from its three seams.
    pub fn new(
        authenticator: Arc<dyn Authenticator>,
        policy: Arc<dyn PassthroughPolicy>,
        limits: Arc<dyn KeyLimitsSource>,
    ) -> Self {
        Self {
            authenticator,
            policy,
            limits,
        }
    }
}

/// Short-circuits the request with a status and verbatim reason.
fn block(ctx: &mut PipelineContext, status: u16, reason: &str) {
    ctx.blocked = true;
    ctx.status_code = status;
    ctx.block_reason = Some(reason.to_owned());
}

#[async_trait]
impl Stage for AuthStage {
    fn name(&self) -> &'static str {
        "auth"
    }

    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        let kind = ctx
            .extensions
            .get::<RequestHeaders>()
            .map(|h| detect_auth_mode(&h.0));
        let mut provider = None;
        match kind {
            Some(CredentialKind::ProviderKey(_)) => {
                tracing::warn!("auth failed: provider key without wa- key");
                block(ctx, 401, "unauthenticated");
                return Ok(());
            }
            Some(CredentialKind::WaddleKey(k)) if ctx.user.is_none() => {
                if !self.authenticate_into(ctx, k.expose_secret()).await? {
                    return Ok(());
                }
            }
            Some(CredentialKind::Passthrough {
                waddle_key,
                provider_key,
            }) => {
                if ctx.user.is_none()
                    && !self
                        .authenticate_into(ctx, waddle_key.expose_secret())
                        .await?
                {
                    return Ok(());
                }
                provider = Some(provider_key);
            }
            _ => {}
        }

        // User must be present, with an organization/tenant.
        let Some(user) = ctx.user.as_ref() else {
            tracing::warn!("auth failed: user is None");
            block(ctx, 401, "unauthenticated");
            return Ok(());
        };
        if user.tenant.is_empty() {
            tracing::warn!(
                user_id = user.user_id,
                "auth failed: no tenant/organization"
            );
            block(ctx, 403, "missing_organization");
            return Ok(());
        }

        if let Some(secret) = provider {
            if !self.policy.enabled(user).await {
                tracing::warn!(org_id = user.org_id, "passthrough not enabled for key");
                block(ctx, 401, "unauthenticated");
                return Ok(());
            }
            ctx.auth_mode = AuthMode::Passthrough;
            ctx.passthrough_credential = Some(secret);
        } else {
            ctx.auth_mode = AuthMode::Virtual;
        }

        if let Some((vkey_id, limits)) = self.limits.limits_for(user).await {
            ctx.extensions.insert(VkeyBudget { vkey_id, limits });
        }
        Ok(())
    }
}

impl AuthStage {
    /// Authenticates `wa_key` into `ctx.user`. Returns `Ok(false)` after blocking the request;
    /// backend faults (db/JWKS) surface as `StageError::Upstream`, failing closed.
    async fn authenticate_into(
        &self,
        ctx: &mut PipelineContext,
        wa_key: &str,
    ) -> Result<bool, StageError> {
        match self.authenticator.authenticate(wa_key).await {
            Ok(u) => {
                ctx.user = Some(u);
                Ok(true)
            }
            Err(AuthError::Overloaded) => {
                block(ctx, 503, "authentication_overloaded");
                Ok(false)
            }
            Err(e @ (AuthError::Db | AuthError::JwksUnavailable(_))) => {
                Err(StageError::Upstream(e.to_string()))
            }
            Err(e) => {
                tracing::warn!(error = %e, "auth failed: credential rejected");
                block(ctx, 401, "unauthenticated");
                Ok(false)
            }
        }
    }
}
