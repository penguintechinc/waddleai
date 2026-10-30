//! Local LLM connectors (Ollama, llama.cpp), the `LocalDispatcher` and the `DispatchStage`.
//!
//! Depends on `pipeline` one-way for the seam traits; `DispatchStage` lives here rather than in
//! `pipeline` so the two crates never form a cycle (ruling R23).

mod dispatch;
mod http;
mod llamacpp;
mod local_dispatcher;
mod ollama;
mod registry;

pub use dispatch::{record_dispatch_error, DispatchStage};
pub use http::{build_client, retry_after, ConnectorConfig};
pub use llamacpp::{LlamaCppConnector, PROVIDER as LLAMACPP_PROVIDER};
pub use local_dispatcher::{DispatchConfig, LocalDispatcher};
pub use ollama::{OllamaConnector, PROVIDER as OLLAMA_PROVIDER};
pub use registry::ConnectorRegistry;
