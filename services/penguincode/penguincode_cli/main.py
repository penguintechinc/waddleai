"""
PenguinCode CLI - Main entry point for the chat and server modes.
"""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from penguincode_cli.config.settings import load_settings
from penguincode_cli.core import start_repl
from penguincode_cli.core.session import SessionManager

app = typer.Typer(
    name="penguincode",
    help="PenguinCode CLI - AI-powered coding assistant using Ollama",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

console = Console()

# Default models to pull during setup
DEFAULT_MODELS = [
    "llama3.2:3b",
    "qwen2.5-coder:7b",
    "nomic-embed-text",  # Required for docs RAG
]


@app.command()
def chat(
    project_dir: str = typer.Option(
        ".",
        "--project",
        "-p",
        help="Project directory to work with",
    ),
    config_path: str = typer.Option(
        "config.yaml",
        "--config",
        "-c",
        help="Path to config.yaml",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        "-d",
        help="Enable verbose debug logging to /tmp/penguincode.log",
    ),
) -> None:
    """Start an interactive chat session."""
    # Enable debug logging if requested
    if debug:
        from penguincode_cli.core.debug import LOG_FILE, enable_debug

        enable_debug()
        console.print(f"[yellow]Debug mode enabled - logging to {LOG_FILE}[/yellow]\n")

    # Run the REPL in async context
    asyncio.run(start_repl(project_dir, config_path))


@app.command()
def serve(
    port: int = typer.Option(
        50051,
        "--port",
        "-p",
        help="gRPC port",
    ),
    rest_port: int = typer.Option(
        8080,
        "--rest-port",
        "-r",
        help="REST API port",
    ),
    host: str = typer.Option(
        "0.0.0.0",
        "--host",
        "-H",
        help="Host to bind to",
    ),
    config_path: str = typer.Option(
        "config.yaml",
        "--config",
        "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """Start the PenguinCode gRPC + REST server."""
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    from penguincode_cli.server.main import serve as _serve

    asyncio.run(_serve(config_path, host, port, rest_port))


@app.command()
def launch(
    api_url: str = typer.Option(
        "http://localhost:8080",
        "--api-url",
        "-a",
        help="code-api REST URL",
        envvar="PENGUINCODE_API_URL",
    ),
    license_key: str = typer.Option(
        "",
        "--license-key",
        "-l",
        help="PenguinTech license key",
        envvar="LICENSE_KEY",
    ),
    skip_models: bool = typer.Option(
        False,
        "--skip-models",
        help="Skip Ollama model auto-pull",
    ),
    skip_orgs: bool = typer.Option(
        False,
        "--skip-orgs",
        help="Skip GitHub org repo setup",
    ),
    no_exec: bool = typer.Option(
        False,
        "--no-exec",
        help="Write config but don't launch OpenCode (for testing)",
    ),
) -> None:
    """Bootstrap from code-api and launch OpenCode.

    Provisions configuration from the code-api server, writes OpenCode
    config files, auto-pulls Ollama models, and launches OpenCode.
    Falls back to cached config if code-api is unreachable.
    """
    from penguincode_cli.client.bootstrap import bootstrap

    console.print("[bold cyan]PenguinCode Launch[/bold cyan]")
    console.print(f"[dim]API: {api_url}[/dim]")

    try:
        config = asyncio.run(
            bootstrap(
                api_url=api_url,
                license_key=license_key,
                skip_models=skip_models,
                skip_orgs=skip_orgs,
                exec_opencode=not no_exec,
            )
        )
        if no_exec:
            tier = config.get("license", {}).get("tier", "community")
            agents = list(config.get("agents", {}).keys())
            console.print(f"[green]Provisioned[/green] tier={tier} agents={agents}")
    except RuntimeError as e:
        console.print(f"[red]Launch failed:[/red] {e}")
        raise typer.Exit(1) from e


@app.command()
def config(
    action: str = typer.Argument(
        "show",
        help="Action: show, set",
    ),
    key: str = typer.Option(
        None,
        "--key",
        "-k",
        help="Configuration key (for set action)",
    ),
    value: str = typer.Option(
        None,
        "--value",
        "-v",
        help="Configuration value (for set action)",
    ),
    config_path: str = typer.Option(
        "config.yaml",
        "--config",
        "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """Manage configuration."""
    if action == "show":
        try:
            settings = load_settings(config_path)
            console.print("\n[bold cyan]PenguinCode Configuration[/bold cyan]\n")

            # Display key settings
            table = Table(title="Ollama Settings", show_header=True, header_style="bold cyan")
            table.add_column("Setting", style="green")
            table.add_column("Value", style="yellow")
            table.add_row("API URL", settings.ollama.api_url)
            table.add_row("Timeout", f"{settings.ollama.timeout}s")
            console.print(table)

            # Display model assignments
            table = Table(title="\nModel Roles", show_header=True, header_style="bold cyan")
            table.add_column("Role", style="green")
            table.add_column("Model", style="yellow")
            table.add_row("Planning", settings.models.planning)
            table.add_row("Orchestration", settings.models.orchestration)
            table.add_row("Research", settings.models.research)
            table.add_row("Execution", settings.models.execution)
            console.print(table)

            console.print(f"\n[dim]Config file: {config_path}[/dim]\n")

        except FileNotFoundError:
            console.print(f"[red]Config file not found: {config_path}[/red]")
            raise typer.Exit(1) from None
        except Exception as e:
            console.print(f"[red]Error loading config: {str(e)}[/red]")
            raise typer.Exit(1) from e

    elif action == "set":
        if not key or value is None:
            console.print("[red]Error: --key and --value required for set action[/red]")
            raise typer.Exit(1)

        console.print(f"[cyan]Setting {key} = {value}[/cyan]")
        console.print("[yellow]Config update not fully implemented yet[/yellow]")
        console.print("[dim]Hint: Edit config.yaml directly for now[/dim]")

    else:
        console.print(f"[red]Unknown action: {action}[/red]")
        raise typer.Exit(1)


@app.command()
def history(
    project_dir: str = typer.Option(
        ".",
        "--project",
        "-p",
        help="Project directory",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        "-l",
        help="Number of sessions to show",
    ),
) -> None:
    """Show session history."""
    try:
        session_manager = SessionManager(project_dir)
        sessions = session_manager.list_sessions(limit=limit)

        if not sessions:
            console.print("[yellow]No sessions found[/yellow]")
            return

        console.print("\n[bold cyan]Session History[/bold cyan]\n")

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Session ID", style="green")
        table.add_column("Created", style="yellow")
        table.add_column("Messages", style="blue")

        for session in sessions:
            table.add_row(
                session["session_id"],
                session["created_at"],
                str(session["message_count"]),
            )

        console.print(table)
        console.print(f"\n[dim]Project: {Path(project_dir).resolve()}[/dim]\n")

    except Exception as e:
        console.print(f"[red]Error loading history: {str(e)}[/red]")
        raise typer.Exit(1) from e


@app.command()
def setup(
    ollama_url: str = typer.Option(
        "http://localhost:11434",
        "--ollama-url",
        help="Ollama API URL",
    ),
    pull_models: bool = typer.Option(
        True,
        "--pull-models/--no-pull-models",
        help="Pull default models during setup",
    ),
    skip_ollama_check: bool = typer.Option(
        False,
        "--skip-ollama-check",
        help="Skip Ollama connectivity check",
    ),
    skip_deps: bool = typer.Option(
        False,
        "--skip-deps",
        help="Skip Python dependency installation",
    ),
) -> None:
    """Set up PenguinCode: install dependencies, check Ollama, pull models."""
    console.print("\n[bold cyan]PenguinCode Setup[/bold cyan]\n")

    # Install Python dependencies
    if not skip_deps:
        in_venv = sys.prefix != sys.base_prefix
        if not in_venv:
            console.print(
                "[yellow]⚠[/yellow] Not running inside a virtual environment.\n"
                "  Dependency installation skipped to avoid system Python conflicts.\n"
                "  Use [bold cyan]./penguincode setup[/bold cyan] instead — "
                "it auto-creates a venv and installs everything for you."
            )
        else:
            console.print("[dim]Installing Python dependencies...[/dim]")
            project_root = Path(__file__).parent.parent
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-e", str(project_root)],
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout
                )
                if result.returncode == 0:
                    console.print("[green]✓[/green] Python dependencies installed")
                else:
                    console.print(f"[yellow]⚠[/yellow] Dependency install warning: {result.stderr.strip()[:200]}")
            except subprocess.TimeoutExpired:
                console.print("[yellow]⚠[/yellow] Dependency installation timed out")
            except Exception as e:
                console.print(f"[yellow]⚠[/yellow] Could not install dependencies: {e}")

    # Create config directories
    console.print("[dim]Creating configuration directories...[/dim]")
    config_dir = Path.home() / ".config" / "penguincode"
    config_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]✓[/green] Config directory: {config_dir}")

    # Check Ollama connectivity
    if not skip_ollama_check:
        console.print(f"\n[dim]Checking Ollama at {ollama_url}...[/dim]")
        try:
            response = httpx.get(f"{ollama_url}/api/tags", timeout=10)
            if response.status_code == 200:
                console.print("[green]✓[/green] Ollama is running")
                models = response.json().get("models", [])
                if models:
                    console.print(f"[dim]  Found {len(models)} model(s) installed[/dim]")
            else:
                console.print(f"[yellow]⚠[/yellow] Ollama responded with status {response.status_code}")
        except httpx.ConnectError:
            console.print("[red]✗[/red] Cannot connect to Ollama")
            console.print("[dim]  Make sure Ollama is running: ollama serve[/dim]")
            if not pull_models:
                raise typer.Exit(1) from None
            console.print("[yellow]⚠[/yellow] Skipping model pull (Ollama not available)")
            pull_models = False
        except Exception as e:
            console.print(f"[red]✗[/red] Error checking Ollama: {e}")
            pull_models = False

    # Pull default models
    if pull_models:
        console.print("\n[dim]Pulling default models...[/dim]")
        for model in DEFAULT_MODELS:
            console.print(f"[dim]  Pulling {model}...[/dim]")
            try:
                result = subprocess.run(
                    ["ollama", "pull", model],
                    capture_output=True,
                    text=True,
                    timeout=600,  # 10 minute timeout per model
                )
                if result.returncode == 0:
                    console.print(f"[green]✓[/green] {model}")
                else:
                    console.print(f"[yellow]⚠[/yellow] {model}: {result.stderr.strip()}")
            except subprocess.TimeoutExpired:
                console.print(f"[yellow]⚠[/yellow] {model}: Pull timed out")
            except FileNotFoundError:
                console.print("[red]✗[/red] 'ollama' command not found")
                console.print("[dim]  Install Ollama from https://ollama.ai[/dim]")
                break
            except Exception as e:
                console.print(f"[yellow]⚠[/yellow] {model}: {e}")

    console.print("\n[green]✓[/green] [bold]Setup complete![/bold]")
    console.print("[dim]Run 'penguincode chat' to start chatting[/dim]\n")


@app.command(name="install-extension")
def install_extension(
    vscode_path: str = typer.Option(
        None,
        "--vscode-path",
        help="Path to VS Code extensions directory",
    ),
) -> None:
    """Install the PenguinCode VS Code extension."""
    console.print("\n[bold cyan]Installing VS Code Extension[/bold cyan]\n")

    # Find the VSIX file
    script_dir = Path(__file__).parent.parent
    vsix_candidates = [
        script_dir / "vsix-extension",
        script_dir / "penguincode-vscode",
        Path.cwd() / "vsix-extension",
    ]

    vsix_dir = None
    for candidate in vsix_candidates:
        if candidate.exists() and (candidate / "package.json").exists():
            vsix_dir = candidate
            break

    if not vsix_dir:
        console.print("[red]✗[/red] VS Code extension not found")
        console.print("[dim]  Expected in: vsix-extension/ or penguincode-vscode/[/dim]")
        raise typer.Exit(1)

    console.print(f"[dim]Found extension at: {vsix_dir}[/dim]")

    # Determine VS Code extensions directory
    if vscode_path:
        ext_dir = Path(vscode_path)
    else:
        # Default locations
        if os.name == "nt" or os.uname().sysname == "Darwin":  # Windows
            ext_dir = Path.home() / ".vscode" / "extensions"
        else:  # Linux
            ext_dir = Path.home() / ".vscode" / "extensions"

    ext_dir.mkdir(parents=True, exist_ok=True)

    # Check for code CLI
    code_cmd = shutil.which("code")
    if code_cmd:
        console.print("[dim]Installing via VS Code CLI...[/dim]")
        try:
            # Try to install from the extension directory
            result = subprocess.run(
                ["code", "--install-extension", str(vsix_dir)],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                console.print("[green]✓[/green] Extension installed successfully")
                console.print("[dim]  Restart VS Code to activate[/dim]\n")
                return
            else:
                console.print(f"[yellow]⚠[/yellow] CLI install failed: {result.stderr.strip()}")
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] CLI install error: {e}")

    # Manual installation fallback
    console.print("[dim]Performing manual installation...[/dim]")
    target_dir = ext_dir / "penguincode.penguincode-vscode"

    try:
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(vsix_dir, target_dir)
        console.print(f"[green]✓[/green] Extension copied to {target_dir}")
        console.print("[dim]  Restart VS Code to activate[/dim]\n")
    except Exception as e:
        console.print(f"[red]✗[/red] Installation failed: {e}")
        raise typer.Exit(1) from e


def main() -> None:
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
