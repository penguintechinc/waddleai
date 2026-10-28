//! WaddleAI Rust data-plane proxy entrypoint.
use clap::Parser;

/// Command-line interface for the proxy; `--version` is derived from the crate version.
#[derive(Parser)]
#[command(
    name = "waddleai-proxy",
    version,
    about = "WaddleAI Rust data-plane proxy"
)]
struct Cli {}

fn main() {
    let _cli = Cli::parse();
}
