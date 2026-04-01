"""ntfy notification adapter — self-hostable push notifications.

ntfy (https://ntfy.sh) is a simple HTTP-based pub-sub notification service.
Self-host or use the public instance.

Set NTFY_URL and NTFY_TOPIC environment variables.
"""

from __future__ import annotations

import os

from integrations.notify.base import NotifyAdapter, ScanEvent, _register

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


@_register("ntfy")
class NtfyAdapter(NotifyAdapter):
    def __init__(self):
        self.url = os.environ.get("NTFY_URL", "https://ntfy.sh")
        self.topic = os.environ.get("NTFY_TOPIC", "scigate")

    def send(self, event: ScanEvent) -> None:
        if not HAS_HTTPX:
            return
        icon = {"EXCELLENT": "white_check_mark", "GOOD": "large_green_circle",
                "FAIR": "warning", "POOR": "red_circle", "CRITICAL": "rotating_light"
                }.get(event.grade, "memo")
        httpx.post(
            f"{self.url}/{self.topic}",
            headers={
                "Title": f"SciGate: {event.repo} scored {event.score}/100 ({event.grade})",
                "Tags": icon,
                "Priority": "5" if event.grade == "CRITICAL" else "3",
            },
            content=(
                f"Repo: {event.repo}\n"
                f"Score: {event.score}/100 | Grade: {event.grade}\n"
                f"Gate: {'BLOCKED' if event.gate_blocked else 'CLEAR'}\n"
                f"Fixes: {event.fixes_count}"
            ),
        )

    def supports_grade(self, grade: str) -> bool:
        return True
