//! Shared HTTP plumbing for the local connectors: pooled client, status mapping, the
//! `Retry-After` parser, the SSE/NDJSON idle timeout and the UTF-8-safe line splitter.

use std::future::Future;
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use bytes::Bytes;
use futures_core::stream::BoxStream;
use futures_core::Stream;
use futures_util::StreamExt;
use http::{header, HeaderMap, StatusCode};
use pipeline::ConnectorError;
use secrecy::{ExposeSecret, SecretString};
use serde_json::Value;
use tokio::time::Sleep;

/// Largest single NDJSON/SSE line accepted; a longer one is a malformed upstream.
const MAX_LINE_BYTES: usize = 4 * 1024 * 1024;

/// Pooled-client and per-connector settings. Defaults mirror the Python 300 s ceiling.
#[derive(Clone)]
pub struct ConnectorConfig {
    /// Base URL of the runtime, e.g. `http://ollama:11434`.
    pub endpoint: String,
    /// TCP connect timeout.
    pub connect_timeout: Duration,
    /// Total timeout for a non-streaming call.
    pub request_timeout: Duration,
    /// Max gap between streamed frames (`response_idle_timeout`).
    pub idle_timeout: Duration,
    /// Optional static bearer for a protected local runtime (llama-server `--api-key`).
    pub bearer: Option<SecretString>,
    /// Default model name when a request carries none (llama-server).
    pub default_model: Option<String>,
}

impl ConnectorConfig {
    /// Config for `endpoint` with default timeouts and no bearer.
    pub fn new(endpoint: impl Into<String>) -> Self {
        Self {
            endpoint: endpoint.into().trim_end_matches('/').to_owned(),
            connect_timeout: Duration::from_secs(5),
            request_timeout: Duration::from_secs(300),
            idle_timeout: Duration::from_secs(300),
            bearer: None,
            default_model: None,
        }
    }
}

/// Builds the pooled `reqwest::Client` (rustls, keep-alive, bounded idle pool).
pub fn build_client(cfg: &ConnectorConfig) -> Result<reqwest::Client, reqwest::Error> {
    reqwest::Client::builder()
        .connect_timeout(cfg.connect_timeout)
        .pool_idle_timeout(Duration::from_secs(90))
        .pool_max_idle_per_host(16)
        .tcp_keepalive(Duration::from_secs(30))
        .build()
}

/// Maps a transport failure to a generic connector error (no URL or upstream detail).
pub(crate) fn transport_error(e: &reqwest::Error) -> ConnectorError {
    if e.is_timeout() {
        ConnectorError::Timeout
    } else {
        ConnectorError::Upstream("upstream unreachable".to_owned())
    }
}

/// Maps a failure reading/decoding a 200 reply body: a timeout stays a timeout, anything else
/// is a generic malformed-reply fault (the body content is never surfaced).
pub(crate) fn reply_error(e: &reqwest::Error, what: &str) -> ConnectorError {
    if e.is_timeout() {
        ConnectorError::Timeout
    } else {
        ConnectorError::Upstream(format!("malformed {what} reply"))
    }
}

/// POSTs `payload` and returns the response only on HTTP 200; any other status becomes a
/// body-free [`ConnectorError::Status`].
pub(crate) async fn post_json(
    client: &reqwest::Client,
    cfg: &ConnectorConfig,
    path: &str,
    payload: &Value,
    total_timeout: Option<Duration>,
) -> Result<reqwest::Response, ConnectorError> {
    let mut rb = client.post(format!("{}{path}", cfg.endpoint)).json(payload);
    if let Some(t) = total_timeout {
        rb = rb.timeout(t);
    }
    if let Some(b) = &cfg.bearer {
        rb = rb.bearer_auth(b.expose_secret());
    }
    let resp = rb.send().await.map_err(|e| transport_error(&e))?;
    let status = resp.status();
    if status != StatusCode::OK {
        return Err(ConnectorError::Status {
            status: status.as_u16(),
            retry_after: retry_after(status, resp.headers()),
        });
    }
    Ok(resp)
}

/// Backoff hint from a 429's headers: `Retry-After` (seconds or HTTP-date), `retry-after-ms`,
/// then `x-ratelimit-reset` (seconds, or a unix timestamp). Hand-written after the shape of
/// agentgateway's `outlierdetection::retry_after`; no code vendored.
pub fn retry_after(status: StatusCode, h: &HeaderMap) -> Option<Duration> {
    if status != StatusCode::TOO_MANY_REQUESTS {
        return None;
    }
    retry_after_at(h, SystemTime::now())
}

