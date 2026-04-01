"""
SciGate — Activity Tracker
────────────────────────────
Pulls live activity from GitHub (PRs, commits, code changes) and Jenkins
to give a unified view of repository health alongside the reproducibility
score.

Works without authentication for public repos; set GITHUB_TOKEN for
private repos or higher rate limits. Set JENKINS_URL + JENKINS_TOKEN for
Jenkins integration.

Usage:
    from agents.tracker import get_activity, get_jenkins_status, validate_dependencies

    activity = get_activity("owner/repo")
    jenkins  = get_jenkins_status("my-job")
    deps     = validate_dependencies(reader)
"""

import os
import re
from datetime import datetime, timezone
from typing import Any

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


# ─── GITHUB CLIENT ───────────────────────────────────────────────────────────

class GitHubTracker:
    def __init__(self, repo: str, token: str = ""):
        self.repo = repo
        base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.base = base
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        tok = token or os.environ.get("GITHUB_TOKEN", "")
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        self._http = httpx.Client(headers=headers, timeout=15)

    def _get(self, path: str, params: dict | None = None) -> Any:
        r = self._http.get(f"{self.base}/repos/{self.repo}/{path}", params=params or {})
        if r.status_code != 200:
            return None
        return r.json()

    # ── Pull Requests ─────────────────────────────────────────────────────

    def recent_prs(self, limit: int = 10) -> list[dict]:
        data = self._get("pulls", {"state": "all", "per_page": limit, "sort": "updated"})
        if not data:
            return []
        return [
            {
                "number": pr["number"],
                "title": pr["title"],
                "state": pr["state"],
                "draft": pr.get("draft", False),
                "author": pr["user"]["login"],
                "created_at": pr["created_at"],
                "updated_at": pr["updated_at"],
                "merged_at": pr.get("merged_at"),
                "url": pr["html_url"],
                "additions": pr.get("additions"),
                "deletions": pr.get("deletions"),
                "changed_files": pr.get("changed_files"),
                "labels": [l["name"] for l in pr.get("labels", [])],
            }
            for pr in data
        ]

    # ── Commits ───────────────────────────────────────────────────────────

    def recent_commits(self, limit: int = 15, branch: str = "main") -> list[dict]:
        data = self._get("commits", {"sha": branch, "per_page": limit})
        if not data:
            return []
        return [
            {
                "sha": c["sha"][:8],
                "sha_full": c["sha"],
                "message": c["commit"]["message"].split("\n")[0][:120],
                "author": (c["commit"].get("author") or {}).get("name", "unknown"),
                "date": (c["commit"].get("author") or {}).get("date"),
                "url": c["html_url"],
            }
            for c in data
        ]

    # ── Code Changes (diff stats for a commit or between refs) ────────────

    def commit_diff(self, sha: str) -> dict:
        data = self._get(f"commits/{sha}")
        if not data:
            return {"sha": sha, "files": [], "stats": {}}
        files = [
            {
                "filename": f["filename"],
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "changes": f["changes"],
            }
            for f in data.get("files", [])
        ]
        return {
            "sha": sha[:8],
            "message": data["commit"]["message"].split("\n")[0],
            "stats": data.get("stats", {}),
            "files": files,
        }

    def compare(self, base: str, head: str) -> dict:
        data = self._get(f"compare/{base}...{head}")
        if not data:
            return {"ahead_by": 0, "behind_by": 0, "files": [], "commits": []}
        return {
            "ahead_by": data.get("ahead_by", 0),
            "behind_by": data.get("behind_by", 0),
            "total_commits": data.get("total_commits", 0),
            "files": [
                {
                    "filename": f["filename"],
                    "status": f["status"],
                    "additions": f["additions"],
                    "deletions": f["deletions"],
                }
                for f in data.get("files", [])
            ],
            "commits": [
                {
                    "sha": c["sha"][:8],
                    "message": c["commit"]["message"].split("\n")[0][:120],
                    "author": (c["commit"].get("author") or {}).get("name", "unknown"),
                }
                for c in data.get("commits", [])
            ],
        }

    # ── Summary ───────────────────────────────────────────────────────────

    def activity_summary(self, limit: int = 10) -> dict:
        prs = self.recent_prs(limit)
        commits = self.recent_commits(limit)

        open_prs = [p for p in prs if p["state"] == "open"]
        merged_prs = [p for p in prs if p["merged_at"]]
        scigate_prs = [p for p in prs if any("scigate" in l.lower() for l in p["labels"])]

        return {
            "repo": self.repo,
            "pull_requests": {
                "recent": prs,
                "open_count": len(open_prs),
                "merged_count": len(merged_prs),
                "scigate_fixes": len(scigate_prs),
            },
            "commits": {
                "recent": commits,
                "total_fetched": len(commits),
            },
        }


