"""
SciGate — FastAPI Server v2.1
─────────────────────────────
Connects the SciGate dashboard UI to the full agent pipeline.

Routes (all under /v1 prefix, also mirrored at root for backward compat):
    POST /v1/scan               — audit a local or GitHub repo
    GET  /v1/activity           — recent PRs, commits from GitHub
    GET  /v1/ci/{provider}/{job} — CI job status (jenkins|woodpecker|gha)
    POST /v1/dependencies       — dependency health analysis
    GET  /v1/leaderboard        — org memory leaderboard + patterns
    GET  /v1/policy/{tenant}    — repo policy
    POST /v1/webhooks/github    — GitHub webhook receiver
    GET  /health                — service health check

Start:
    uvicorn api.server:app --reload --port 8000

Environment variables:
    ANTHROPIC_API_KEY   — for Agent 2 fix generation
    GITHUB_TOKEN        — for remote repo scanning
    VCS_PROVIDER        — github | gitea (default: github)
    CI_PROVIDER         — jenkins | woodpecker | gha (default: jenkins)
    JENKINS_URL         — Jenkins base URL (optional)
    JENKINS_USER        — Jenkins username (optional)
    JENKINS_TOKEN       — Jenkins API token (optional)
    SCIGATE_THRESHOLD   — default: 75
    SCIGATE_MEMORY_DIR  — default: ./memory
    SCIGATE_NOTIFY_CHANNELS — comma-sep: ntfy,mattermost
"""

import os
import sys
import json
import logging
import tempfile
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Literal

_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.audit_agent import RepoReader, audit
from agents.memory_agent import run as memory_run, leaderboard_summary, top_patterns
from agents.tracker import get_activity, validate_dependencies

# ─── STRUCTURED LOGGING ──────────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "agent": "server",
            "msg": record.getMessage(),
        }
        if hasattr(record, "scan_id"):
            entry["scan_id"] = record.scan_id
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
        return json.dumps(entry)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger = logging.getLogger("scigate")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# ─── ALLOWED LOCAL PATHS ─────────────────────────────────────────────────────

ALLOWED_ROOTS_RAW = os.environ.get("SCIGATE_ALLOWED_ROOTS", "")
ALLOWED_ROOTS = [
    Path(p.strip()).resolve() for p in ALLOWED_ROOTS_RAW.split(",") if p.strip()
] if ALLOWED_ROOTS_RAW else []


def validate_local_path(raw_path: str) -> Path:
    """Resolve and jail local_path to allowed root directories."""
    resolved = Path(raw_path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(status_code=404, detail=f"Local path not found: {raw_path}")
    if ALLOWED_ROOTS:
        if not any(resolved == root or root in resolved.parents for root in ALLOWED_ROOTS):
            raise HTTPException(
                status_code=403,
                detail=f"Path outside allowed roots. Set SCIGATE_ALLOWED_ROOTS env var.",
            )
    return resolved

# ─── APP ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SciGate API",
    description="Scientific Reproducibility Intelligence Platform — 100% open source",
    version="2.1.0",
)

# Rate limiting
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    logger.info("Rate limiting enabled (60/min per IP)")
except ImportError:
    logger.info("slowapi not installed — rate limiting disabled (pip install slowapi)")