/// [`retry_after`] against an explicit clock, for deterministic tests.
pub(crate) fn retry_after_at(h: &HeaderMap, now: SystemTime) -> Option<Duration> {
    let get = |name: &str| h.get(name).and_then(|v| v.to_str().ok());
    if let Some(v) = get(header::RETRY_AFTER.as_str()) {
        if let Ok(secs) = v.trim().parse::<u64>() {
            return Some(Duration::from_secs(secs));
        }
        if let Ok(date) = httpdate::parse_http_date(v.trim()) {
            if let Ok(d) = date.duration_since(now) {
                return Some(d);
            }
        }
    }
    if let Some(ms) = get("retry-after-ms").and_then(|v| v.trim().parse::<u64>().ok()) {
        return Some(Duration::from_millis(ms));
    }
    if let Some(reset) = get("x-ratelimit-reset").and_then(|v| v.trim().parse::<u64>().ok()) {
        const THIRTY_DAYS: u64 = 30 * 24 * 60 * 60;
        if reset < THIRTY_DAYS {
            return Some(Duration::from_secs(reset));
        }
        if let Ok(d) = (UNIX_EPOCH + Duration::from_secs(reset)).duration_since(now) {
            return Some(d);
        }
    }
    None
}

/// A body read failure: the idle window elapsed, or the transport broke.
#[derive(Debug)]
pub(crate) enum BodyError {
    /// No frame arrived within the idle window.
    Idle,
    /// The underlying read failed.
    Read,
}

/// Bounds the gap between frames of a streaming body (`response_idle_timeout`). Reimplemented
/// over a byte stream after agentgateway's `http::idle_timeout::IdleTimeout`; the window only
/// starts when a read is pending, so a slow consumer never trips it.
pub(crate) struct IdleTimeout {
    inner: BoxStream<'static, Result<Bytes, reqwest::Error>>,
    duration: Duration,
    sleep: Option<Pin<Box<Sleep>>>,
    done: bool,
}

impl IdleTimeout {
    /// Wraps `inner` with a per-frame idle window of `duration`.
    pub(crate) fn new(
        inner: BoxStream<'static, Result<Bytes, reqwest::Error>>,
        duration: Duration,
    ) -> Self {
        Self {
            inner,
            duration,
            sleep: None,
            done: false,
        }
    }
}

impl Stream for IdleTimeout {
    type Item = Result<Bytes, BodyError>;

    /// Polls the inner stream first; only a pending read arms (or checks) the idle timer.
    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        if this.done {
            return Poll::Ready(None);
        }
        match this.inner.as_mut().poll_next(cx) {
            Poll::Ready(item) => {
                this.sleep = None;
                this.done = matches!(item, None | Some(Err(_)));
                Poll::Ready(item.map(|r| r.map_err(|_| BodyError::Read)))
            }
            Poll::Pending => {
                let duration = this.duration;
                let sleep = this
                    .sleep
                    .get_or_insert_with(|| Box::pin(tokio::time::sleep(duration)));
                if sleep.as_mut().poll(cx).is_ready() {
                    this.done = true;
                    this.sleep = None;
                    Poll::Ready(Some(Err(BodyError::Idle)))
                } else {
                    Poll::Pending
                }
            }
        }
    }
}

/// Splits a byte stream into complete lines, buffering raw bytes so a multibyte UTF-8
/// character split across network chunks is reassembled before decoding.
#[derive(Default)]
pub(crate) struct LineBuffer {
    buf: Vec<u8>,
}

impl LineBuffer {
    /// Feeds bytes; returns the complete lines (decoded, trimmed, blank lines dropped).
    /// Lines that are not valid UTF-8 are skipped, like the Python parsers' `continue`.
    pub(crate) fn push(&mut self, bytes: &[u8]) -> Result<Vec<String>, ConnectorError> {
        self.buf.extend_from_slice(bytes);
        let mut out = Vec::new();
        while let Some(pos) = self.buf.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = self.buf.drain(..=pos).collect();
            push_line(&mut out, &line);
        }
        if self.buf.len() > MAX_LINE_BYTES {
            return Err(ConnectorError::Upstream(
                "upstream line too long".to_owned(),
            ));
        }
        Ok(out)
    }

    /// Flushes a trailing line that had no terminating newline.
    pub(crate) fn finish(&mut self) -> Option<String> {
        let line = std::mem::take(&mut self.buf);
        let mut out = Vec::new();
        push_line(&mut out, &line);
        out.pop()
    }
}

/// Decodes one raw line into `out` if it is valid, non-blank UTF-8.
fn push_line(out: &mut Vec<String>, raw: &[u8]) {
    if let Ok(s) = std::str::from_utf8(raw) {
        let s = s.trim();
        if !s.is_empty() {
            out.push(s.to_owned());
        }
    }
}

