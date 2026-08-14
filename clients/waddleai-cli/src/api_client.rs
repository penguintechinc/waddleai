//! Thin `/api/v1` (+ `/v1`, `/mcp`) HTTP client.
//!
//! No business logic lives here or anywhere else in this crate (per plan
//! §11.2: "neither holds business logic") -- every method is a direct,
//! typed wrapper over one WaddleAI endpoint. Endpoints marked "planned" in
//! their doc comment don't exist on the server yet (they land with the
//! Management `/api/v1/integrations/*` work, tracked separately); the
//! client-side call is still real, working code -- it simply has nothing
//! to talk to until that lands, exactly like the WebUI's calls to the same
//! surface.

use serde::{de::DeserializeOwned, Deserialize, Serialize};

use crate::error::{CliError, Result};

const USER_AGENT: &str = concat!("waddleai-cli/", env!("CARGO_PKG_VERSION"));

/// Thin wrapper over [`reqwest::Client`] carrying the resolved base URL and
/// (optionally) a bearer token for authenticated calls.
#[derive(Clone)]
pub struct ApiClient {
    http: reqwest::Client,
    base_url: String,
    bearer: Option<String>,
}

impl ApiClient {
    pub fn new(base_url: impl Into<String>, bearer: Option<String>) -> Result<Self> {
        let http = reqwest::Client::builder().user_agent(USER_AGENT).build()?;
        Ok(ApiClient {
            http,
            base_url: base_url.into(),
            bearer,
        })
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    fn authed(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        match &self.bearer {
            Some(token) => builder.bearer_auth(token),
            None => builder,
        }
    }

    async fn handle_response<T: DeserializeOwned>(response: reqwest::Response) -> Result<T> {
        let status = response.status();
        let bytes = response.bytes().await?;
        if status.is_success() {
            return Ok(serde_json::from_slice(&bytes)?);
        }
        let message = extract_error_message(&bytes).unwrap_or_else(|| status.to_string());
        Err(CliError::Api {
            status: status.as_u16(),
            message,
        })
    }

    async fn handle_empty_response(response: reqwest::Response) -> Result<()> {
        let status = response.status();
        if status.is_success() {
            return Ok(());
        }
        let bytes = response.bytes().await?;
        let message = extract_error_message(&bytes).unwrap_or_else(|| status.to_string());
        Err(CliError::Api {
            status: status.as_u16(),
            message,
        })
    }

    pub async fn get_json<T: DeserializeOwned>(&self, path: &str) -> Result<T> {
        let req = self.authed(self.http.get(self.url(path)));
        let resp = req.send().await?;
        Self::handle_response(resp).await
    }

    pub async fn post_json<B: Serialize, T: DeserializeOwned>(
        &self,
        path: &str,
        body: &B,
    ) -> Result<T> {
        let req = self.authed(self.http.post(self.url(path)).json(body));
        let resp = req.send().await?;
        Self::handle_response(resp).await
    }

    pub async fn post_empty(&self, path: &str) -> Result<()> {
        let req = self.authed(self.http.post(self.url(path)));
        let resp = req.send().await?;
        Self::handle_empty_response(resp).await
    }

    pub async fn delete_empty(&self, path: &str) -> Result<()> {
        let req = self.authed(self.http.delete(self.url(path)));
        let resp = req.send().await?;
        Self::handle_empty_response(resp).await
    }

    /// Uploads a file as `multipart/form-data` under the `file` field --
    /// used by `waddleai knowledge upload`.
    pub async fn upload_file<T: DeserializeOwned>(
        &self,
        path: &str,
        file_path: &std::path::Path,
    ) -> Result<T> {
        let file_name = file_path
            .file_name()
            .and_then(|n| n.to_str())
            .ok_or_else(|| CliError::InvalidInput(format!("invalid file name: {file_path:?}")))?
            .to_string();
        let bytes = tokio::fs::read(file_path).await?;
        let part = reqwest::multipart::Part::bytes(bytes).file_name(file_name);
        let form = reqwest::multipart::Form::new().part("file", part);
        let req = self.authed(self.http.post(self.url(path)).multipart(form));
        let resp = req.send().await?;
        Self::handle_response(resp).await
    }

    // ---- Typed endpoint wrappers -------------------------------------

    /// `POST /api/v1/auth/login` -- confirmed live today (username/password).
    pub async fn login_basic(&self, username: &str, password: &str) -> Result<LoginResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            username: &'a str,
            password: &'a str,
        }
        self.post_json("/api/v1/auth/login", &Body { username, password })
            .await
    }

    /// `POST /api/v1/auth/refresh` -- confirmed live today.
    pub async fn refresh(&self, refresh_token: &str) -> Result<LoginResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            refresh_token: &'a str,
        }
        self.post_json("/api/v1/auth/refresh", &Body { refresh_token })
            .await
    }

    /// `POST /api/v1/auth/logout` -- confirmed live today.
    pub async fn logout(&self) -> Result<()> {
        self.post_empty("/api/v1/auth/logout").await
    }

    /// `GET /api/v1/keys` -- confirmed live today.
    pub async fn list_keys(&self) -> Result<KeysResponse> {
        self.get_json("/api/v1/keys").await
    }

    /// `GET /api/v1/usage/summary?days=N` -- confirmed live today.
    pub async fn usage_summary(&self, days: u32) -> Result<serde_json::Value> {
        self.get_json(&format!("/api/v1/usage/summary?days={days}"))
            .await
    }

    /// `GET /v1/models` -- OpenAI-compatible model listing, confirmed live today.
    pub async fn list_models(&self) -> Result<ModelsResponse> {
        self.get_json("/v1/models").await
    }

    /// `GET /api/v1/fleet/status` -- **planned**: lands with the inference
    /// fleet backend abstraction (`feature/inference-fleet-v2`, tracked
    /// separately). Until then this returns a 404 `CliError::Api`.
    pub async fn fleet_status(&self) -> Result<serde_json::Value> {
        self.get_json("/api/v1/fleet/status").await
    }

    /// `POST /api/v1/knowledge` (multipart) -- **planned**: lands with
    /// `feature/knowledge-layer`, tracked separately.
    pub async fn upload_knowledge(&self, file_path: &std::path::Path) -> Result<serde_json::Value> {
        self.upload_file("/api/v1/knowledge", file_path).await
    }

    /// `GET /api/v1/integrations/mcp-endpoints/{id}` -- **planned**: lands
    /// with the Management `/api/v1/integrations/*` surface (plan Task 10),
    /// tracked separately. Fetched *before* [`Self::start_link`] so the
    /// caller has an admin-registered, trusted host to validate the
    /// link-flow's returned `auth_url` against -- see `crate::browser`.
    pub async fn get_mcp_endpoint(&self, endpoint_id: &str) -> Result<McpEndpointSummary> {
        self.get_json(&format!("/api/v1/integrations/mcp-endpoints/{endpoint_id}"))
            .await
    }

    /// `GET /api/v1/integrations/mcp-endpoints/{id}/link` -- **planned**:
    /// lands with the Management `/api/v1/integrations/*` surface (plan
    /// Task 10), tracked separately.
    pub async fn start_link(&self, endpoint_id: &str) -> Result<LinkStartResponse> {
        self.get_json(&format!(
            "/api/v1/integrations/mcp-endpoints/{endpoint_id}/link"
        ))
        .await
    }
}