CORS_ORIGINS = os.environ.get("SCIGATE_CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"


# ─── REQUEST / RESPONSE MODELS ────────────────────────────────────────────────

class ScanRequest(BaseModel):
    local_path:      Optional[str] = Field(None, description="Absolute local path to repo")
    github_repo:     Optional[str] = Field(None, description="GitHub repo (owner/repo)")
    gitea_repo:      Optional[str] = Field(None, description="Gitea repo (owner/repo)")
    ref:             str            = Field("main",    description="Git ref to scan")
    commit_sha:      str            = Field("unknown", description="Commit SHA for tracking")
    trigger:         Literal["push", "pr", "tag", "slash_command", "schedule", "api"] = "api"
    run_fix_agent:   bool           = Field(False, description="Trigger Agent 2 after audit")
    repo_name:       Optional[str]  = Field(None,  description="Override repo display name")
    async_mode:      bool           = Field(False, description="Return scan_id immediately, poll for results")


class ScanResponse(BaseModel):
    domain:        str
    scores:        dict
    grade:         str
    commit_sha:    str
    trigger:       str
    fixes:         list
    gate_blocked:  bool
    gate_threshold: int
    scan_duration_ms: int
    projected_score:  Optional[int] = None
    projected_grade:  Optional[str] = None
    total_effort_label: Optional[str] = None
    total_effort_minutes: Optional[int] = None
    fix_pr_url:    Optional[str] = None
    memory:        Optional[dict] = None
    regression:    Optional[dict] = None
    credentials:   Optional[dict] = None
    repo_map:      Optional[dict] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    agents: dict
    infrastructure: dict


class LeaderboardResponse(BaseModel):
    leaderboard: list
    top_patterns: list


# ─── ASYNC SCAN STORE ─────────────────────────────────────────────────────────

_scan_store: dict[str, dict] = {}
_scan_lock = threading.Lock()
MAX_SCAN_STORE = 200


def _set_scan_status(scan_id: str, status: str, **kwargs):
    with _scan_lock:
        if scan_id not in _scan_store:
            _scan_store[scan_id] = {"scan_id": scan_id}
        _scan_store[scan_id].update(status=status, updated_at=datetime.now(timezone.utc).isoformat(), **kwargs)


def _prune_scan_store():
    with _scan_lock:
        if len(_scan_store) > MAX_SCAN_STORE:
            ids = sorted(_scan_store, key=lambda k: _scan_store[k].get("updated_at", ""))
            for old_id in ids[:len(ids) - MAX_SCAN_STORE]:
                del _scan_store[old_id]


# ─── HEALTH ──────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
def health():
    return {
        "status":  "ok",
        "version": "2.1.0",
        "agents": {
            "audit":      "ready",
            "fix":        "ready" if os.environ.get("ANTHROPIC_API_KEY") else "no_api_key",
            "memory":     "ready",
            "regression": "ready",
            "notify":     "ready" if os.environ.get("SCIGATE_NOTIFY_CHANNELS") else "no_channels",
            "tracker":    "ready",
        },
        "infrastructure": {
            "vcs":     os.environ.get("VCS_PROVIDER", "github"),
            "ci":      os.environ.get("CI_PROVIDER", "jenkins"),
            "jenkins": "configured" if os.environ.get("JENKINS_URL") else "not_configured",
        },
    }


# ─── SCAN ─────────────────────────────────────────────────────────────────────

def _run_scan_pipeline(req: ScanRequest, scan_id: str | None = None) -> dict:
    """Core scan pipeline shared by sync and async modes."""
    if req.local_path:
        path = validate_local_path(req.local_path)
        reader = RepoReader(mode="local", path=str(path))
        repo_name = req.repo_name or path.name
    else:
        remote_repo = req.github_repo or req.gitea_repo
        reader = RepoReader(mode="github", repo=remote_repo, ref=req.ref)
        repo_name = req.repo_name or remote_repo

    if scan_id:
        _set_scan_status(scan_id, "auditing", progress=10, repo=repo_name)

    try:
        score = audit(reader, commit_sha=req.commit_sha, trigger=req.trigger)
    except Exception as exc:
        msg = str(exc)
        if "403" in msg and "rate limit" in msg.lower():
            raise HTTPException(
                status_code=429,
                detail="GitHub API rate limit exceeded. Set GITHUB_TOKEN env var or wait ~1 hour.",
            )
        if "404" in msg:
            raise HTTPException(
                status_code=404,
                detail=f"Repository not found: {req.github_repo or req.gitea_repo or req.local_path}",
            )
        raise HTTPException(status_code=500, detail=f"Scan failed: {msg[:200]}")

    if scan_id:
        _set_scan_status(scan_id, "post_processing", progress=60, score=score["scores"]["total"])

    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    tmp.write(json.dumps(score))
    tmp.close()
    score_path = Path(tmp.name)

    fix_pr_url: Optional[str] = None

    if req.run_fix_agent and os.environ.get("ANTHROPIC_API_KEY"):
        gh_repo = req.github_repo or req.gitea_repo or ""
        if gh_repo:
            try:
                from agents.fix_agent import run as fix_run
                fix_result = fix_run(score, gh_repo)
                fix_pr_url = fix_result.get("pr_url")
            except Exception as exc:
                logger.warning("Fix agent failed (non-fatal): %s", exc)
            finally:
                score_path.unlink(missing_ok=True)

    if scan_id:
        _set_scan_status(scan_id, "regression_check", progress=75)

    regression_result = None
    try:
        from agents.regression_agent import check_regression
        reg = check_regression(score, repo_name)
        regression_result = reg.to_dict()
    except Exception as exc:
        logger.warning("Regression check failed (non-fatal): %s", exc)

    if scan_id:
        _set_scan_status(scan_id, "memory", progress=85)

    mem_result = None
    try:
        mem_result = memory_run(score, repo_name)
    except Exception as exc:
        logger.warning("Memory agent failed (non-fatal): %s", exc)

    cred_result = None
    try:
        from agents.tracker import credential_scan
        local = str(Path(req.local_path).resolve()) if req.local_path else ""
        gh = req.github_repo or req.gitea_repo or ""
        cred_result = credential_scan(reader, repo_path=local, github_repo=gh)
    except Exception as exc:
        logger.warning("Credential scan failed (non-fatal): %s", exc)

    map_result = None
    try:
        from agents.tracker import generate_repo_map
        map_result = generate_repo_map(reader)
    except Exception as exc:
        logger.warning("Repo map failed (non-fatal): %s", exc)

    try:
        from agents.notify_agent import notify
        notify(score, repo_name, fix_pr_url)
    except Exception as exc:
        logger.warning("Notify failed (non-fatal): %s", exc)

    result = {k: v for k, v in score.items() if not k.startswith("_")}
    return {
        **result,
        "fix_pr_url":  fix_pr_url,
        "memory":      mem_result,
        "regression":  regression_result,
        "credentials": cred_result,
        "repo_map":    map_result,
    }


def _run_async_scan(req: ScanRequest, scan_id: str):
    """Background worker for async scans."""
    try:
        result = _run_scan_pipeline(req, scan_id=scan_id)
        _set_scan_status(scan_id, "completed", progress=100, result=result)
    except HTTPException as exc:
        _set_scan_status(scan_id, "failed", progress=100, error=exc.detail)
    except Exception as exc:
        _set_scan_status(scan_id, "failed", progress=100, error=str(exc)[:300])
    _prune_scan_store()


@app.post("/v1/scan")
def scan(req: ScanRequest, request: Request, background_tasks: BackgroundTasks):
    if not req.local_path and not req.github_repo and not req.gitea_repo:
        raise HTTPException(
            status_code=422,
            detail="Provide local_path, github_repo, or gitea_repo",
        )

    if req.async_mode:
        scan_id = str(uuid.uuid4())
        _set_scan_status(scan_id, "queued", progress=0,
                         repo=req.github_repo or req.gitea_repo or req.local_path)
        thread = threading.Thread(target=_run_async_scan, args=(req, scan_id), daemon=True)
        thread.start()
        return {"scan_id": scan_id, "status": "queued", "poll_url": f"/v1/scan/{scan_id}"}

    result = _run_scan_pipeline(req)
    return result


@app.get("/v1/scan/{scan_id}")
def get_scan_status(scan_id: str):
    """Poll the status of an async scan."""
    with _scan_lock:
        entry = _scan_store.get(scan_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Scan not found")
    return entry


# ─── WEBSOCKET PROGRESS ──────────────────────────────────────────────────────

try:
    from fastapi import WebSocket, WebSocketDisconnect

    @app.websocket("/ws/scan/{scan_id}")
    async def ws_scan_progress(websocket: WebSocket, scan_id: str):
        """Stream scan progress updates over WebSocket."""
        await websocket.accept()
        import asyncio
        last_status = None
        try:
            for _ in range(300):  # 5 min max
                with _scan_lock:
                    entry = _scan_store.get(scan_id)
                if not entry:
                    await websocket.send_json({"error": "scan_not_found"})
                    break
                if entry.get("status") != last_status:
                    await websocket.send_json(entry)
                    last_status = entry.get("status")
                if last_status in ("completed", "failed"):
                    break
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass
except ImportError:
    pass


# ─── LEADERBOARD & HISTORY ──────────────────────────────────────────────────

@app.get("/v1/leaderboard", response_model=LeaderboardResponse)
def leaderboard():
    return {
        "leaderboard":  leaderboard_summary(20),
        "top_patterns": top_patterns(10),
    }


@app.get("/v1/repo/{repo_slug}/history")
def repo_history(repo_slug: str):
    from agents.memory_agent import MEMORY_DIR
    path = MEMORY_DIR / "scans" / f"{repo_slug}.jsonl"
    if not path.exists():
        return {"history": []}
    records = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {"history": sorted(records, key=lambda r: r.get("ts", ""), reverse=True)}


# ─── ACTIVITY (PRs, Commits, Code Changes) ───────────────────────────────────

@app.get("/v1/activity/{owner}/{repo}")
def activity(owner: str, repo: str, limit: int = 10):
    full_repo = f"{owner}/{repo}"
    try:
        return get_activity(full_repo, limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Activity fetch failed: {exc}")


@app.get("/v1/activity/{owner}/{repo}/commits")
def activity_commits(owner: str, repo: str, limit: int = 15, branch: str = "main"):
    from agents.tracker import GitHubTracker
    tracker = GitHubTracker(f"{owner}/{repo}")
    return {"commits": tracker.recent_commits(limit, branch)}


@app.get("/v1/activity/{owner}/{repo}/prs")
def activity_prs(owner: str, repo: str, limit: int = 10):
    from agents.tracker import GitHubTracker
    tracker = GitHubTracker(f"{owner}/{repo}")
    return {"pull_requests": tracker.recent_prs(limit)}


@app.get("/v1/activity/{owner}/{repo}/diff/{sha}")
def activity_diff(owner: str, repo: str, sha: str):
    from agents.tracker import GitHubTracker
    tracker = GitHubTracker(f"{owner}/{repo}")
    return tracker.commit_diff(sha)


@app.get("/v1/activity/{owner}/{repo}/compare/{base}/{head}")
def activity_compare(owner: str, repo: str, base: str, head: str):
    from agents.tracker import GitHubTracker
    tracker = GitHubTracker(f"{owner}/{repo}")
    return tracker.compare(base, head)


# ─── CI (multi-provider) ─────────────────────────────────────────────────────

@app.get("/v1/ci/{provider}/{job_name}")
def ci_status(provider: str, job_name: str):
    try:
        from integrations.ci import get_ci_adapter
        adapter = get_ci_adapter(provider)
        status = adapter.get_job_status(job_name)
        return {
            "name": status.name, "status": status.status,
            "configured": status.configured,
            "last_build": status.last_build, "error": status.error,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/v1/ci/{provider}/{job_name}/builds")
def ci_builds(provider: str, job_name: str, limit: int = 10):
    try:
        from integrations.ci import get_ci_adapter
        adapter = get_ci_adapter(provider)
        return {"builds": adapter.get_build_history(job_name, limit)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))



# ─── DEPENDENCY VALIDATION ───────────────────────────────────────────────────

class DepsRequest(BaseModel):
    local_path:  Optional[str] = None
    github_repo: Optional[str] = None
    ref:         str = "main"


@app.post("/v1/dependencies")
def dependencies(req: DepsRequest):
    if not req.local_path and not req.github_repo:
        raise HTTPException(status_code=422, detail="Provide local_path or github_repo")

    if req.local_path:
        path = Path(req.local_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Path not found")
        reader = RepoReader(mode="local", path=str(path))
    else:
        reader = RepoReader(mode="github", repo=req.github_repo, ref=req.ref)

    return validate_dependencies(reader)


# ─── CREDENTIAL HISTORY SCAN ─────────────────────────────────────────────────

@app.post("/v1/credentials")
def credentials_endpoint(req: DepsRequest):
    """Scan current files and commit history for leaked credentials."""
    from agents.tracker import credential_scan

    if not req.local_path and not req.github_repo:
        raise HTTPException(status_code=422, detail="Provide local_path or github_repo")

    if req.local_path:
        path = Path(req.local_path).expanduser().resolve()
        if not path.exists():
            raise HTTPException(status_code=404, detail="Path not found")
        reader = RepoReader(mode="local", path=str(path))
        return credential_scan(reader, repo_path=str(path))
    else:
        reader = RepoReader(mode="github", repo=req.github_repo, ref=req.ref)
        return credential_scan(reader, github_repo=req.github_repo)


# ─── REPO MAP ────────────────────────────────────────────────────────────────

@app.post("/v1/repo-map")
def repo_map_endpoint(req: DepsRequest):
    """Generate a comprehensive structural map of a repository."""
    from agents.tracker import generate_repo_map

    if not req.local_path and not req.github_repo:
        raise HTTPException(status_code=422, detail="Provide local_path or github_repo")

    if req.local_path:
        path = Path(req.local_path).expanduser().resolve()
        if not path.exists():
            raise HTTPException(status_code=404, detail="Path not found")
        reader = RepoReader(mode="local", path=str(path))
    else:
        reader = RepoReader(mode="github", repo=req.github_repo, ref=req.ref)

    return generate_repo_map(reader)


# ─── POLICY ──────────────────────────────────────────────────────────────────

@app.get("/v1/policy/{tenant_id}")
def get_policy(tenant_id: str):
    try:
        from policy.loader import load_policy
        return load_policy(tenant_id)
    except Exception as exc:
        logger.warning("Policy load failed for %s, using defaults: %s", tenant_id, exc)
        return {
            "tenant_id": tenant_id,
            "gate_threshold": int(os.environ.get("SCIGATE_THRESHOLD", "75")),
            "regression_gate": False,
            "notify_channels": [c for c in os.environ.get("SCIGATE_NOTIFY_CHANNELS", "").split(",") if c],
        }


# ─── HELP ─────────────────────────────────────────────────────────────────────

@app.get("/v1/help")
def get_help():
    return {
        "app": "SciGate",
        "version": "2.1.0",
        "description": "Scientific Reproducibility Intelligence Platform",
        "agents": [
            {"name": "Audit",      "file": "agents/audit_agent.py",      "purpose": "Score 6 dimensions of reproducibility (0-100)"},
            {"name": "Fix",        "file": "agents/fix_agent.py",        "purpose": "Generate AI-authored minimal patches for deductions"},
            {"name": "Memory",     "file": "agents/memory_agent.py",     "purpose": "Persist scans, track patterns, maintain leaderboard"},
            {"name": "Regression", "file": "agents/regression_agent.py", "purpose": "Detect score regressions across scan history"},
            {"name": "Notify",     "file": "agents/notify_agent.py",     "purpose": "Fan-out alerts via VCS, ntfy, Mattermost, email"},
        ],
        "scoring": {
            "dimensions": {
                "environment":   {"max": 17, "checks": "Lockfile, pinned deps, Dockerfile, CUDA, Python version"},
                "seeds":         {"max": 17, "checks": "Random seeds, PYTHONHASHSEED, cudnn.deterministic"},
                "data":          {"max": 17, "checks": "Download scripts, checksums, DVC/LFS, no hardcoded paths"},
                "docs":          {"max": 17, "checks": "Run instructions, hardware reqs, expected outputs, citation"},
                "testing":       {"max": 17, "checks": "Test suite, coverage, smoke tests, shape assertions"},
                "compliance":    {"max": 15, "checks": "LICENSE file, dependency license conflicts, NOTICE files"},
            },
            "grades": {
                "EXCELLENT": "90-100 — auto-approve",
                "GOOD":      "75-89  — approve with suggestions",
                "FAIR":      "50-74  — block merge, draft PR opened",
                "POOR":      "25-49  — block merge, notify team lead",
                "CRITICAL":  "0-24   — block merge, escalation",
            },
        },
        "endpoints": [
            {"method": "POST", "path": "/v1/scan",                   "desc": "Scan a repo (sync or async_mode=true for background scan)"},
            {"method": "GET",  "path": "/v1/scan/{scan_id}",        "desc": "Poll async scan status and result"},
            {"method": "WS",   "path": "/ws/scan/{scan_id}",        "desc": "WebSocket stream for live scan progress"},
            {"method": "GET",  "path": "/v1/leaderboard",            "desc": "Org leaderboard and recurring patterns"},
            {"method": "GET",  "path": "/v1/repo/{repo_slug}/history",    "desc": "Scan history for a repo (slug: owner__repo)"},
            {"method": "POST", "path": "/v1/dependencies",           "desc": "Dependency health analysis"},
            {"method": "POST", "path": "/v1/credentials",            "desc": "Credential history scan (live + deleted secrets)"},
            {"method": "POST", "path": "/v1/repo-map",               "desc": "Repo structure map (languages, dirs, key files, AI configs)"},
            {"method": "GET",  "path": "/v1/activity/{owner}/{repo}", "desc": "PRs and commits from GitHub"},
            {"method": "POST", "path": "/v1/journal-check",          "desc": "Journal compliance check"},
            {"method": "GET",  "path": "/v1/certificate/{owner}/{repo}", "desc": "Reproducibility certificate (HTML)"},
            {"method": "GET",  "path": "/v1/badge/{owner}/{repo}",      "desc": "Dynamic shields.io badge (redirects with latest score)"},
            {"method": "GET",  "path": "/v1/ci/{provider}/{job}",    "desc": "CI job status (jenkins|woodpecker|gha)"},
            {"method": "GET",  "path": "/v1/policy/{tenant_id}",     "desc": "Repo policy config"},
            {"method": "POST", "path": "/v1/webhooks/github",        "desc": "GitHub webhook receiver"},
            {"method": "POST", "path": "/v1/webhooks/gitea",         "desc": "Gitea webhook receiver"},
            {"method": "GET",  "path": "/v1/help",                   "desc": "This help document"},
        ],
        "scan_input_formats": [
            {"format": "GitHub shorthand", "example": "owner/repo"},
            {"format": "GitHub URL",       "example": "https://github.com/owner/repo"},
            {"format": "Branch/ref",       "example": "owner/repo/tree/branch-name"},
            {"format": "Local path",       "example": "/path/to/local/repo"},
        ],
        "env_vars": [
            {"name": "GITHUB_TOKEN",        "desc": "GitHub PAT for remote repo scanning (avoids rate limits)"},
            {"name": "ANTHROPIC_API_KEY",   "desc": "Anthropic API key for AI-generated fixes"},
            {"name": "VCS_PROVIDER",        "desc": "github or gitea (default: github)"},
            {"name": "CI_PROVIDER",         "desc": "jenkins, woodpecker, or gha (default: jenkins)"},
            {"name": "SCIGATE_THRESHOLD",   "desc": "Gate threshold score (default: 75)"},
            {"name": "SCIGATE_MEMORY_DIR",  "desc": "Memory storage directory (default: ./memory)"},
        ],
        "keyboard_shortcuts": {
            "?": "Open / close help",
            "Esc": "Close any modal",
            "H": "Open scan history",
            "/": "Focus the scan input",
        },
    }


# ─── WEBHOOKS ────────────────────────────────────────────────────────────────

@app.post("/v1/webhooks/github")
async def webhook_github(request: Request, background_tasks: BackgroundTasks):
    event = request.headers.get("X-GitHub-Event", "")
    if event not in ("push", "pull_request"):
        return {"status": "ignored", "event": event}

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if secret:
        try:
            from integrations.vcs import get_vcs_adapter
            vcs = get_vcs_adapter()
            if not vcs.verify_webhook(body, sig, secret):
                raise HTTPException(status_code=401, detail="Invalid webhook signature")
        except ImportError:
            logger.warning("VCS adapter not available — skipping webhook signature verification")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Malformed JSON payload")
    repo = payload.get("repository", {}).get("full_name", "")

    if event == "pull_request":
        pr = payload.get("pull_request", {})
        action = payload.get("action", "")
        if action not in ("opened", "synchronize", "reopened"):
            return {"status": "ignored", "event": event, "action": action}
        ref = pr.get("head", {}).get("ref", "main")
        sha = pr.get("head", {}).get("sha", "unknown")
        trigger = "pr"
    else:
        ref = payload.get("ref", "main").split("/")[-1]
        sha = payload.get("after", payload.get("head_commit", {}).get("id", "unknown"))
        trigger = "push"

    if repo:
        background_tasks.add_task(
            _run_webhook_scan, repo=repo, ref=ref, sha=sha, trigger=trigger,
        )

    return {"status": "accepted", "event": event, "repo": repo, "ref": ref}


@app.post("/v1/webhooks/gitea")
async def webhook_gitea(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Malformed JSON payload")
    repo = payload.get("repository", {}).get("full_name", "")

    pr = payload.get("pull_request")
    if pr:
        action = payload.get("action", "")
        if action not in ("opened", "synchronized", "reopened"):
            return {"status": "ignored", "action": action}
        ref = pr.get("head", {}).get("ref", "main")
        sha = pr.get("head", {}).get("sha", "unknown")
        trigger = "pr"
    else:
        ref = payload.get("ref", "main").split("/")[-1]
        sha = payload.get("after", "unknown")
        trigger = "push"

    if repo:
        background_tasks.add_task(
            _run_webhook_scan, repo=repo, ref=ref, sha=sha, trigger=trigger,
        )

    return {"status": "accepted", "repo": repo, "ref": ref}


# ─── CERTIFICATE ─────────────────────────────────────────────────────────────

def _find_scan_history(owner: str, repo: str) -> Path:
    """Resolve the JSONL scan history file for owner/repo, using MEMORY_DIR."""
    from agents.memory_agent import MEMORY_DIR
    slug = f"{owner}__{repo}"
    path = MEMORY_DIR / "scans" / f"{slug}.jsonl"
    if not path.exists():
        slug_alt = f"{owner}/{repo}".replace("/", "__")
        path = MEMORY_DIR / "scans" / f"{slug_alt}.jsonl"
    return path


def _read_last_scan(owner: str, repo: str) -> dict:
    """Read the most recent scan record for a repo, or raise 404."""
    path = _find_scan_history(owner, repo)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No scan history found")
    try:
        lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read scan history: {exc}")
    if not lines:
        raise HTTPException(status_code=404, detail="No scans recorded")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupt scan history — last line is not valid JSON")


@app.get("/v1/certificate/{owner}/{repo}")
def get_certificate(owner: str, repo: str):
    """Generate an HTML reproducibility certificate for the last scan."""
    last = _read_last_scan(owner, repo)
    total = last.get("total", 0)
    grade = last.get("grade", "?")
    domain = last.get("domain", "?")
    ts = last.get("ts", "?")
    sha = last.get("commit_sha", "unknown")

    dim_map = {"env": 17, "seeds": 17, "data": 17, "docs": 17, "testing": 17, "compliance": 15}
    dims_html = ""
    for dim, mx in dim_map.items():
        val = last.get(dim, 0)
        status = "✓" if val >= mx * 0.8 else "△" if val >= mx * 0.4 else "✗"
        label = {"env": "Environment", "seeds": "Seeds & Determinism", "data": "Data Provenance",
                 "docs": "Documentation", "testing": "Testing", "compliance": "Compliance"}[dim]
        dims_html += f"<tr><td>{status}</td><td>{label}</td><td>{val}/{mx}</td></tr>\n"

    grade_color = {"EXCELLENT": "#22c55e", "GOOD": "#3b82f6", "FAIR": "#eab308",
                   "POOR": "#f97316", "CRITICAL": "#ef4444"}.get(grade, "#888")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>SciGate Certificate</title>
<style>
body {{ font-family: 'Georgia', serif; max-width: 700px; margin: 40px auto; padding: 40px;
       border: 3px double #333; background: #fafaf8; }}
h1 {{ text-align: center; font-size: 1.5em; letter-spacing: 2px; border-bottom: 2px solid #333;
     padding-bottom: 12px; }}
.meta {{ text-align: center; color: #555; margin: 16px 0; }}
.score {{ text-align: center; font-size: 3em; font-weight: bold; color: {grade_color}; margin: 20px 0; }}
.grade {{ text-align: center; font-size: 1.2em; color: {grade_color}; font-weight: bold; }}
table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #ddd; }}
td:first-child {{ width: 30px; text-align: center; font-size: 1.2em; }}
td:last-child {{ text-align: right; font-family: monospace; }}
.footer {{ text-align: center; color: #999; font-size: 0.85em; margin-top: 30px;
           border-top: 1px solid #ddd; padding-top: 12px; }}
@media print {{ body {{ border: none; }} }}
</style></head><body>
<h1>SCIGATE REPRODUCIBILITY CERTIFICATE</h1>
<div class="meta">Repository: <strong>{owner}/{repo}</strong> &nbsp;·&nbsp; Commit: <code>{sha[:8]}</code></div>
<div class="score">{total} / 100</div>
<div class="grade">{grade}</div>
<div class="meta">Domain: {domain} &nbsp;·&nbsp; Scanned: {ts[:10] if len(ts) > 10 else ts}</div>
<table>{dims_html}</table>
<div class="footer">
Verified by SciGate v2.1.0<br>
<a href="https://github.com/parthassamal/SciGate">scigate.dev</a>
</div></body></html>"""

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


# ─── DYNAMIC BADGE ───────────────────────────────────────────────────────────

@app.get("/v1/badge/{owner}/{repo}")
def get_badge(owner: str, repo: str, style: str = "for-the-badge"):
    """Redirect to a shields.io badge reflecting the latest scan score."""
    total, grade = 0, "UNKNOWN"
    try:
        last = _read_last_scan(owner, repo)
        total = last.get("total", 0)
        grade = last.get("grade", "UNKNOWN")
    except HTTPException:
        pass

    grade_colors = {
        "EXCELLENT": "brightgreen", "GOOD": "green",
        "FAIR": "yellow", "POOR": "orange", "CRITICAL": "red",
    }
    color = grade_colors.get(grade, "lightgrey")
    label = f"{total}%20%2F%20100%20{grade}"

    from fastapi.responses import RedirectResponse
    badge_url = (
        f"https://img.shields.io/badge/SciGate-{label}-{color}"
        f"?style={style}&labelColor=08090d"
    )
    return RedirectResponse(url=badge_url, status_code=302)


# ─── JOURNAL CHECKLIST ───────────────────────────────────────────────────────

@app.post("/v1/journal-check")
def journal_check_endpoint(req: ScanRequest, journal: str = "nature"):
    """Run audit and check against journal reproducibility requirements."""
    from agents.audit_agent import journal_checklist

    if req.local_path:
        path = Path(req.local_path).expanduser().resolve()
        if not path.exists():
            raise HTTPException(status_code=404, detail="Path not found")
        reader = RepoReader(mode="local", path=str(path))
    elif req.github_repo:
        reader = RepoReader(mode="github", repo=req.github_repo, ref=req.ref)
    else:
        raise HTTPException(status_code=400, detail="Provide local_path or github_repo")

    result = audit(reader, commit_sha=req.commit_sha, trigger=req.trigger)
    checklist = journal_checklist(result, journal)
    return {**result, "journal_checklist": checklist}


# ─── DASHBOARD SERVING ───────────────────────────────────────────────────────

@app.get("/")
def dashboard_index():
    html_path = DASHBOARD_DIR / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    return {"message": "Dashboard not found. Place index.html in dashboard/"}


if DASHBOARD_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")


# ─── BACKGROUND TASK HELPERS ─────────────────────────────────────────────────

def _run_webhook_scan(repo: str, ref: str, sha: str, trigger: str = "push") -> None:
    try:
        reader = RepoReader(mode="github", repo=repo, ref=ref)
        score = audit(reader, commit_sha=sha, trigger=trigger)
        total = score["scores"]["total"]
        grade = score["grade"]

        memory_run(score, repo)

        from agents.regression_agent import check_regression
        check_regression(score, repo)

        # Post commit status check so the result is visible on the PR
        try:
            from integrations.vcs import get_vcs_adapter
            vcs = get_vcs_adapter()
            status = "success" if not score["gate_blocked"] else "failure"
            vcs.post_check(repo, sha, status, f"SciGate: {total}/100 ({grade})")
        except Exception as exc:
            logger.warning("VCS status post failed (non-fatal): %s", exc)

        # On PR events, run fix agent if score is below threshold
        if trigger == "pr" and score["gate_blocked"] and os.environ.get("ANTHROPIC_API_KEY"):
            try:
                from agents.fix_agent import run as fix_run
                fix_run(score, repo)
            except Exception as exc:
                logger.warning("Fix agent failed on PR (non-fatal): %s", exc)

        from agents.notify_agent import notify
        notify(score, repo)

        logger.info("Webhook scan complete: %s@%s = %s/100 (%s)", repo, ref, total, grade)
    except Exception as exc:
        logger.error("Webhook scan failed: %s", exc)
        # Post failure status so the PR isn't left hanging
        try:
            from integrations.vcs import get_vcs_adapter
            vcs = get_vcs_adapter()
            vcs.post_check(repo, sha, "error", f"SciGate scan failed: {exc}")
        except Exception as vcs_exc:
            logger.warning("Could not post failure status to VCS: %s", vcs_exc)
