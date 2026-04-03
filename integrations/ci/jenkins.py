"""Jenkins CI Adapter — wraps Jenkins REST API."""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

from integrations.ci.base import CIAdapter, CIJobStatus

logger = logging.getLogger("scigate.ci.jenkins")

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


class JenkinsCIAdapter(CIAdapter):
    def __init__(self):
        self.base = os.environ.get("JENKINS_URL", "").rstrip("/")
        user = os.environ.get("JENKINS_USER", "")
        token = os.environ.get("JENKINS_TOKEN", "")
        self._http = None
        if self.base and HAS_HTTPX:
            auth = (user, token) if user and token else None
            self._http = httpx.Client(auth=auth, timeout=15)

    @property
    def configured(self) -> bool:
        return bool(self.base and self._http)

    def get_job_status(self, job_name: str) -> CIJobStatus:
        if not self.configured:
            return CIJobStatus(name=job_name, status="not_configured", configured=False,
                               error="JENKINS_URL not set")
        try:
            r = self._http.get(f"{self.base}/job/{job_name}/api/json", params={
                "tree": "name,color,lastBuild[number,result,timestamp,duration,url],"
                        "lastSuccessfulBuild[number,timestamp],"
                        "lastFailedBuild[number,timestamp]"
            })
            if r.status_code != 200:
                return CIJobStatus(name=job_name, status="error",
                                   error=f"HTTP {r.status_code}")
            data = r.json()
            last = data.get("lastBuild") or {}
            return CIJobStatus(
                name=data.get("name", job_name),
                status=_color_to_status(data.get("color", "notbuilt")),
                last_build={
                    "number": last.get("number"),
                    "result": last.get("result"),
                    "timestamp": _epoch_iso(last.get("timestamp")),
                    "duration_ms": last.get("duration"),
                    "url": last.get("url"),
                },
                last_success=_fmt_build(data.get("lastSuccessfulBuild")),
                last_failure=_fmt_build(data.get("lastFailedBuild")),
            )
        except Exception as exc:
            return CIJobStatus(name=job_name, status="error", error=str(exc))

    def get_build_history(self, job_name: str, limit: int = 10) -> list[dict]:
        if not self.configured:
            return []
        try:
            r = self._http.get(
                f"{self.base}/job/{job_name}/api/json",
                params={"tree": f"builds[number,result,timestamp,duration,url]{{0,{limit}}}"},
            )
            if r.status_code != 200:
                return []
            return [
                {
                    "number": b["number"],
                    "result": b.get("result", "RUNNING"),
                    "timestamp": _epoch_iso(b.get("timestamp")),
                    "duration_ms": b.get("duration"),
                    "url": b.get("url"),
                }
                for b in r.json().get("builds", [])
            ]
        except Exception as exc:
            logger.warning("Jenkins build history failed for %s: %s", job_name, exc)
            return []


def _color_to_status(color: str) -> str:
    return {
        "blue": "success", "blue_anime": "running",
        "red": "failure", "red_anime": "running",
        "yellow": "unstable", "yellow_anime": "running",
        "grey": "pending", "disabled": "disabled",
        "aborted": "aborted", "notbuilt": "not_built",
    }.get(color, color)


def _epoch_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _fmt_build(build: dict | None) -> dict | None:
    if not build:
        return None
    return {"number": build.get("number"), "timestamp": _epoch_iso(build.get("timestamp"))}
