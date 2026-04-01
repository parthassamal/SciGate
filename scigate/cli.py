"""SciGate CLI — Reproducibility Credit Score for scientific codebases.

Usage:
    scigate audit /path/to/repo          # audit + findings table
    scigate scan  /path/to/repo          # full scan, JSON output for dashboard
    scigate dashboard                    # launch interactive UI
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


@click.group()
@click.version_option(package_name="scigate")
def main():
    """SciGate — Reproducibility Credit Score for scientific codebases."""
    pass


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--json-out", "-j", is_flag=True, help="Output raw JSON")
def audit(repo_path: str, json_out: bool):
    """Run the production audit agent on a repository."""
    from agents.audit_agent import RepoReader, audit as run_audit

    console.print(f"[bold]Auditing:[/bold] {repo_path}")
    reader = RepoReader(mode="local", path=repo_path)
    result = run_audit(reader)

    if json_out:
        click.echo(json.dumps(result, indent=2))
    else:
        scores = result["scores"]
        grade = result["grade"]
        domain = result["domain"]
        total = scores["total"]

        color = "green" if total >= 75 else "yellow" if total >= 50 else "red"
        console.print()
        console.print(Panel(
            f"[bold {color}]{total}/100  Grade: {grade}[/bold {color}]\n"
            f"Domain: {domain}\n\n"
            f"  Environment:    {scores['env']} / 17\n"
            f"  Random Seeds:   {scores['seeds']} / 17\n"
            f"  Data Provenance:{scores['data']} / 17\n"
            f"  Documentation:  {scores['docs']} / 17\n"
            f"  Testing:        {scores.get('testing', 0)} / 17\n"
            f"  Compliance:     {scores.get('compliance', 0)} / 15",
            title="[bold]SciGate Reproducibility Credit Score[/bold]",
            border_style=color,
        ))

        if result.get("fixes"):
            table = Table(title="Suggested Fixes", show_lines=True)
            table.add_column("#", width=4)
            table.add_column("Title", min_width=30)
            table.add_column("Dim", width=12)
            table.add_column("Pts", width=5)
            table.add_column("Files", width=25)

            for fix in result["fixes"]:
                table.add_row(
                    str(fix["rank"]),
                    fix["title"],
                    fix["dimension"],
                    f"+{fix['points_recoverable']}",
                    ", ".join(fix["files"])[:25],
                )
            console.print(table)


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
def scan(repo_path: str):
    """Full scan with JSON output (used by dashboard API)."""
    from agents.audit_agent import RepoReader, audit as run_audit

    reader = RepoReader(mode="local", path=repo_path)
    result = run_audit(reader)
    click.echo(json.dumps(result, indent=2))


@main.command()
@click.option("--port", type=int, default=8000)
def dashboard(port: int):
    """Launch the interactive SciGate dashboard (FastAPI)."""
    console.print(f"[bold]Launching SciGate Dashboard[/bold] on http://localhost:{port}")
    cmd = [sys.executable, "-m", "uvicorn", "api.server:app",
           "--host", "0.0.0.0", "--port", str(port)]
    subprocess.run(cmd)


if __name__ == "__main__":
    main()
