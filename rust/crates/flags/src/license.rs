//! License-server REST source (`/api/v2/features`, `/api/v2/validate`).

use std::fmt;
use std::time::Duration;

use serde_json::{json, Value};

use crate::error::SourceError;
use crate::{BoxFut, LicenseSource, Scope, Tier};

/// Talks to `license.penguintech.io`. The license key is a secret: redacted from
/// `Debug`, never logged, never in error text.
pub struct HttpLicenseSource {
    http: reqwest::Client,
    base: String,
    key: String,
    product: String,
    timeout: Duration,
}

impl fmt::Debug for HttpLicenseSource {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("HttpLicenseSource")
            .field("base", &self.base)
            .field("product", &self.product)
            .field("key", &"<redacted>")
            .finish()
    }
}

impl HttpLicenseSource {
    /// Builds a source for `server_url`; every call is bounded by `timeout`.
    pub fn new(
        http: reqwest::Client,
        server_url: &str,
        key: String,
        product: String,
        timeout: Duration,
    ) -> Self {
        Self {
            http,
            base: server_url.trim_end_matches('/').to_owned(),
            key,
            product,
            timeout,
        }
    }

    async fn post(&self, path: &str, body: Value) -> Result<Value, SourceError> {
        let resp = self
            .http
            .post(format!("{}{path}", self.base))
            .timeout(self.timeout)
            .bearer_auth(&self.key)
            .json(&body)
            .send()
            .await
            .map_err(|e| SourceError::Unreachable(e.without_url().to_string()))?;
        let status = resp.status();
        if !status.is_success() {
            return Err(SourceError::Unreachable(format!("status {status}")));
        }
        resp.json()
            .await
            .map_err(|e| SourceError::Malformed(e.without_url().to_string()))
    }
}

impl LicenseSource for HttpLicenseSource {
    fn feature<'a>(
        &'a self,
        feature: &'a str,
        _scope: &'a Scope,
    ) -> BoxFut<'a, Result<bool, SourceError>> {
        Box::pin(async move {
            let data = self
                .post(
                    "/api/v2/features",
                    json!({"product": self.product, "feature": feature}),
                )
                .await?;
            let features = data
                .get("features")
                .and_then(Value::as_array)
                .ok_or_else(|| SourceError::Malformed("missing features".into()))?;
            Ok(features
                .first()
                .and_then(|f| f.get("entitled"))
                .and_then(Value::as_bool)
                .unwrap_or(false))
        })
    }

    fn tier<'a>(&'a self, _scope: &'a Scope) -> BoxFut<'a, Result<Tier, SourceError>> {
        Box::pin(async move {
            let data = self
                .post("/api/v2/validate", json!({"product": self.product}))
                .await?;
            if data.get("valid").and_then(Value::as_bool) != Some(true) {
                return Ok(Tier::Free);
            }
            data.get("tier")
                .and_then(Value::as_str)
                .ok_or_else(|| SourceError::Malformed("missing tier".into()))?
                .parse()
                .map_err(|_| SourceError::Malformed("unknown tier".into()))
        })
    }
}
