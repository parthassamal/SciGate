"""Reproducibility Credit Score engine.

Wraps the audit report's 4-dimension scores (each 0-25) into a
ScoreBreakdown with grade, badge color, and the full deduction list.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from scigate.agents.audit import AuditReport


@dataclass
class ScoreBreakdown:
    total_score: float
    grade: str
    env: float
    seeds: float
    data: float
    docs: float
    field: str = ""
    field_confidence: float = 0.0
    badge_color: str = "red"
    badge_label: str = ""
    deductions: list[dict] = dc_field(default_factory=list)
    bonuses: list[dict] = dc_field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_score": round(self.total_score, 1),
            "grade": self.grade,
            "badge_color": self.badge_color,
            "badge_label": self.badge_label,
            "field": self.field,
            "field_confidence": round(self.field_confidence, 2),
            "dimensions": {
                "env": round(self.env, 1),
                "seeds": round(self.seeds, 1),
                "data": round(self.data, 1),
                "docs": round(self.docs, 1),
            },
            "deductions": self.deductions,
            "bonuses": self.bonuses,
        }


def compute_score(report: AuditReport) -> ScoreBreakdown:
    total = report.total_score
    grade = _grade(total)
    color = _badge_color(total)

    deductions = [
        {
            "check_id": f.check_id,
            "title": f.title,
            "severity": f.severity.value,
            "dimension": f.dimension,
            "points_lost": f.points_deducted,
        }
        for f in report.findings
        if f.points_deducted > 0
    ]

    return ScoreBreakdown(
        total_score=total,
        grade=grade,
        env=report.env_score,
        seeds=report.seeds_score,
        data=report.data_score,
        docs=report.docs_score,
        field=report.field.value,
        field_confidence=report.field_confidence,
        badge_color=color,
        badge_label=f"SciGate {grade} ({total:.0f}/100)",
        deductions=deductions,
    )


def _grade(score: float) -> str:
    if score >= 90: return "EXCELLENT"
    if score >= 75: return "GOOD"
    if score >= 50: return "FAIR"
    if score >= 25: return "POOR"
    return "CRITICAL"


def _badge_color(score: float) -> str:
    if score >= 90: return "brightgreen"
    if score >= 80: return "green"
    if score >= 70: return "yellowgreen"
    if score >= 60: return "yellow"
    if score >= 45: return "orange"
    return "red"