# ─── JENKINS INTEGRATION ─────────────────────────────────────────────────────

class JenkinsTracker:
    def __init__(self, base_url: str = "", token: str = "", user: str = ""):
        self.base = (base_url or os.environ.get("JENKINS_URL", "")).rstrip("/")
        self.token = token or os.environ.get("JENKINS_TOKEN", "")
        self.user = user or os.environ.get("JENKINS_USER", "")
        self._http = None
        if self.base and HAS_HTTPX:
            auth = (self.user, self.token) if self.user and self.token else None
            self._http = httpx.Client(auth=auth, timeout=15)

    @property
    def configured(self) -> bool:
        return bool(self.base and self._http)

    def job_status(self, job_name: str) -> dict:
        if not self.configured:
            return {"error": "Jenkins not configured", "configured": False}
        try:
            r = self._http.get(f"{self.base}/job/{job_name}/api/json", params={
                "tree": "name,color,lastBuild[number,result,timestamp,duration,url],"
                        "lastSuccessfulBuild[number,timestamp],"
                        "lastFailedBuild[number,timestamp]"
            })
            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}", "configured": True}
            data = r.json()
            last = data.get("lastBuild") or {}
            return {
                "configured": True,
                "name": data.get("name", job_name),
                "status": _jenkins_color_to_status(data.get("color", "notbuilt")),
                "last_build": {
                    "number": last.get("number"),
                    "result": last.get("result"),
                    "timestamp": _epoch_to_iso(last.get("timestamp")),
                    "duration_ms": last.get("duration"),
                    "url": last.get("url"),
                },
                "last_success": _format_jenkins_build(data.get("lastSuccessfulBuild")),
                "last_failure": _format_jenkins_build(data.get("lastFailedBuild")),
            }
        except Exception as exc:
            return {"error": str(exc), "configured": True}

    def recent_builds(self, job_name: str, limit: int = 10) -> list[dict]:
        if not self.configured:
            return []
        try:
            r = self._http.get(
                f"{self.base}/job/{job_name}/api/json",
                params={"tree": f"builds[number,result,timestamp,duration,url]{{0,{limit}}}"},
            )
            if r.status_code != 200:
                return []
            builds = r.json().get("builds", [])
            return [
                {
                    "number": b["number"],
                    "result": b.get("result", "RUNNING"),
                    "timestamp": _epoch_to_iso(b.get("timestamp")),
                    "duration_ms": b.get("duration"),
                    "url": b.get("url"),
                }
                for b in builds
            ]
        except Exception:
            return []


def _jenkins_color_to_status(color: str) -> str:
    mapping = {
        "blue": "success", "blue_anime": "running",
        "red": "failure", "red_anime": "running",
        "yellow": "unstable", "yellow_anime": "running",
        "grey": "pending", "disabled": "disabled",
        "aborted": "aborted", "notbuilt": "not_built",
    }
    return mapping.get(color, color)


def _epoch_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _format_jenkins_build(build: dict | None) -> dict | None:
    if not build:
        return None
    return {
        "number": build.get("number"),
        "timestamp": _epoch_to_iso(build.get("timestamp")),
    }


# ─── DEPENDENCY VALIDATION ───────────────────────────────────────────────────

KNOWN_DEPRECATED = {
    "nose", "pep8", "pyflakes", "pylint-django",
    "optparse", "imp", "distutils",
}

KNOWN_SECURITY_FLAGS = {
    "pyyaml": "< 5.4 has arbitrary code execution via yaml.load()",
    "jinja2": "< 2.11.3 has sandbox escape",
    "urllib3": "< 1.26.5 has CRLF injection",
    "requests": "< 2.20.0 has session fixation",
    "cryptography": "< 3.3.2 has multiple CVEs",
    "pillow": "< 8.1.1 has buffer overflow",
    "django": "< 3.2 has multiple CVEs",
    "flask": "< 2.0 has debugger PIN bypass",
    "numpy": "< 1.22.0 has buffer overflow",
    "tensorflow": "< 2.7.2 has code execution vulns",
    "torch": "check for latest security advisories",
}

