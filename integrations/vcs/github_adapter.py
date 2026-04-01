"""GitHub VCS Adapter — implements VCSAdapter for GitHub REST API v3."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

import httpx

from integrations.vcs.base import PRResult, VCSAdapter


class GitHubVCSAdapter(VCSAdapter):
    def __init__(self):
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.token = os.environ.get("GITHUB_TOKEN", "")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
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
        base_sha = self._get_ref_sha(repo, base)
        self._http.post(
            self._url(repo, "git/refs"),
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        ).raise_for_status()

        r = self._http.get(self._url(repo, f"git/commits/{base_sha}"))
        r.raise_for_status()
        base_tree = r.json()["tree"]["sha"]

        tree_items = [
            {"path": f["file_path"], "mode": "100644", "type": "blob", "content": f["content"]}
            for f in files
        ]
        r = self._http.post(
            self._url(repo, "git/trees"),
            json={"base_tree": base_tree, "tree": tree_items},
        )
        r.raise_for_status()

        r = self._http.post(
            self._url(repo, "git/commits"),
            json={"message": title, "tree": r.json()["sha"], "parents": [base_sha]},
        )
        r.raise_for_status()
        new_sha = r.json()["sha"]

        self._http.patch(
            self._url(repo, f"git/refs/heads/{branch}"),
            json={"sha": new_sha},
        ).raise_for_status()

        r = self._http.post(
            self._url(repo, "pulls"),
            json={"head": branch, "base": base, "title": title, "body": body, "draft": True},
        )
        r.raise_for_status()
        pr_data = r.json()
        return PRResult(url=pr_data["html_url"], number=pr_data["number"], branch=branch)

    def post_check(self, repo: str, sha: str, status: str, summary: str) -> None:
        self._http.post(
            self._url(repo, "statuses/" + sha),
            json={"state": status, "description": summary[:140], "context": "scigate/score"},
        ).raise_for_status()

    def verify_webhook(self, payload: bytes, signature: str, secret: str) -> bool:
        expected = "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        r = self._http.post(self._url(repo, "issues"), json=payload)
        r.raise_for_status()
        return r.json()

    def _get_ref_sha(self, repo: str, ref: str) -> str:
        r = self._http.get(self._url(repo, f"git/ref/heads/{ref}"))
        r.raise_for_status()
        return r.json()["object"]["sha"]
