"""Badge generation for Reproducibility Credit Score."""

from __future__ import annotations

from urllib.parse import quote

from scigate.scoring.engine import ScoreBreakdown


def badge_url(score: ScoreBreakdown) -> str:
    label = quote("SciGate Score")
    message = quote(f"{score.total_score:.0f}/100 {score.grade}")
    return f"https://img.shields.io/badge/{label}-{message}-{score.badge_color}?style=for-the-badge"


def badge_markdown(score: ScoreBreakdown, repo_url: str = "") -> str:
    url = badge_url(score)
    link = repo_url or "#"
    return f"[![SciGate Score]({url})]({link})"


def score_summary_markdown(score: ScoreBreakdown) -> str:
    lines = [
        f"# SciGate Reproducibility Report",
        f"",
        f"**Score: {score.total_score:.0f}/100 ({score.grade})**",
        f"**Field: {score.field}** (confidence: {score.field_confidence:.0%})",
        f"",
        f"| Dimension | Score |",
        f"|-----------|-------|",
        f"| Environment | {score.env:.0f} / 25 |",
        f"| Random Seeds | {score.seeds:.0f} / 25 |",
        f"| Data Provenance | {score.data:.0f} / 25 |",
        f"| Documentation | {score.docs:.0f} / 25 |",
        f"",
    ]

    if score.deductions:
        lines.append("## Issues Found")
        lines.append("")
        lines.append("| Dim | Check | Issue | Points |")
        lines.append("|-----|-------|-------|--------|")
        for d in score.deductions:
            lines.append(
                f"| {d['dimension']} | `{d['check_id']}` | {d['title']} | -{d['points_lost']:.0f} |"
            )
        lines.append("")

    lines.append(f"## Badge")
    lines.append("")
    lines.append(f"```markdown")
    lines.append(badge_markdown(score))
    lines.append(f"```")

    return "\n".join(lines)