def validate_dependencies(reader) -> dict:
    """Analyze dependency files for security, staleness, and quality issues."""
    results = {
        "files_checked": [],
        "total_deps": 0,
        "pinned": 0,
        "unpinned": 0,
        "deprecated": [],
        "security_flags": [],
        "duplicate": [],
        "issues": [],
    }

    dep_files = [
        ("requirements.txt", _parse_requirements),
        ("requirements-dev.txt", _parse_requirements),
        ("Pipfile", _parse_pipfile),
        ("pyproject.toml", _parse_pyproject_deps),
    ]

    all_deps: dict[str, list[str]] = {}

    for filename, parser in dep_files:
        content = reader.read(filename)
        if not content:
            continue
        results["files_checked"].append(filename)
        deps = parser(content)
        for dep in deps:
            name = dep["name"].lower()
            if name in all_deps:
                all_deps[name].append(filename)
            else:
                all_deps[name] = [filename]

            results["total_deps"] += 1
            if dep["pinned"]:
                results["pinned"] += 1
            else:
                results["unpinned"] += 1

            if name in KNOWN_DEPRECATED:
                results["deprecated"].append({
                    "package": dep["name"],
                    "file": filename,
                    "hint": f"{dep['name']} is deprecated — find a modern replacement",
                })

            if name in KNOWN_SECURITY_FLAGS:
                results["security_flags"].append({
                    "package": dep["name"],
                    "file": filename,
                    "advisory": KNOWN_SECURITY_FLAGS[name],
                    "version_spec": dep.get("version_spec", ""),
                })

    for name, files in all_deps.items():
        if len(files) > 1:
            results["duplicate"].append({
                "package": name,
                "files": files,
            })

    if results["unpinned"] > 0:
        results["issues"].append({
            "severity": "high",
            "title": f"{results['unpinned']} unpinned dependencies",
            "hint": "Pin all dependencies with == for reproducible builds",
        })
    if results["deprecated"]:
        results["issues"].append({
            "severity": "medium",
            "title": f"{len(results['deprecated'])} deprecated package(s)",
            "hint": "Replace deprecated packages with maintained alternatives",
        })
    if results["security_flags"]:
        results["issues"].append({
            "severity": "high",
            "title": f"{len(results['security_flags'])} package(s) with known security advisories",
            "hint": "Update flagged packages to patched versions",
        })
    if results["duplicate"]:
        results["issues"].append({
            "severity": "low",
            "title": f"{len(results['duplicate'])} duplicate dependency declarations",
            "hint": "Consolidate dependencies into a single file",
        })

    pin_ratio = results["pinned"] / max(results["total_deps"], 1)
    results["health_score"] = round(pin_ratio * 100)
    results["health_grade"] = (
        "A" if pin_ratio >= 0.95 else
        "B" if pin_ratio >= 0.80 else
        "C" if pin_ratio >= 0.60 else
        "D" if pin_ratio >= 0.40 else "F"
    )

    return results


def _parse_requirements(content: str) -> list[dict]:
    deps = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.match(r'^([a-zA-Z0-9_\-\.]+)\s*(.*)', line)
        if m:
            name = m.group(1)
            spec = m.group(2).strip()
            deps.append({
                "name": name,
                "version_spec": spec,
                "pinned": "==" in spec,
            })
    return deps


def _parse_pipfile(content: str) -> list[dict]:
    deps = []
    in_packages = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped in ("[packages]", "[dev-packages]"):
            in_packages = True
            continue
        if stripped.startswith("["):
            in_packages = False
            continue
        if in_packages and "=" in stripped:
            parts = stripped.split("=", 1)
            name = parts[0].strip().strip('"')
            spec = parts[1].strip().strip('"') if len(parts) > 1 else ""
            deps.append({
                "name": name,
                "version_spec": spec,
                "pinned": "==" in spec or spec == '"*"',
            })
    return deps


def _parse_pyproject_deps(content: str) -> list[dict]:
    deps = []
    in_deps = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("dependencies") and "=" in stripped:
            in_deps = True
            continue
        if in_deps:
            if stripped == "]":
                in_deps = False
                continue
            m = re.match(r'^\s*"([a-zA-Z0-9_\-\.]+)\s*(.*?)"', stripped)
            if m:
                name = m.group(1)
                spec = m.group(2).strip()
                deps.append({
                    "name": name,
                    "version_spec": spec,
                    "pinned": "==" in spec,
                })
    return deps


# ─── CONVENIENCE FUNCTIONS ───────────────────────────────────────────────────

def get_activity(repo: str, limit: int = 10) -> dict:
    if not HAS_HTTPX:
        return {"error": "httpx not installed"}
    tracker = GitHubTracker(repo)
    return tracker.activity_summary(limit)


def get_jenkins_status(job_name: str) -> dict:
    tracker = JenkinsTracker()
    return tracker.job_status(job_name)


def get_jenkins_builds(job_name: str, limit: int = 10) -> list[dict]:
    tracker = JenkinsTracker()
    return tracker.recent_builds(job_name, limit)