/// Turns a streaming response into decoded lines, bounded by the per-frame idle timeout.
/// Dropping the returned stream drops the response body and cancels the upstream request.
pub(crate) fn response_lines(
    resp: reqwest::Response,
    idle: Duration,
) -> BoxStream<'static, Result<String, ConnectorError>> {
    let body = IdleTimeout::new(Box::pin(resp.bytes_stream()), idle);
    Box::pin(async_stream::stream! {
        let mut body = body;
        let mut lines = LineBuffer::default();
        while let Some(frame) = body.next().await {
            let bytes = match frame {
                Ok(b) => b,
                Err(BodyError::Idle) => {
                    yield Err(ConnectorError::Timeout);
                    return;
                }
                Err(BodyError::Read) => {
                    yield Err(ConnectorError::Upstream("upstream stream broken".to_owned()));
                    return;
                }
            };
            match lines.push(&bytes) {
                Ok(ready) => {
                    for l in ready {
                        yield Ok(l);
                    }
                }
                Err(e) => {
                    yield Err(e);
                    return;
                }
            }
        }
        if let Some(last) = lines.finish() {
            yield Ok(last);
        }
    })
}

/// Reads a JSON number as `u64`, defaulting to 0 when absent or not a non-negative integer.
pub(crate) fn count(v: &Value, key: &str) -> u64 {
    v.get(key).and_then(Value::as_u64).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use http::HeaderValue;

    fn headers(pairs: &[(&'static str, &str)]) -> HeaderMap {
        let mut h = HeaderMap::new();
        for (k, v) in pairs {
            h.insert(*k, HeaderValue::from_str(v).unwrap());
        }
        h
    }

    #[test]
    fn retry_after_only_for_429() {
        let h = headers(&[("retry-after", "5")]);
        assert_eq!(retry_after(StatusCode::INTERNAL_SERVER_ERROR, &h), None);
        assert_eq!(
            retry_after(StatusCode::TOO_MANY_REQUESTS, &h),
            Some(Duration::from_secs(5))
        );
    }

    #[test]
    fn retry_after_parses_every_header_form() {
        let now = UNIX_EPOCH + Duration::from_secs(1_800_000_000);
        // seconds
        assert_eq!(
            retry_after_at(&headers(&[("retry-after", " 7 ")]), now),
            Some(Duration::from_secs(7))
        );
        // HTTP-date 30 s in the future (Fri, 15 Jan 2027 08:00:30 GMT = 1_800_000_030)
        let date = httpdate::fmt_http_date(now + Duration::from_secs(30));
        assert_eq!(
            retry_after_at(&headers(&[("retry-after", &date)]), now),
            Some(Duration::from_secs(30))
        );
        // HTTP-date in the past falls through to nothing
        let past = httpdate::fmt_http_date(now - Duration::from_secs(30));
        assert_eq!(
            retry_after_at(&headers(&[("retry-after", &past)]), now),
            None
        );
        // retry-after-ms
        assert_eq!(
            retry_after_at(&headers(&[("retry-after-ms", "250")]), now),
            Some(Duration::from_millis(250))
        );
        // x-ratelimit-reset: small = seconds, large = unix timestamp
        assert_eq!(
            retry_after_at(&headers(&[("x-ratelimit-reset", "12")]), now),
            Some(Duration::from_secs(12))
        );
        assert_eq!(
            retry_after_at(&headers(&[("x-ratelimit-reset", "1800000045")]), now),
            Some(Duration::from_secs(45))
        );
        // past timestamp, garbage and absence -> None
        assert_eq!(
            retry_after_at(&headers(&[("x-ratelimit-reset", "1700000000")]), now),
            None
        );
        assert_eq!(
            retry_after_at(&headers(&[("retry-after", "soon")]), now),
            None
        );
        assert_eq!(retry_after_at(&HeaderMap::new(), now), None);
    }

    #[test]
    fn line_buffer_reassembles_split_multibyte_and_flushes_tail() {
        let mut b = LineBuffer::default();
        let line = "héllo\n".as_bytes();
        // split inside the 2-byte é
        assert!(b.push(&line[..2]).unwrap().is_empty());
        assert_eq!(b.push(&line[2..]).unwrap(), ["héllo"]);
        assert_eq!(b.push(b"a\n\n  \nb\r\nc").unwrap(), ["a", "b"]);
        assert_eq!(b.finish().as_deref(), Some("c"));
        assert_eq!(b.finish(), None);
    }

    #[test]
    fn line_buffer_skips_invalid_utf8_and_bounds_memory() {
        let mut b = LineBuffer::default();
        assert_eq!(
            b.push(&[0xff, 0xfe, b'\n', b'o', b'k', b'\n']).unwrap(),
            ["ok"]
        );
        let big = vec![b'x'; MAX_LINE_BYTES + 1];
        assert!(matches!(b.push(&big), Err(ConnectorError::Upstream(_))));
    }

    #[test]
    fn config_trims_trailing_slash() {
        assert_eq!(ConnectorConfig::new("http://h:1/").endpoint, "http://h:1");
    }
}
