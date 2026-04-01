"""SciGate CLI — Reproducibility Credit Score for scientific codebases.

Usage:
    scigate audit /path/to/repo           # audit + findings table
    scigate audit /path/to/repo --journal nature  # journal checklist
    scigate scan  /path/to/repo           # full scan, JSON output
    scigate dashboard                     # launch interactive UI
    scigate install-hook                  # install pre-commit hook
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

PRE_COMMIT_HOOK = textwrap.dedent("""\
    #!/usr/bin/env bash
    # SciGate pre-commit hook — fast subset scan (seeds, paths, license)
    set -e

    THRESHOLD="${SCIGATE_THRESHOLD:-75}"

    SCORE=$(python -c "
    import sys, json
    sys.path.insert(0, '.')
    from agents.audit_agent import RepoReader, audit
    r = RepoReader(mode='local', path='.')
    result = audit(r)
    print(result['scores']['total'])
    " 2>/dev/null)

    if [ -z "$SCORE" ]; then
        echo "[SciGate] Warning: audit failed, skipping check"
        exit 0
    fi

    if [ "$SCORE" -lt "$THRESHOLD" ]; then
        echo ""
        echo "╭──────────────────────────────────────────────╮"
        echo "│  SciGate: Score $SCORE / 100 — below $THRESHOLD      │"
        echo "│  Run 'scigate audit .' for details            │"
        echo "│  Use --no-verify to bypass                    │"
        echo "╰──────────────────────────────────────────────╯"
        echo ""
        exit 1
    fi

    echo "[SciGate] Score: $SCORE / 100 ✓"
""")


@click.group()
@click.version_option(package_name="scigate")
def main():
    """SciGate — Reproducibility Credit Score for scientific codebases."""
    pass


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--json-out", "-j", is_flag=True, help="Output raw JSON")
@click.option("--journal", type=click.Choice(["nature", "neurips", "plos-one"]),
              default=None, help="Check against journal requirements")
def audit(repo_path: str, json_out: bool, journal: str | None):
    """Run the production audit agent on a repository."""
    from agents.audit_agent import RepoReader, audit as run_audit, journal_checklist

    console.print(f"[bold]Auditing:[/bold] {repo_path}")
    reader = RepoReader(mode="local", path=repo_path)
    result = run_audit(reader)

    if journal:
        result["journal_checklist"] = journal_checklist(result, journal)

    if json_out:
        click.echo(json.dumps(result, indent=2))
        return

    scores = result["scores"]
    grade = result["grade"]
    domain = result["domain"]
    total = scores["total"]
    projected = result.get("projected_score", total)
    effort = result.get("total_effort_label", "?")

    color = "green" if total >= 75 else "yellow" if total >= 50 else "red"
    proj_color = "green" if projected >= 75 else "yellow" if projected >= 50 else "red"

    console.print()
    console.print(Panel(
        f"[bold {color}]{total}/100  Grade: {grade}[/bold {color}]\n"
        f"Domain: {domain}\n\n"
        f"  Environment:    {scores['env']} / 17\n"
        f"  Random Seeds:   {scores['seeds']} / 17\n"
        f"  Data Provenance:{scores['data']} / 17\n"
        f"  Documentation:  {scores['docs']} / 17\n"
        f"  Testing:        {scores.get('testing', 0)} / 17\n"
        f"  Compliance:     {scores.get('compliance', 0)} / 15\n\n"
        f"  [{proj_color}]Projected: {projected}/100 after fixes ({effort})[/{proj_color}]",
        title="[bold]SciGate Reproducibility Credit Score[/bold]",
        border_style=color,
    ))

    if result.get("fixes"):
        table = Table(title="Suggested Fixes", show_lines=True)
        table.add_column("#", width=4)
        table.add_column("Title", min_width=24)
        table.add_column("Dim", width=10)
        table.add_column("Pts", width=5)
        table.add_column("Effort", width=7)
        table.add_column("Why it matters", min_width=30, max_width=50)

        for fix in result["fixes"]:
            table.add_row(
                str(fix["rank"]),
                fix["title"],
                fix["dimension"],
                f"+{fix['points_recoverable']}",
                fix.get("effort_label", "?"),
                (fix.get("explanation", "")[:80] + "...") if len(fix.get("explanation", "")) > 80 else fix.get("explanation", ""),
            )
        console.print(table)

    if journal and "journal_checklist" in result:
        jc = result["journal_checklist"]
        console.print()
        jcolor = "green" if jc["passed"] == jc["total"] else "yellow" if jc["passed"] >= jc["total"] // 2 else "red"
        console.print(Panel(
            f"[bold]{jc['summary']}[/bold]\n\n" +
            "\n".join(
                f"  {'[green]✓[/green]' if c['passed'] else '[red]✗[/red]'}  {c['criterion']}"
                for c in jc["checks"]
            ) +
            (f"\n\n[dim]Reference: {jc['url']}[/dim]" if jc.get("url") else ""),
            title=f"[bold]Journal Checklist: {jc['journal']}[/bold]",
            border_style=jcolor,
        ))


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


@main.command("install-hook")
@click.option("--threshold", type=int, default=75, help="Minimum score to pass")
def install_hook(threshold: int):
    """Install SciGate as a git pre-commit hook."""
    git_dir = Path(".git")
    if not git_dir.exists():
        console.print("[red]Error: not a git repository[/red]")
        raise SystemExit(1)

    hook_path = git_dir / "hooks" / "pre-commit"
    hook_content = PRE_COMMIT_HOOK.replace(
        'THRESHOLD="${SCIGATE_THRESHOLD:-75}"',
        f'THRESHOLD="${{SCIGATE_THRESHOLD:-{threshold}}}"',
    )

    if hook_path.exists():
        console.print(f"[yellow]Warning: {hook_path} already exists. Overwriting.[/yellow]")

    hook_path.write_text(hook_content)
    hook_path.chmod(0o755)
    console.print(f"[green]✓ Pre-commit hook installed at {hook_path}[/green]")
    console.print(f"  Threshold: {threshold}")
    console.print("  Bypass with: git commit --no-verify")


if __name__ == "__main__":
    main()
