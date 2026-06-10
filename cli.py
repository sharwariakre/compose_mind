"""Command-line interface for compose-mind."""

import typer
from rich.console import Console

from compose_mind import __version__

app = typer.Typer(help="compose-mind: an AI-assisted Docker Compose CLI.")
console = Console()


@app.callback()
def main() -> None:
    """compose-mind entry point."""
    pass


if __name__ == "__main__":
    app()
