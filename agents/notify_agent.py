"""
SciGate — Agent 5: Notification Fan-Out
────────────────────────────────────────
Sends scan results to all configured notification channels:
  - VCS commit status check
  - Mattermost / ntfy / Email (Postal) / Teams
  - Grafana OnCall escalation for CRITICAL grade
  - Badge SVG generation

Usage:
    from agents.notify_agent import notify

    result = notify(scan_result, repo="owner/repo", pr_url=None)
"""

from __future__ import annotations

import os
import logging
from typing import Any

logger = logging.getLogger("scigate.notify")


def notify(
    scan: dict,
    repo: str,
    pr_url: str | None = None,
    scan_id: str = "",
) -> dict[str, Any]:
    """Fan out scan results to all configured channels."""
    from integrations.notify.base import ScanEvent, fan_out

    event = ScanEvent(
        repo=repo,
        domain=scan.get("domain", "unknown"),
        score=scan.get("scores", {}).get("total", 0),
        grade=scan.get("grade", "CRITICAL"),
        gate_blocked=scan.get("gate_blocked", False),
        scan_id=scan_id,
        pr_url=pr_url,
        regression_detected=scan.get("regression_detected", False),
        fixes_count=len(scan.get("fixes", [])),
        dashboard_url=os.environ.get("SCIGATE_DASHBOARD_URL", ""),
    )

    sent_to = fan_out(event)

    vcs_posted = False
    if os.environ.get("VCS_PROVIDER"):
        try:
            from integrations.vcs import get_vcs_adapter
            vcs = get_vcs_adapter()
            status = "success" if not event.gate_blocked else "failure"
            summary = f"SciGate: {event.score}/100 ({event.grade})"
            sha = scan.get("commit_sha", "")
            if sha and sha != "unknown":
                vcs.post_check(repo, sha, status, summary)
                vcs_posted = True
        except Exception as exc:
            logger.warning("VCS check post failed: %s", exc)

    badge_url = _generate_badge_url(event)

    return {
        "channels_notified": sent_to,
        "vcs_check_posted": vcs_posted,
        "badge_url": badge_url,
        "grade": event.grade,
        "score": event.score,
    }


def _generate_badge_url(event) -> str:
    """Generate shields.io badge URL for the score."""
    colors = {
        "EXCELLENT": "brightgreen",
        "GOOD": "green",
        "FAIR": "yellow",
        "POOR": "orange",
        "CRITICAL": "red",
    }
    color = colors.get(event.grade, "lightgrey")
    return (
        f"https://img.shields.io/badge/SciGate-"
        f"{event.score}%2F100-{color}"
        f"?style=flat-square&logo=data:image/svg+xml;base64,"
        "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDJDNi40OCAyIDIgNi40OCAyIDEyczQuNDggMTAgMTAgMTAgMTAtNC40OCAxMC0xMFMxNy41MiAyIDEyIDJ6bTAgMThhOCA4IDAgMSAxIDAtMTYgOCA4IDAgMCAxIDAgMTZ6IiBmaWxsPSIjZmZmIi8+PC9zdmc+"
    )


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="SciGate Agent 5 — Notification fan-out")
    parser.add_argument("--score-json", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-url", default=None)
    args = parser.parse_args()

    with open(args.score_json) as f:
        scan = json.load(f)

    result = notify(scan, args.repo, args.pr_url)
    print(json.dumps(result, indent=2))
