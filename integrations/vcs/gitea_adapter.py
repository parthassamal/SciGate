"""Gitea VCS Adapter — implements VCSAdapter for Gitea REST API v1.

Gitea (https://gitea.io) is the recommended self-hosted VCS. Its API is
largely GitHub-compatible, so this adapter shares the same structure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

import httpx

from integrations.vcs.base import PRResult, VCSAdapter


class GiteaVCSAdapter(VCSAdapter):
    def __init__(self):
        self.base = os.environ.get("GITEA_URL", "http://localhost:3000").rstrip("/") + "/api/v1"
        self.token = os.environ.get("GITEA_TOKEN", "")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        self._http = httpx.Client(headers=headers, timeout=30)

    def _url(self, repo: str, path: str) -> str:
        return f"{self.base}/repos/{repo}/{path}"

    def get_file(self, repo: str, path: str, ref: str = "main") -> str | None:
        r = self._http.get(self._url(repo, f"contents/{path}"), params={"ref": ref})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return data.get("content", "")

    def open_draft_pr(
        self, repo: str, branch: str, base: str,
        title: str, body: str, files: list[dict[str, str]],
    ) -> PRResult:
        owner, name = repo.split("/", 1)

        ref_r = self._http.get(self._url(repo, f"branches/{base}"))
        ref_r.raise_for_status()
        base_sha = ref_r.json()["commit"]["id"]

        self._http.post(
            self._url(repo, "branches"),
            json={"new_branch_name": branch, "old_branch_name": base},
        ).raise_for_status()

        for f in files:
            self._http.post(
                self._url(repo, f"contents/{f['file_path']}"),
                json={
                    "message": f"SciGate: update {f['file_path']}",
                    "content": base64.b64encode(f["content"].encode()).decode(),
                    "branch": branch,
                },
            ).raise_for_status()

        r = self._http.post(
            self._url(repo, "pulls"),
            json={"head": branch, "base": base, "title": title, "body": body},
        )
        r.raise_for_status()
        pr_data = r.json()
        return PRResult(url=pr_data["html_url"], number=pr_data["number"], branch=branch)

    def post_check(self, repo: str, sha: str, status: str, summary: str) -> None:
        gitea_status = {"success": "success", "failure": "failure", "pending": "pending"}.get(
            status, "warning"
        )
        self._http.post(
            self._url(repo, f"statuses/{sha}"),
            json={"state": gitea_status, "description": summary[:140], "context": "scigate/score"},
        ).raise_for_status()

    def verify_webhook(self, payload: bytes, signature: str, secret: str) -> bool:
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        r = self._http.post(self._url(repo, "issues"), json=payload)
        r.raise_for_status()
        return r.json()
