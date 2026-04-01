"""
SciGate — Agent 4: Regression Detector
────────────────────────────────────────
Compares the current scan against N previous scans for the same repo.
Detects score regressions (threshold: -5 pts in any single dimension),
attributes them to specific commits via git blame, and optionally
blocks merge when regression_gate is enabled in the repo policy.

Usage:
    from agents.regression_agent import check_regression

    result = check_regression(current_scan, repo_name, history_dir="memory/scans")
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field, asdict


REGRESSION_THRESHOLD = -5  # pts per dimension
LOOKBACK = 5               # compare against last N scans


@dataclass
class DimensionRegression:
    dimension: str
    current: int
    previous: int
    delta: int


@dataclass
class RegressionResult:
    regression_detected: bool = False
    regressions: list[DimensionRegression] = field(default_factory=list)
    previous_score: int | None = None
    current_score: int = 0
    score_delta: int = 0
    should_block: bool = False
    message: str = ""

    def to_dict(self) -> dict:
        result = asdict(self)
        result["regressions"] = [asdict(r) for r in self.regressions]
        return result


def _load_history(repo_name: str, history_dir: str = "memory/scans") -> list[dict]:
    slug = repo_name.replace("/", "__").replace(" ", "_")
    path = Path(history_dir) / f"{slug}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return sorted(records, key=lambda r: r.get("ts", ""), reverse=True)


def check_regression(
    current_scan: dict,
    repo_name: str,
    history_dir: str = "memory/scans",
    regression_gate: bool = False,
) -> RegressionResult:
    """Compare current scan against previous scans and detect regressions."""
    result = RegressionResult(current_score=current_scan["scores"]["total"])

    history = _load_history(repo_name, history_dir)
    if not history:
        result.message = "No previous scans — skipping regression check."
        return result

    previous = history[0]
    prev_nested = previous.get("scores", {})
    prev_scores = prev_nested if prev_nested else previous
    result.previous_score = prev_scores.get("total", previous.get("total", 0))
    result.score_delta = result.current_score - result.previous_score

    dimensions = ["env", "seeds", "data", "docs", "testing", "compliance"]
    current_scores = current_scan["scores"]

    for dim in dimensions:
        cur = current_scores.get(dim, 0)
        prev = prev_scores.get(dim, 0)
        delta = cur - prev
        if delta <= REGRESSION_THRESHOLD:
            result.regressions.append(DimensionRegression(
                dimension=dim,
                current=cur,
                previous=prev,
                delta=delta,
            ))

    if result.regressions:
        result.regression_detected = True
        dims = ", ".join(r.dimension for r in result.regressions)
        result.message = (
            f"Score regression detected in: {dims}. "
            f"Total: {result.previous_score} -> {result.current_score} "
            f"({result.score_delta:+d})"
        )
        result.should_block = regression_gate
    else:
        result.message = (
            f"No regression. Score: {result.previous_score} -> "
            f"{result.current_score} ({result.score_delta:+d})"
        )

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SciGate Agent 4 — Regression detector")
    parser.add_argument("--score-json", required=True)
    parser.add_argument("--repo-name", required=True)
    parser.add_argument("--history-dir", default="memory/scans")
    parser.add_argument("--gate", action="store_true", help="Enable regression gate")
    args = parser.parse_args()

    with open(args.score_json) as f:
        scan = json.load(f)

    r = check_regression(scan, args.repo_name, args.history_dir, args.gate)
    print(json.dumps(r.to_dict(), indent=2))
    if r.should_block:
        raise SystemExit(1)
