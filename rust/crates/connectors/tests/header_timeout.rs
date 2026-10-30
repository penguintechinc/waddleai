//! A runtime that accepts the connection but never sends response headers must time out.

mod common;

use std::sync::Arc;
use std::time::{Duration, Instant};

use common::{ctx, silent_server};
use connectors::{
    ConnectorRegistry, DispatchStage, LlamaCppConnector, LocalDispatcher, OllamaConnector,
};
use pipeline::{IdentityDepseudonymizer, Stage};

fn stage(url: &str, llama: bool) -> DispatchStage {
    let mut conf = common::cfg(url);
    conf.idle_timeout = Duration::from_millis(300);
    let mut reg = ConnectorRegistry::new();
    if llama {
        reg.register(Arc::new(LlamaCppConnector::new(conf).unwrap()));
    } else {
        reg.register(Arc::new(OllamaConnector::new(conf).unwrap()));
    }
    DispatchStage::new(Arc::new(LocalDispatcher::new(
        Arc::new(reg),
        Arc::new(IdentityDepseudonymizer),
        Default::default(),
    )))
}

#[tokio::test]
async fn header_wait_times_out_on_stream_and_non_stream_for_both_connectors() {
    for (llama, model) in [(false, "m"), (true, "llamacpp:m")] {
        for streaming in [false, true] {
            let url = silent_server().await;
            let s = stage(&url, llama);
            let mut c = ctx(model);
            c.stream = streaming;
            let t = Instant::now();
            tokio::time::timeout(Duration::from_secs(5), s.call(&mut c))
                .await
                .expect("dispatch hung waiting for response headers")
                .unwrap();
            assert!(t.elapsed() < Duration::from_secs(3));
            assert_eq!(
                (c.blocked, c.status_code),
                (true, 504),
                "llama={llama} stream={streaming}"
            );
            assert_eq!(c.block_reason.as_deref(), Some("provider_error_504"));
        }
    }
}
