"""
CI Adapter — abstract interface for continuous integration systems.

Supports Jenkins, Woodpecker CI, and GitHub Actions.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CIJobStatus:
    name: str
    status: str
    configured: bool = True
    last_build: dict[str, Any] = field(default_factory=dict)
    last_success: dict[str, Any] | None = None
    last_failure: dict[str, Any] | None = None
    error: str | None = None


class CIAdapter(ABC):
    @abstractmethod
    def get_job_status(self, job_name: str) -> CIJobStatus:
        """Get current status of a CI job."""
        ...

    @abstractmethod
    def get_build_history(self, job_name: str, limit: int = 10) -> list[dict]:
        """Get recent build history for a job."""
        ...


def get_ci_adapter(provider: str = "") -> CIAdapter:
    """Factory — returns adapter matching provider string or CI_PROVIDER env."""
    provider = (provider or os.environ.get("CI_PROVIDER", "jenkins")).lower()

    if provider == "jenkins":
        from integrations.ci.jenkins import JenkinsCIAdapter
        return JenkinsCIAdapter()
    elif provider == "woodpecker":
        from integrations.ci.woodpecker import WoodpeckerCIAdapter
        return WoodpeckerCIAdapter()
    elif provider in ("github", "gha", "github_actions"):
        from integrations.ci.github_actions import GitHubActionsCIAdapter
        return GitHubActionsCIAdapter()
    else:
        raise ValueError(f"Unknown CI_PROVIDER: {provider}. Use jenkins|woodpecker|gha.")
