"""
SciGate — Activity Tracker
────────────────────────────
Pulls live activity from GitHub (PRs, commits, code changes),
validates dependencies, scans for credential leaks, generates repo maps,
and detects AI config / repo poisoning files.

Works without authentication for public repos; set GITHUB_TOKEN for
private repos or higher rate limits.

Usage:
    from agents.tracker import get_activity, validate_dependencies

    activity = get_activity("owner/repo")
    deps     = validate_dependencies(reader)
"""

import os
import re
import fnmatch
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("scigate.tracker")

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


# ─── DEPENDENCY VALIDATION ───────────────────────────────────────────────────

KNOWN_DEPRECATED = {
    "nose", "pep8", "pyflakes", "pylint-django",
    "optparse", "imp", "distutils",
}

KNOWN_SECURITY_FLAGS = {
    "pyyaml": "< 5.4 — arbitrary code execution via yaml.load()",
    "jinja2": "< 3.1.3 — CVE-2024-22195: XSS via xmlattr filter",
    "urllib3": "< 2.0.7 — CVE-2023-45803: request body not stripped on redirect",
    "requests": "< 2.31.0 — CVE-2023-32681: session cookie leak",
    "cryptography": "< 41.0.0 — CVE-2023-38325: PKCS7 cert validation bypass",
    "pillow": "< 10.0.1 — CVE-2023-44271: DoS via large TIFF; < 8.1.1 buffer overflow",
    "tornado": "< 6.3.3 — CVE-2023-28370: open redirect",
    "django": "< 4.2.8 — CVE-2023-46695: DoS via large file uploads",
    "flask": "< 2.3.2 — CVE-2023-30861: session cookie on every response",
    "certifi": "< 2023.7.22 — CVE-2023-37920: removed e-Tugra root cert",
    "numpy": "< 1.22.0 — CVE-2021-34141: incomplete string comparison / buffer overflow",
    "scipy": "< 1.10.0 — CVE-2023-25399: refcount issue",
    "setuptools": "< 65.5.1 — CVE-2022-40897: ReDoS in package_index",
    "tensorflow": "< 2.7.2 — multiple code execution vulnerabilities",
    "torch": "check PyTorch security advisories for your version",
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


# ─── CREDENTIAL HISTORY DIGGER ───────────────────────────────────────────────

SECRET_PATTERNS = [
    (r'(?:AKIA|ASIA)[0-9A-Z]{16}',                          "AWS Access Key"),
    (r'(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}',          "GitHub Token"),
    (r'github_pat_[A-Za-z0-9_]{22,}',                        "GitHub PAT (fine-grained)"),
    (r'sk-[A-Za-z0-9]{32,}',                                 "OpenAI / Stripe Secret Key"),
    (r'sk-ant-[A-Za-z0-9\-]{40,}',                           "Anthropic API Key"),
    (r'xox[bpras]-[A-Za-z0-9\-]{10,}',                       "Slack Token"),
    (r'AIza[0-9A-Za-z_\-]{35}',                              "Google API Key"),
    (r'ya29\.[0-9A-Za-z_\-]+',                                "Google OAuth Token"),
    (r'SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}',         "SendGrid API Key"),
    (r'(?:password|passwd|pwd|secret|token|api_key|apikey)\s*[:=]\s*["\'][^"\']{8,}["\']', "Hardcoded Secret"),
    (r'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----',        "Private Key"),
    (r'-----BEGIN OPENSSH PRIVATE KEY-----',                   "OpenSSH Private Key"),
    (r'(?:mysql|postgres|postgresql|mongodb|redis)://\S+:\S+@', "Database Connection String"),
    (r'Bearer\s+[A-Za-z0-9\-_.~+/]{20,}',                    "Bearer Token"),
    (r'npm_[A-Za-z0-9]{36}',                                  "npm Token"),
    (r'pypi-[A-Za-z0-9]{60,}',                                "PyPI Token"),
    (r'PRIVATE KEY',                                           "Generic Private Key Marker"),
    (r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.',         "JWT Token"),
]

_compiled_patterns = [(re.compile(pat), label) for pat, label in SECRET_PATTERNS]

IGNORE_PATHS = {
    ".env.example", ".env.sample", ".env.template",
    "test_", "tests/", "mock", "fixture", "example",
}


def _should_ignore_path(filepath: str) -> bool:
    fp = filepath.lower()
    return any(tok in fp for tok in IGNORE_PATHS)


def _scan_text_for_secrets(text: str, source_label: str, filepath: str = "") -> list[dict]:
    if _should_ignore_path(filepath):
        return []
    findings = []
    seen = set()
    for regex, label in _compiled_patterns:
        for m in regex.finditer(text):
            matched = m.group(0)
            snippet = matched[:12] + "..." + matched[-4:] if len(matched) > 20 else matched[:8] + "..."
            key = (label, snippet)
            if key not in seen:
                seen.add(key)
                findings.append({
                    "type": label,
                    "snippet": snippet,
                    "source": source_label,
                    "file": filepath,
                })
    return findings


def dig_local_history(repo_path: str, max_commits: int = 200) -> dict:
    """Scan local git history for deleted/reverted credentials using git log -p."""
    import subprocess

    result = {
        "scanned_commits": 0,
        "findings": [],
        "severity": "clean",
        "summary": "",
    }

    git_dir = os.path.join(repo_path, ".git")
    if not os.path.isdir(git_dir):
        result["summary"] = "Not a git repository — history scan skipped"
        return result

    try:
        proc = subprocess.run(
            ["git", "log", f"--max-count={max_commits}", "-p", "--diff-filter=D",
             "--no-color", "--format=commit %H %ai"],
            capture_output=True, text=True, timeout=30,
            cwd=repo_path, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        deleted_diff = proc.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        deleted_diff = ""

    try:
        proc2 = subprocess.run(
            ["git", "log", f"--max-count={max_commits}", "-p",
             "--no-color", "--format=commit %H %ai",
             "-S", "password", "-S", "secret", "-S", "token", "-S", "PRIVATE KEY",
             "--pickaxe-all"],
            capture_output=True, text=True, timeout=30,
            cwd=repo_path, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        keyword_diff = proc2.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        keyword_diff = ""

    try:
        count_proc = subprocess.run(
            ["git", "rev-list", "--count", f"--max-count={max_commits}", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=repo_path,
        )
        result["scanned_commits"] = int(count_proc.stdout.strip()) if count_proc.stdout.strip() else 0
    except Exception as exc:
        logger.debug("Could not count git commits: %s", exc)

    current_commit = "unknown"
    current_file = ""
    for raw_block in [deleted_diff, keyword_diff]:
        for line in raw_block.splitlines():
            if line.startswith("commit "):
                current_commit = line.split()[1][:8] if len(line.split()) > 1 else "unknown"
                current_file = ""
            elif line.startswith("diff --git"):
                parts = line.split(" b/")
                current_file = parts[-1] if len(parts) > 1 else ""
            elif line.startswith("+") or line.startswith("-"):
                if line.startswith("+++") or line.startswith("---"):
                    continue
                findings = _scan_text_for_secrets(
                    line, f"commit {current_commit}", current_file
                )
                result["findings"].extend(findings)

    seen_keys = set()
    unique = []
    for f in result["findings"]:
        key = (f["type"], f["snippet"], f["file"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(f)
    result["findings"] = unique[:25]

    count = len(result["findings"])
    if count == 0:
        result["severity"] = "clean"
        result["summary"] = f"No leaked credentials found in {result['scanned_commits']} commits"
    elif count <= 2:
        result["severity"] = "warning"
        result["summary"] = f"{count} potential credential leak(s) found in commit history"
    else:
        result["severity"] = "critical"
        result["summary"] = f"{count} credential leaks found in commit history — rotate immediately"

    return result


def dig_github_history(repo: str, max_commits: int = 30) -> dict:
    """Scan GitHub repo commit diffs for deleted/reverted credentials via API."""
    result = {
        "scanned_commits": 0,
        "findings": [],
        "severity": "clean",
        "summary": "",
    }

    if not HAS_HTTPX:
        result["summary"] = "httpx not installed — GitHub history scan skipped"
        return result

    token = os.environ.get("GITHUB_TOKEN", "")
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    client = httpx.Client(headers=headers, timeout=15)

    try:
        r = client.get(f"{base}/repos/{repo}/commits", params={"per_page": max_commits})
        if r.status_code != 200:
            result["summary"] = f"Could not fetch commits (HTTP {r.status_code})"
            return result
        commits = r.json()
        result["scanned_commits"] = len(commits)
    except Exception as exc:
        logger.warning("Failed to fetch commit list: %s", exc)
        result["summary"] = "Failed to fetch commit list"
        return result

    for commit in commits:
        sha = commit["sha"]
        try:
            r2 = client.get(f"{base}/repos/{repo}/commits/{sha}")
            if r2.status_code != 200:
                continue
            data = r2.json()
            for file_info in data.get("files", []):
                patch = file_info.get("patch", "")
                filepath = file_info.get("filename", "")
                if file_info.get("status") in ("removed", "modified") and patch:
                    for line in patch.splitlines():
                        if line.startswith("-") and not line.startswith("---"):
                            findings = _scan_text_for_secrets(
                                line, f"commit {sha[:8]}", filepath
                            )
                            result["findings"].extend(findings)
        except Exception as exc:
            logger.debug("Skipping commit %s: %s", sha[:8], exc)
            continue

    seen_keys = set()
    unique = []
    for f in result["findings"]:
        key = (f["type"], f["snippet"], f["file"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(f)
    result["findings"] = unique[:25]

    count = len(result["findings"])
    if count == 0:
        result["severity"] = "clean"
        result["summary"] = f"No leaked credentials found in {result['scanned_commits']} commits"
    elif count <= 2:
        result["severity"] = "warning"
        result["summary"] = f"{count} potential credential leak(s) found in commit history"
    else:
        result["severity"] = "critical"
        result["summary"] = f"{count} credential leaks found in commit history — rotate immediately"

    return result


def dig_current_files(reader) -> list[dict]:
    """Scan current repo files for live secrets (not just history)."""
    findings = []
    sensitive_globs = [
        ".env", "*.env", ".env.*",
        "config.json", "credentials.json", "secrets.json",
        "*.pem", "*.key", "id_rsa", "id_ed25519",
    ]

    all_files = reader.list_files()
    for filepath in all_files:
        basename = os.path.basename(filepath).lower()
        is_sensitive_name = any(
            fnmatch.fnmatch(basename, pat) for pat in sensitive_globs
        )
        is_config = any(tok in filepath.lower() for tok in [
            "config", "settings", "credential", "secret", ".env",
            "docker-compose", "application.yml", "application.yaml",
        ])

        if is_sensitive_name or is_config:
            content = reader.read(filepath)
            if content:
                findings.extend(_scan_text_for_secrets(content, "current file", filepath))

    seen_keys = set()
    unique = []
    for f in findings:
        key = (f["type"], f["snippet"], f["file"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(f)
    return unique[:15]


def credential_scan(reader, repo_path: str = "", github_repo: str = "") -> dict:
    """Full credential scan: current files + commit history."""
    current = dig_current_files(reader)

    if repo_path:
        history = dig_local_history(repo_path)
    elif github_repo:
        history = dig_github_history(github_repo, max_commits=20)
    else:
        history = {"scanned_commits": 0, "findings": [], "severity": "clean", "summary": "No history source"}

    all_findings = current + history.get("findings", [])
    live_count = len(current)
    history_count = len(history.get("findings", []))
    total = len(all_findings)

    if total == 0:
        severity = "clean"
    elif live_count > 0:
        severity = "critical"
    elif history_count > 3:
        severity = "critical"
    elif history_count > 0:
        severity = "warning"
    else:
        severity = "clean"

    return {
        "total_findings": total,
        "live_secrets": live_count,
        "history_secrets": history_count,
        "scanned_commits": history.get("scanned_commits", 0),
        "severity": severity,
        "summary": (
            f"{live_count} live + {history_count} historical credential(s) detected"
            if total > 0
            else f"Clean — no credentials found ({history.get('scanned_commits', 0)} commits scanned)"
        ),
        "findings": all_findings[:25],
    }


# ─── SHARED EXCLUSIONS ───────────────────────────────────────────────────────

EXCLUDE_DIRS = {
    ".venv", "venv", "env", ".env", "node_modules", "__pycache__",
    ".git", ".tox", ".mypy_cache", ".pytest_cache", ".eggs",
    "dist", "build", "*.egg-info", ".nox", "htmlcov",
}


def _is_excluded(filepath: str) -> bool:
    parts = filepath.replace("\\", "/").split("/")
    return any(
        part in EXCLUDE_DIRS or part.endswith(".egg-info")
        for part in parts
    )


# ─── AI CONFIG FILE DETECTION (inspired by Medusa) ───────────────────────────
# Ref: https://github.com/Pantheon-Security/medusa — repo poisoning detection

AI_CONFIG_RISK_FILES = {
    ".cursorrules":                    {"risk": "critical", "tool": "Cursor AI",          "cve": "CVE-2025-54135", "desc": "Cursor rules — known RCE vector (CurXecute)"},
    ".cursor/mcp.json":                {"risk": "critical", "tool": "Cursor MCP",         "cve": "CVE-2025-54135", "desc": "Cursor MCP config — can execute arbitrary tools"},
    ".clinerules":                     {"risk": "critical", "tool": "Cline",              "cve": None,             "desc": "Cline rules — Clinejection attack vector"},
    ".windsurfrules":                  {"risk": "critical", "tool": "Windsurf",           "cve": "CVE-2025-36730", "desc": "Windsurf rules — known RCE vector"},
    ".codex/config.toml":              {"risk": "critical", "tool": "Codex CLI",          "cve": "CVE-2025-61260", "desc": "Codex CLI config — known RCE vector"},
    ".kiro/settings/mcp.json":         {"risk": "critical", "tool": "Kiro",               "cve": "CVE-2026-0830",  "desc": "Kiro MCP config — known RCE vector"},
    ".vscode/settings.json":           {"risk": "high",     "tool": "VS Code",            "cve": None,             "desc": "VS Code settings — can configure terminal, tasks"},
    "mcp.json":                        {"risk": "high",     "tool": "MCP",                "cve": None,             "desc": "MCP server config — tool poisoning risk"},
    ".mcp.json":                       {"risk": "high",     "tool": "MCP",                "cve": None,             "desc": "MCP server config — tool poisoning risk"},
    "CLAUDE.md":                       {"risk": "high",     "tool": "Claude Code",        "cve": None,             "desc": "Claude Code instructions — prompt injection surface"},
    "GEMINI.md":                       {"risk": "high",     "tool": "Gemini CLI",         "cve": None,             "desc": "Gemini CLI instructions — prompt injection surface"},
    "AGENTS.md":                       {"risk": "high",     "tool": "OpenAI Codex",       "cve": None,             "desc": "Codex agent instructions — prompt injection surface"},
    ".github/copilot-instructions.md": {"risk": "medium",   "tool": "GitHub Copilot",     "cve": None,             "desc": "Copilot custom instructions"},
    "CONVENTIONS.md":                  {"risk": "medium",   "tool": "Aider",              "cve": None,             "desc": "Aider conventions — instruction injection surface"},
    "SKILL.md":                        {"risk": "medium",   "tool": "ClawHub",            "cve": None,             "desc": "Skill definition — ToxicSkills attack surface"},
}

AI_CONFIG_DIR_PATTERNS = [
    (".cursor/rules/",    "critical", "Cursor rules directory"),
    (".clinerules/",      "critical", "Cline rules directory"),
    (".windsurf/rules/",  "critical", "Windsurf rules directory"),
    (".amazonq/rules/",   "medium",  "Amazon Q rules directory"),
    (".augment/rules/",   "medium",  "Augment Code rules"),
    (".roo/rules/",       "medium",  "Roo Code rules"),
]


def detect_ai_config_files(reader) -> dict:
    """Detect AI editor config files that could be repo poisoning vectors."""
    all_files = [f for f in reader.list_files() if not _is_excluded(f)]
    findings = []

    for filepath in all_files:
        normalized = filepath.replace("\\", "/")

        if normalized in AI_CONFIG_RISK_FILES:
            info = AI_CONFIG_RISK_FILES[normalized]
            findings.append({
                "file": filepath,
                "risk": info["risk"],
                "tool": info["tool"],
                "cve": info["cve"],
                "description": info["desc"],
            })
            continue

        basename = os.path.basename(normalized)
        if basename in AI_CONFIG_RISK_FILES:
            info = AI_CONFIG_RISK_FILES[basename]
            findings.append({
                "file": filepath,
                "risk": info["risk"],
                "tool": info["tool"],
                "cve": info["cve"],
                "description": info["desc"],
            })
            continue

        for dir_pat, risk, desc in AI_CONFIG_DIR_PATTERNS:
            if dir_pat in normalized:
                findings.append({
                    "file": filepath,
                    "risk": risk,
                    "tool": desc.split(" ")[0],
                    "cve": None,
                    "description": desc,
                })
                break

    risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: risk_order.get(f["risk"], 9))

    critical = sum(1 for f in findings if f["risk"] == "critical")
    high = sum(1 for f in findings if f["risk"] == "high")

    if critical > 0:
        severity = "critical"
    elif high > 0:
        severity = "warning"
    elif findings:
        severity = "info"
    else:
        severity = "clean"

    return {
        "total": len(findings),
        "critical": critical,
        "high": high,
        "severity": severity,
        "findings": findings,
        "summary": (
            f"{len(findings)} AI config file(s) detected ({critical} critical, {high} high)"
            if findings else "No AI editor config files detected"
        ),
    }


# ─── REPO MAP GENERATOR ─────────────────────────────────────────────────────

LANG_EXTENSIONS = {
    ".py":     "Python",
    ".js":     "JavaScript",
    ".ts":     "TypeScript",
    ".jsx":    "React JSX",
    ".tsx":    "React TSX",
    ".java":   "Java",
    ".go":     "Go",
    ".rs":     "Rust",
    ".rb":     "Ruby",
    ".php":    "PHP",
    ".c":      "C",
    ".cpp":    "C++",
    ".cs":     "C#",
    ".swift":  "Swift",
    ".kt":     "Kotlin",
    ".r":      "R",
    ".R":      "R",
    ".jl":     "Julia",
    ".m":      "MATLAB",
    ".f90":    "Fortran",
    ".f":      "Fortran",
    ".sh":     "Shell",
    ".bash":   "Shell",
    ".zsh":    "Shell",
    ".ps1":    "PowerShell",
    ".sql":    "SQL",
    ".html":   "HTML",
    ".css":    "CSS",
    ".scss":   "SCSS",
    ".vue":    "Vue",
    ".svelte": "Svelte",
    ".json":   "JSON",
    ".yml":    "YAML",
    ".yaml":   "YAML",
    ".toml":   "TOML",
    ".xml":    "XML",
    ".md":     "Markdown",
    ".rst":    "reStructuredText",
    ".txt":    "Text",
    ".ipynb":  "Jupyter Notebook",
    ".dockerfile": "Dockerfile",
    ".tf":     "Terraform",
    ".proto":  "Protobuf",
    ".graphql":"GraphQL",
    ".sol":    "Solidity",
    ".lua":    "Lua",
    ".pl":     "Perl",
    ".ex":     "Elixir",
    ".erl":    "Erlang",
    ".hs":     "Haskell",
    ".scala":  "Scala",
    ".dart":   "Dart",
}

KEY_FILES = {
    "README.md":         "Documentation",
    "README.rst":        "Documentation",
    "LICENSE":           "License",
    "LICENSE.md":        "License",
    "Makefile":          "Build system",
    "Dockerfile":        "Container",
    "docker-compose.yml":"Container orchestration",
    "docker-compose.yaml":"Container orchestration",
    ".gitignore":        "Git config",
    ".github":           "GitHub config",
    "requirements.txt":  "Python dependencies",
    "pyproject.toml":    "Python project config",
    "setup.py":          "Python packaging",
    "setup.cfg":         "Python packaging",
    "Pipfile":           "Python dependencies",
    "package.json":      "Node.js project",
    "yarn.lock":         "Yarn lockfile",
    "package-lock.json": "npm lockfile",
    "go.mod":            "Go modules",
    "Cargo.toml":        "Rust project",
    "pom.xml":           "Maven (Java)",
    "build.gradle":      "Gradle (Java)",
    "Gemfile":           "Ruby dependencies",
    ".env":              "Environment config",
    ".env.example":      "Environment template",
    "dvc.yaml":          "DVC pipeline",
    "Snakefile":         "Snakemake workflow",
    "MLproject":         "MLflow project",
    "wandb":             "Weights & Biases",
    ".scigate":          "SciGate config",
    "action.yml":        "GitHub Action",
    ".gitlab-ci.yml":    "GitLab CI",
    "Jenkinsfile":       "Jenkins pipeline",
    "tox.ini":           "Tox config",
    "pytest.ini":        "Pytest config",
    ".pre-commit-config.yaml": "Pre-commit hooks",
    "CITATION.cff":      "Citation file",
    "CONTRIBUTING.md":   "Contributing guide",
    "CHANGELOG.md":      "Changelog",
}


def generate_repo_map(reader) -> dict:
    """Generate a comprehensive map of a repository's structure."""
    all_files = [f for f in reader.list_files() if not _is_excluded(f)]

    lang_counts: dict[str, int] = {}
    lang_files: dict[str, list[str]] = {}
    dir_tree: dict[str, list[str]] = {}
    key_files_found = []
    total_size_estimate = 0

    for filepath in all_files:
        parts = filepath.replace("\\", "/").split("/")
        basename = parts[-1]
        ext = ""
        if "." in basename:
            ext = "." + basename.rsplit(".", 1)[-1].lower()
        elif basename == "Dockerfile":
            ext = ".dockerfile"
        elif basename == "Makefile":
            ext = ".makefile"

        lang = LANG_EXTENSIONS.get(ext, "")
        if lang:
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
            if lang not in lang_files:
                lang_files[lang] = []
            if len(lang_files[lang]) < 5:
                lang_files[lang].append(filepath)

        dir_key = "/".join(parts[:-1]) if len(parts) > 1 else "."
        if dir_key not in dir_tree:
            dir_tree[dir_key] = []
        dir_tree[dir_key].append(basename)

        if basename in KEY_FILES:
            key_files_found.append({
                "file": filepath,
                "role": KEY_FILES[basename],
            })
        elif filepath in KEY_FILES:
            key_files_found.append({
                "file": filepath,
                "role": KEY_FILES[filepath],
            })

    top_dirs = sorted(dir_tree.keys())
    tree_lines = []
    root_files = dir_tree.get(".", [])

    for d in top_dirs:
        if d == ".":
            continue
        file_count = len(dir_tree[d])
        depth = d.count("/")
        indent = "  " * depth
        dirname = d.split("/")[-1]
        tree_lines.append(f"{indent}{dirname}/ ({file_count} files)")

    lang_sorted = sorted(lang_counts.items(), key=lambda x: -x[1])
    total_typed = sum(c for _, c in lang_sorted)

    lang_breakdown = []
    for lang, count in lang_sorted[:12]:
        pct = round(count / max(total_typed, 1) * 100, 1)
        lang_breakdown.append({
            "language": lang,
            "files": count,
            "percentage": pct,
            "sample_files": lang_files.get(lang, [])[:3],
        })

    primary_lang = lang_sorted[0][0] if lang_sorted else "Unknown"

    dir_summary = []
    for d in sorted(dir_tree.keys()):
        if d == ".":
            continue
        depth = d.count("/")
        if depth > 1:
            continue
        sub_count = sum(
            len(dir_tree[sd]) for sd in dir_tree if sd.startswith(d)
        )
        dir_summary.append({
            "path": d,
            "files": sub_count,
            "children": [
                sd.split("/")[-1] for sd in top_dirs
                if sd.startswith(d + "/") and sd.count("/") == depth + 1
            ][:8],
        })
    dir_summary.sort(key=lambda x: -x["files"])

    ai_configs = detect_ai_config_files(reader)

    return {
        "total_files": len(all_files),
        "total_directories": len(dir_tree),
        "primary_language": primary_lang,
        "languages": lang_breakdown,
        "key_files": key_files_found,
        "directories": dir_summary[:20],
        "root_files": sorted(root_files)[:30],
        "tree_text": "\n".join(tree_lines[:60]),
        "ai_config": ai_configs,
    }


# ─── CONVENIENCE FUNCTIONS ───────────────────────────────────────────────────

def get_activity(repo: str, limit: int = 10) -> dict:
    if not HAS_HTTPX:
        return {"error": "httpx not installed"}
    tracker = GitHubTracker(repo)
    return tracker.activity_summary(limit)


