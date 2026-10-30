//! Provider name -> connector lookup used by the dispatcher.

use std::collections::HashMap;
use std::sync::Arc;

use pipeline::Connector;

/// Registered connectors keyed by provider name (P2: `ollama`, `llamacpp`; P3 adds commercial).
#[derive(Default)]
pub struct ConnectorRegistry {
    connectors: HashMap<&'static str, Arc<dyn Connector>>,
}

impl ConnectorRegistry {
    /// An empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Registers a connector under its own `provider()` name, replacing any previous one.
    pub fn register(&mut self, connector: Arc<dyn Connector>) {
        self.connectors.insert(connector.provider(), connector);
    }

    /// The connector for `provider`, if registered.
    pub fn get(&self, provider: &str) -> Option<Arc<dyn Connector>> {
        self.connectors.get(provider).cloned()
    }

    /// Whether a connector is registered for `provider`.
    pub fn contains(&self, provider: &str) -> bool {
        self.connectors.contains_key(provider)
    }
}
