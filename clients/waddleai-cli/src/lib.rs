//! `waddleai` -- Rust static-binary CLI and MCP stdio shim.
//!
//! A thin client over WaddleAI's `/api/v1`, `/v1`, and `/mcp` surfaces
//! (plan §11.2). No business logic lives in this crate: every command is a
//! direct, typed HTTP call, and the `mcp` subcommand is a pure
//! stdio-to-streamable-HTTP transport bridge. Exposed as a library so
//! integration tests can drive commands without spawning a subprocess
//! where that's more convenient than an end-to-end binary test.

pub mod api_client;
pub mod auth;
pub mod browser;
pub mod cache_configs;
pub mod cilium;
pub mod cli;
pub mod config;
pub mod error;
pub mod fleet;
pub mod harness_sync;
pub mod hooks;
pub mod integrations;
pub mod keys;
pub mod knowledge;
pub mod llamacpp;
pub mod mcp_shim;
pub mod memory;
pub mod ollama;
pub mod orgs;
pub mod providers;
pub mod quotas;
pub mod routing;
pub mod secrets;
pub mod security_policies;
pub mod system;
pub mod teams;
pub mod token_store;
pub mod usage;
pub mod users;
pub mod vllm;
