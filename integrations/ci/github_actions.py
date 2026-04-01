"""GitHub Actions CI Adapter — wraps GitHub Actions API."""

from __future__ import annotations

import os

from integrations.ci.base import CIAdapter, CIJobStatus

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


class GitHubActionsCIAdapter(CIAdapter):
    def __init__(self):
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        token = os.environ.get("GITHUB_TOKEN", "")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(headers=headers, timeout=15) if HAS_HTTPX else None

    @property
    def configured(self) -> bool:
        return self._http is not None

    def get_job_status(self, job_name: str) -> CIJobStatus:
        if not self.configured:
            return CIJobStatus(name=job_name, status="not_configured", configured=False,
                               error="httpx not installed")
        try:
            r = self._http.get(
                f"{self.base}/repos/{job_name}/actions/runs",
                params={"per_page": 1},
            )
            if r.status_code != 200:
                return CIJobStatus(name=job_name, status="error",
                                   error=f"HTTP {r.status_code}")
            runs = r.json().get("workflow_runs", [])
            if not runs:
                return CIJobStatus(name=job_name, status="not_built")
            latest = runs[0]
            return CIJobStatus(
                name=job_name,
                status=latest.get("conclusion") or latest.get("status", "unknown"),
                last_build={
                    "number": latest.get("run_number"),
                    "result": latest.get("conclusion"),
                    "timestamp": latest.get("created_at"),
                    "url": latest.get("html_url"),
                },
            )
        except Exception as exc:
            return CIJobStatus(name=job_name, status="error", error=str(exc))

    def get_build_history(self, job_name: str, limit: int = 10) -> list[dict]:
        if not self.configured:
            return []
        try:
            r = self._http.get(
                f"{self.base}/repos/{job_name}/actions/runs",
                params={"per_page": limit},
            )
            if r.status_code != 200:
                return []
            return [
                {
                    "number": run["run_number"],
                    "result": run.get("conclusion") or run.get("status"),
                    "timestamp": run.get("created_at"),
                    "url": run.get("html_url"),
                }
                for run in r.json().get("workflow_runs", [])
            ]
        except Exception:
            return []
