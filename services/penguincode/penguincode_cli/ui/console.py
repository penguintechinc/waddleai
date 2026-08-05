"""Rich console wrapper and formatting utilities."""

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

# Global console instance
console = Console()


def print_markdown(text: str, title: str | None = None) -> None:
    """
    Print markdown formatted text.

    Args:
        text: Markdown text to print
        title: Optional panel title
    """
    md = Markdown(text)
    if title:
        console.print(Panel(md, title=title, border_style="cyan"))
    else:
        console.print(md)


def print_code(code: str, language: str = "python", title: str | None = None) -> None:
    """
    Print syntax highlighted code.

    Args:
        code: Code to print
        language: Programming language for syntax highlighting
        title: Optional panel title
    """
    syntax = Syntax(code, language, theme="monokai", line_numbers=True)
    if title:
        console.print(Panel(syntax, title=title, border_style="cyan"))
    else:
        console.print(syntax)


def print_info(message: str, title: str | None = None) -> None:
    """
    Print info message.

    Args:
        message: Info message
        title: Optional title
    """
    if title:
        console.print(f"[cyan][bold]{title}:[/bold][/cyan] {message}")
    else:
        console.print(f"[cyan]{message}[/cyan]")


def print_success(message: str, title: str | None = None) -> None:
    """
    Print success message.

    Args:
        message: Success message
        title: Optional title
    """
    if title:
        console.print(f"[green][bold]{title}:[/bold][/green] {message}")
    else:
        console.print(f"[green]✓ {message}[/green]")


def print_warning(message: str, title: str | None = None) -> None:
    """
    Print warning message.

    Args:
        message: Warning message
        title: Optional title
    """
    if title:
        console.print(f"[yellow][bold]{title}:[/bold][/yellow] {message}")
    else:
        console.print(f"[yellow]⚠ {message}[/yellow]")


def print_error(message: str, title: str | None = None) -> None:
    """
    Print error message.

    Args:
        message: Error message
        title: Optional title
    """
    if title:
        console.print(f"[red][bold]{title}:[/bold][/red] {message}")
    else:
        console.print(f"[red]✗ {message}[/red]")


def create_table(title: str, headers: list[str]) -> Table:
    """
    Create a Rich table.

    Args:
        title: Table title
        headers: Column headers

    Returns:
        Table instance
    """
    table = Table(title=title, show_header=True, header_style="bold cyan")
    for header in headers:
        table.add_column(header)
    return table


def print_panel(content: str, title: str, border_style: str = "cyan") -> None:
    """
    Print content in a panel.

    Args:
        content: Panel content
        title: Panel title
        border_style: Border color style
    """
    console.print(Panel(content, title=title, border_style=border_style))
