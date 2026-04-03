"""Woodpecker CI Adapter — wraps Woodpecker REST API.

Woodpecker CI (https://woodpecker-ci.org) is the recommended OSS CI
for self-hosted deployments. YAML-native, Docker-based pipelines.
"""

from __future__ import annotations

import os
import logging

logger = logging.getLogger("scigate.ci.woodpecker")

from integrations.ci.base import CIAdapter, CIJobStatus

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


class WoodpeckerCIAdapter(CIAdapter):
    def __init__(self):
        self.base = os.environ.get("WOODPECKER_URL", "").rstrip("/")
        token = os.environ.get("WOODPECKER_TOKEN", "")
        self._http = None
        if self.base and HAS_HTTPX:
            self._http = httpx.Client(
                headers={"Authorization": f"Bearer {token}"} if token else {},
                timeout=15,
            )

    @property
    def configured(self) -> bool:
        return bool(self.base and self._http)

    def get_job_status(self, job_name: str) -> CIJobStatus:
        if not self.configured:
            return CIJobStatus(name=job_name, status="not_configured", configured=False,
                               error="WOODPECKER_URL not set")
        try:
            owner, repo = job_name.split("/", 1) if "/" in job_name else ("", job_name)
            r = self._http.get(f"{self.base}/api/repos/{owner}/{repo}/pipelines",
                               params={"page": 1, "per_page": 1})
            if r.status_code != 200:
                return CIJobStatus(name=job_name, status="error",
                                   error=f"HTTP {r.status_code}")
            pipelines = r.json()
            if not pipelines:
                return CIJobStatus(name=job_name, status="not_built")
            latest = pipelines[0]
            return CIJobStatus(
                name=job_name,
                status=latest.get("status", "unknown"),
                last_build={
                    "number": latest.get("number"),
                    "result": latest.get("status"),
                    "timestamp": latest.get("created_at"),
                    "duration_ms": (latest.get("finished_at", 0) - latest.get("started_at", 0)) * 1000
                    if latest.get("finished_at") and latest.get("started_at") else None,
                    "url": f"{self.base}/{owner}/{repo}/pipeline/{latest.get('number')}",
                },
            )
        except Exception as exc:
            return CIJobStatus(name=job_name, status="error", error=str(exc))

    def get_build_history(self, job_name: str, limit: int = 10) -> list[dict]:
        if not self.configured:
            return []
        try:
            owner, repo = job_name.split("/", 1) if "/" in job_name else ("", job_name)
            r = self._http.get(
                f"{self.base}/api/repos/{owner}/{repo}/pipelines",
                params={"page": 1, "per_page": limit},
            )
            if r.status_code != 200:
                return []
            return [
                {
                    "number": p["number"],
                    "result": p.get("status", "unknown"),
                    "timestamp": p.get("created_at"),
                    "url": f"{self.base}/{owner}/{repo}/pipeline/{p['number']}",
                }
                for p in r.json()
            ]
        except Exception as exc:
            logger.warning("Woodpecker build history failed for %s: %s", job_name, exc)
            return []
