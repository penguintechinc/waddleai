//! PostHog REST flag source (`POST {host}/flags/?v=2`).

use std::fmt;
use std::time::Duration;

use serde_json::{json, Value};

use crate::error::SourceError;
use crate::{BoxFut, FlagLookup, FlagSource};

/// Evaluates flags against a PostHog host. The project key is a secret: it is
/// redacted from `Debug` and never appears in errors or logs.
pub struct HttpFlagSource {
    http: reqwest::Client,
    url: String,
    key: String,
    timeout: Duration,
}

impl fmt::Debug for HttpFlagSource {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("HttpFlagSource")
            .field("url", &self.url)
            .field("key", &"<redacted>")
            .finish()
    }
}

impl HttpFlagSource {
    /// Builds a source for `host` using project `key`; every call is bounded by `timeout`.
    pub fn new(http: reqwest::Client, host: &str, key: String, timeout: Duration) -> Self {
        Self {
            http,
            url: format!("{}/flags/?v=2", host.trim_end_matches('/')),
            key,
            timeout,
        }
    }
}

/// Maps one `featureFlags` entry: bool as-is, multivariate string => enabled,
/// missing key => `Absent` (global), present-but-null/odd => `Null` (per-id).
fn flag_value(flags: &Value, flag: &str) -> FlagLookup {
    match flags.get(flag) {
        None => FlagLookup::Absent,
        Some(Value::Bool(b)) => FlagLookup::Value(*b),
        Some(Value::String(_)) => FlagLookup::Value(true),
        Some(_) => FlagLookup::Null,
    }
}

impl FlagSource for HttpFlagSource {
    fn fetch<'a>(
        &'a self,
        flag: &'a str,
        distinct_id: &'a str,
    ) -> BoxFut<'a, Result<FlagLookup, SourceError>> {
        Box::pin(async move {
            let resp = self
                .http
                .post(&self.url)
                .timeout(self.timeout)
                .json(&json!({"api_key": self.key, "distinct_id": distinct_id}))
                .send()
                .await
                .map_err(|e| SourceError::Unreachable(e.without_url().to_string()))?;
            let status = resp.status();
            if !status.is_success() {
                return Err(SourceError::Unreachable(format!("status {status}")));
            }
            let body: Value = resp
                .json()
                .await
                .map_err(|e| SourceError::Malformed(e.without_url().to_string()))?;
            let flags = body
                .get("featureFlags")
                .ok_or_else(|| SourceError::Malformed("missing featureFlags".into()))?;
            Ok(flag_value(flags, flag))
        })
    }
}
