//! Binary entry point: parse args, wire the production `TokenStore`,
//! dispatch, and translate a returned error into a clean stderr message +
//! non-zero exit code (never a panic/backtrace for an expected failure).

use clap::Parser;
use waddleai_cli::browser::SystemBrowserOpener;
use waddleai_cli::cli::{run, Cli};
use waddleai_cli::token_store::KeyringTokenStore;

#[tokio::main]
async fn main() -> std::process::ExitCode {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn")),
        )
        .init();

    let cli = Cli::parse();
    let store = KeyringTokenStore;
    let opener = SystemBrowserOpener;

    match run(cli, &store, &opener).await {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("Error: {err}");
            std::process::ExitCode::FAILURE
        }
    }
}
