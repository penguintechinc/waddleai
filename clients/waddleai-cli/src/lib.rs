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
pub mod cli;
pub mod config;
pub mod error;
pub mod mcp_shim;
pub mod token_store;
