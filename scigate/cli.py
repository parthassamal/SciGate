"""SciGate CLI — Reproducibility Credit Score for scientific codebases.

Usage:
    scigate audit /path/to/repo          # audit + findings table
    scigate score /path/to/repo          # audit + score
    scigate scan  /path/to/repo          # full scan, JSON output for dashboard
    scigate full  /path/to/repo --apply  # audit + score + fix + memory
    scigate dashboard                    # launch interactive UI
    scigate memory stats                 # view org memory
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

from scigate.agents.audit import AuditReport, run_audit
from scigate.agents.memory import OrgMemory
from scigate.scoring.badge import badge_markdown, score_summary_markdown
from scigate.scoring.engine import ScoreBreakdown, compute_score

console = Console()

DEFAULT_MEMORY_PATH = Path.home() / ".scigate" / "org_memory.json"


def _load_memory() -> OrgMemory:
    return OrgMemory.load(DEFAULT_MEMORY_PATH)


def _print_score(score: ScoreBreakdown) -> None:
    color_map = {
        "brightgreen": "green", "green": "green",
        "yellowgreen": "yellow", "yellow": "yellow",
        "orange": "dark_orange", "red": "red",
    }
    rc = color_map.get(score.badge_color, "white")

    console.print()
    console.print(Panel(
        f"[bold {rc}]{score.total_score:.0f}/100  Grade: {score.grade}[/bold {rc}]\n"
        f"Field: {score.field} (confidence: {score.field_confidence:.0%})\n\n"
        f"  Environment:    {score.env:.0f} / 25\n"
        f"  Random Seeds:   {score.seeds:.0f} / 25\n"
        f"  Data Provenance:{score.data:.0f} / 25\n"
        f"  Documentation:  {score.docs:.0f} / 25",
        title="[bold]SciGate Reproducibility Credit Score[/bold]",
        border_style=rc,
    ))


def _print_findings(report: AuditReport) -> None:
    if not report.findings:
        console.print("[green]No reproducibility issues found![/green]")
        return

    table = Table(title="Audit Findings", show_lines=True)
    table.add_column("Sev", style="bold", width=8)
    table.add_column("Dim", width=6)
    table.add_column("ID", width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Pts", width=5)
    table.add_column("File", width=25)

    sev_style = {
        "critical": "bold red", "high": "dark_orange",
        "medium": "yellow", "low": "blue", "info": "dim",
    }

    for f in report.findings:
        table.add_row(
            f"[{sev_style.get(f.severity.value, 'white')}]{f.severity.value.upper()}[/]",
            f.dimension,
            f.check_id,
            f.title,
            f"-{f.points_deducted:.0f}" if f.points_deducted else "",
            (f.file_path or "")[:25],
        )

    console.print(table)


@click.group()
@click.version_option(package_name="scigate")
def main():
    """SciGate — Reproducibility Credit Score for scientific codebases."""
    pass


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--json-out", "-j", is_flag=True, help="Output raw JSON")
def audit(repo_path: str, json_out: bool):
    """Run the audit agent on a repository."""
    console.print(f"[bold]Auditing:[/bold] {repo_path}")
    report = run_audit(repo_path)

    if json_out:
        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        console.print(f"\n[bold]Field:[/bold] {report.field.value} "
                       f"(confidence: {report.field_confidence:.0%})")
        console.print(f"[bold]Files scanned:[/bold] {report.files_scanned}")
        console.print(f"[bold]Score:[/bold] {report.total_score:.0f}/100")
        _print_findings(report)


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--json-out", "-j", is_flag=True)
def score(repo_path: str, json_out: bool):
    """Compute the Reproducibility Credit Score."""
    console.print(f"[bold]Scoring:[/bold] {repo_path}")
    report = run_audit(repo_path)
    sc = compute_score(report)

    if json_out:
        click.echo(json.dumps(sc.to_dict(), indent=2))
    else:
        _print_score(sc)
        console.print(f"\n[dim]Badge markdown:[/dim]")
        console.print(badge_markdown(sc))


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
def scan(repo_path: str):
    """Full scan with JSON output (used by dashboard API)."""
    report = run_audit(repo_path)
    click.echo(json.dumps(report.to_dict(), indent=2))


@main.command()
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--apply", "do_apply", is_flag=True, help="Apply fixes (requires ANTHROPIC_API_KEY)")
@click.option("--output", "-o", type=click.Path(), help="Write Markdown report to file")
def full(repo_path: str, do_apply: bool, output: str | None):
    """Run full pipeline: Audit -> Score -> Fix -> Badge."""
    mem = _load_memory()

    console.print("[bold cyan]Phase 1/3:[/bold cyan] Auditing repository...")
    hints = mem.get_hints("general-science")
    report = run_audit(repo_path, memory_hints=hints)
    _print_findings(report)

    console.print("\n[bold cyan]Phase 2/3:[/bold cyan] Computing score...")
    sc = compute_score(report)
    _print_score(sc)

    if do_apply:
        try:
            from scigate.agents.fix import generate_fix_plan, apply_fixes
            console.print("\n[bold cyan]Phase 3/3:[/bold cyan] Generating fixes via Claude...")
            plan = generate_fix_plan(report)
            console.print(f"[bold]{len(plan.actions)} fix actions[/bold]: {plan.summary}")
            apply_fixes(plan, dry_run=False)

            report_after = run_audit(repo_path)
            sc_after = compute_score(report_after)
            delta = sc_after.total_score - sc.total_score
            console.print(f"\n[bold green]Score after fixes: {sc_after.total_score:.0f}/100 "
                           f"({delta:+.0f})[/bold green]")

            for finding in report.findings:
                mem.record(
                    repo_pattern=finding.check_id,
                    repro_failure_type=finding.title,
                    fix_applied=finding.suggestion or "",
                    score_delta=delta / max(len(report.findings), 1),
                    sci_field=report.field.value,
                )
        except Exception as exc:
            console.print(f"\n[yellow]Fix generation skipped:[/yellow] {exc}")
            console.print("[dim]Set ANTHROPIC_API_KEY to enable Claude-powered fixes.[/dim]")

    console.print("\n[bold cyan]Updating org memory...[/bold cyan]")
    for finding in report.findings:
        mem.record(
            repo_pattern=finding.check_id,
            repro_failure_type=finding.title,
            fix_applied=finding.suggestion or "",
            score_delta=0,
            sci_field=report.field.value,
        )
    console.print(f"[dim]Memory: {len(mem.entries)} patterns.[/dim]")

    md = score_summary_markdown(sc)
    if output:
        Path(output).write_text(md)
        console.print(f"\n[bold]Report written to:[/bold] {output}")


@main.group()
def memory():
    """Manage SciGate org memory."""
    pass


@memory.command()
def stats():
    """Show org memory statistics."""
    mem = _load_memory()
    st = mem.get_stats()

    if st["total_patterns"] == 0:
        console.print("[dim]Org memory is empty. Run `scigate full` to start learning.[/dim]")
        return

    console.print(Panel(
        f"Total patterns: {st['total_patterns']}\n"
        f"Avg confidence: {st['avg_confidence']:.0%}\n"
        f"Fields: {json.dumps(st['fields'], indent=2)}",
        title="[bold]SciGate Org Memory[/bold]",
    ))


@memory.command()
def decay():
    """Apply confidence decay to old patterns."""
    mem = _load_memory()
    before = len(mem.entries)
    mem.decay()
    console.print(f"Decay applied. Patterns: {before} -> {len(mem.entries)}")


@main.command()
@click.option("--port", type=int, default=8742)
@click.option("--no-browser", is_flag=True)
def dashboard(port: int, no_browser: bool):
    """Launch the interactive SciGate dashboard."""
    server_path = Path(__file__).parent.parent / "dashboard" / "server.py"
    if not server_path.exists():
        console.print("[red]Dashboard not found.[/red]")
        sys.exit(1)
    cmd = [sys.executable, str(server_path), "--port", str(port)]
    if no_browser:
        cmd.append("--no-browser")
    console.print(f"[bold]Launching SciGate Dashboard[/bold] on port {port}...")
    subprocess.run(cmd)


if __name__ == "__main__":
    main()
