"""
VCS Adapter — abstract interface for version control system operations.

All SciGate VCS interactions (PRs, checks, webhooks) go through this
interface. Adding a new provider means implementing one adapter class.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class PRResult:
    url: str
    number: int
    branch: str


class VCSAdapter(ABC):
    @abstractmethod
    def get_file(self, repo: str, path: str, ref: str = "main") -> str | None:
        """Fetch a single file's content from the repository."""
        ...

    @abstractmethod
    def open_draft_pr(
        self,
        repo: str,
        branch: str,
        base: str,
        title: str,
        body: str,
        files: list[dict[str, str]],
    ) -> PRResult:
        """Create a branch, commit files, and open a draft PR."""
        ...

    @abstractmethod
    def post_check(
        self,
        repo: str,
        sha: str,
        status: str,
        summary: str,
    ) -> None:
        """Post a commit status check (success/failure/pending)."""
        ...

    @abstractmethod
    def verify_webhook(
        self,
        payload: bytes,
        signature: str,
        secret: str,
    ) -> bool:
        """Verify HMAC-SHA256 webhook signature."""
        ...

    @abstractmethod
    def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an issue in the repository."""
        ...


def get_vcs_adapter() -> VCSAdapter:
    """Factory — returns the adapter matching VCS_PROVIDER env var."""
    provider = os.environ.get("VCS_PROVIDER", "github").lower()

    if provider == "github":
        from integrations.vcs.github_adapter import GitHubVCSAdapter
        return GitHubVCSAdapter()
    elif provider == "gitea":
        from integrations.vcs.gitea_adapter import GiteaVCSAdapter
        return GiteaVCSAdapter()
    else:
        raise ValueError(f"Unknown VCS_PROVIDER: {provider}. Use 'github' or 'gitea'.")
