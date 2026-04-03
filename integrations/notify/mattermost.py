"""Mattermost notification adapter — open-source Slack alternative.

Set MATTERMOST_WEBHOOK_URL environment variable to the incoming webhook URL.
"""

from __future__ import annotations

import os

from integrations.notify.base import NotifyAdapter, ScanEvent, _register

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


@_register("mattermost")
class MattermostAdapter(NotifyAdapter):
    def __init__(self):
        self.webhook = os.environ.get("MATTERMOST_WEBHOOK_URL", "")

    def send(self, event: ScanEvent) -> None:
        if not HAS_HTTPX or not self.webhook:
            return
        color = {"EXCELLENT": "#00C853", "GOOD": "#64DD17",
                 "FAIR": "#FFD600", "POOR": "#FF6D00", "CRITICAL": "#D50000"
                 }.get(event.grade, "#9E9E9E")
        httpx.post(self.webhook, json={
            "attachments": [{
                "color": color,
                "title": f"SciGate: {event.repo}",
                "text": (
                    f"**Score:** {event.score}/100 | **Grade:** {event.grade}\n"
                    f"**Gate:** {'BLOCKED' if event.gate_blocked else 'CLEAR'}\n"
                    f"**Domain:** {event.domain} | **Fixes:** {event.fixes_count}"
                ),
            }],
        }, timeout=10)

    def supports_grade(self, grade: str) -> bool:
        return True