fn extract_error_message(bytes: &[u8]) -> Option<String> {
    let value: serde_json::Value = serde_json::from_slice(bytes).ok()?;
    value
        .get("error")
        .or_else(|| value.get("message"))
        .and_then(|v| v.as_str())
        .map(str::to_string)
}

#[derive(Debug, Clone, Deserialize)]
pub struct LoginResponse {
    pub access_token: String,
    #[serde(default)]
    pub refresh_token: Option<String>,
    #[serde(default = "default_expires_in")]
    pub expires_in: i64,
}

fn default_expires_in() -> i64 {
    3600
}

#[derive(Debug, Clone, Deserialize)]
pub struct KeysResponse {
    #[serde(default)]
    pub keys: Vec<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ModelsResponse {
    #[serde(default)]
    pub data: Vec<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct LinkStartResponse {
    pub auth_url: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct McpEndpointSummary {
    pub id: String,
    /// The admin-registered base URL of the external MCP endpoint --
    /// treated as the trusted host for validating that endpoint's
    /// link-flow `auth_url` (see `crate::browser::validate_redirect_url`).
    pub url: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    #[tokio::test]
    async fn login_basic_posts_credentials_and_parses_tokens() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/login"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "access_token": "at-123",
                "refresh_token": "rt-456",
                "expires_in": 3600
            })))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), None).expect("client");
        let resp = client.login_basic("alice", "hunter2").await.expect("login");
        assert_eq!(resp.access_token, "at-123");
        assert_eq!(resp.refresh_token.as_deref(), Some("rt-456"));
    }

    #[tokio::test]
    async fn authed_request_sends_bearer_header() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/keys"))
            .and(header("Authorization", "Bearer at-secret"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"keys": []})))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), Some("at-secret".to_string())).expect("client");
        let resp = client.list_keys().await.expect("list_keys");
        assert!(resp.keys.is_empty());
    }

    #[tokio::test]
    async fn error_response_surfaces_server_message() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/keys"))
            .respond_with(
                ResponseTemplate::new(403)
                    .set_body_json(serde_json::json!({"error": "insufficient scope"})),
            )
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), None).expect("client");
        let err = client.list_keys().await.expect_err("should fail");
        match err {
            CliError::Api { status, message } => {
                assert_eq!(status, 403);
                assert_eq!(message, "insufficient scope");
            }
            other => panic!("expected CliError::Api, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn list_models_hits_v1_models() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/v1/models"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "data": [{"id": "smart-router"}]
            })))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), None).expect("client");
        let resp = client.list_models().await.expect("list_models");
        assert_eq!(resp.data.len(), 1);
    }

    #[tokio::test]
    async fn refresh_posts_refresh_token_and_parses_new_tokens() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/refresh"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "access_token": "at-new",
                "refresh_token": "rt-new",
                "expires_in": 1800
            })))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), None).expect("client");
        let resp = client.refresh("rt-old").await.expect("refresh");
        assert_eq!(resp.access_token, "at-new");
        assert_eq!(resp.expires_in, 1800);
    }

    #[tokio::test]
    async fn logout_posts_to_logout_endpoint() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/logout"))
            .respond_with(ResponseTemplate::new(204))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), Some("at-1".to_string())).expect("client");
        client.logout().await.expect("logout");
    }

    #[tokio::test]
    async fn usage_summary_passes_days_query_param() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/usage/summary"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "total_tokens": 42
            })))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), None).expect("client");
        let usage = client.usage_summary(7).await.expect("usage_summary");
        assert_eq!(usage["total_tokens"], 42);
    }

    #[tokio::test]
    async fn fleet_status_hits_planned_endpoint() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/status"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "backends": []
            })))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), None).expect("client");
        let status = client.fleet_status().await.expect("fleet_status");
        assert_eq!(status["backends"], serde_json::json!([]));
    }

    #[tokio::test]
    async fn fleet_status_not_found_surfaces_clean_api_error() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/status"))
            .respond_with(ResponseTemplate::new(404).set_body_string("not found"))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), None).expect("client");
        let err = client.fleet_status().await.expect_err("should 404");
        assert!(matches!(err, CliError::Api { status: 404, .. }));
    }

    #[tokio::test]
    async fn get_mcp_endpoint_returns_registered_url() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/integrations/mcp-endpoints/elder-1"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "id": "elder-1",
                "url": "https://elder.example"
            })))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), Some("at-1".to_string())).expect("client");
        let endpoint = client
            .get_mcp_endpoint("elder-1")
            .await
            .expect("get_mcp_endpoint");
        assert_eq!(endpoint.url, "https://elder.example");
    }

    #[tokio::test]
    async fn start_link_returns_auth_url() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/integrations/mcp-endpoints/elder-1/link"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "auth_url": "https://elder.example/oauth/authorize?..."
            })))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), Some("at-1".to_string())).expect("client");
        let started = client.start_link("elder-1").await.expect("start_link");
        assert!(started.auth_url.starts_with("https://elder.example"));
    }

    #[tokio::test]
    async fn upload_knowledge_sends_multipart_file() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/knowledge"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "id": "doc-1",
                "status": "queued"
            })))
            .mount(&server)
            .await;

        let file_path =
            std::env::temp_dir().join(format!("waddleai-upload-{}.md", uuid::Uuid::new_v4()));
        tokio::fs::write(&file_path, b"# hello\n")
            .await
            .expect("write scratch file");

        let client = ApiClient::new(server.uri(), Some("at-1".to_string())).expect("client");
        let result = client
            .upload_knowledge(&file_path)
            .await
            .expect("upload_knowledge");
        assert_eq!(result["status"], "queued");

        let _ = std::fs::remove_file(&file_path);
    }

    #[tokio::test]
    async fn error_response_with_unparseable_body_falls_back_to_status_text() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/v1/models"))
            .respond_with(ResponseTemplate::new(500).set_body_string("<not json>"))
            .mount(&server)
            .await;

        let client = ApiClient::new(server.uri(), None).expect("client");
        let err = client.list_models().await.expect_err("should fail");
        match err {
            CliError::Api { status, message } => {
                assert_eq!(status, 500);
                assert!(!message.is_empty());
            }
            other => panic!("expected CliError::Api, got {other:?}"),
        }
    }

    #[test]
    fn base_url_accessor_returns_configured_value() {
        let client = ApiClient::new("https://host.example", None).expect("client");
        assert_eq!(client.base_url(), "https://host.example");
    }
}
