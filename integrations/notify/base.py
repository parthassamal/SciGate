"""
Notification Adapter — abstract interface for scan event notifications.

Supports: Mattermost, ntfy, Postal (email), MS Teams, Grafana OnCall.
"""

from __future__ import annotations

import os
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger("scigate.notify")


@dataclass
class ScanEvent:
    repo: str
    domain: str
    score: int
    grade: str
    gate_blocked: bool
    scan_id: str = ""
    pr_url: str | None = None
    regression_detected: bool = False
    fixes_count: int = 0
    dashboard_url: str = ""


class NotifyAdapter(ABC):
    @abstractmethod
    def send(self, event: ScanEvent) -> None:
        """Send a notification for a scan event."""
        ...

    @abstractmethod
    def supports_grade(self, grade: str) -> bool:
        """Whether this adapter should fire for the given grade."""
        ...


NOTIFY_REGISTRY: dict[str, type[NotifyAdapter]] = {}


def _register(name: str):
    def decorator(cls):
        NOTIFY_REGISTRY[name] = cls
        return cls
    return decorator


def get_notify_adapters() -> list[NotifyAdapter]:
    """Return all configured notification adapters."""
    channels = os.environ.get("SCIGATE_NOTIFY_CHANNELS", "").split(",")
    adapters = []
    for ch in channels:
        ch = ch.strip()
        if ch and ch in NOTIFY_REGISTRY:
            try:
                adapters.append(NOTIFY_REGISTRY[ch]())
            except Exception as exc:
                logger.warning("Failed to init notify adapter '%s': %s", ch, exc)
    return adapters


def fan_out(event: ScanEvent) -> list[str]:
    """Send event to all configured adapters that support this grade."""
    sent_to = []
    for adapter in get_notify_adapters():
        if adapter.supports_grade(event.grade):
            try:
                adapter.send(event)
                sent_to.append(type(adapter).__name__)
            except Exception as exc:
                logger.warning("Notify adapter %s failed: %s", type(adapter).__name__, exc)
    return sent_to
