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
import tempfile
from pathlib import Path
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.audit_agent import RepoReader, audit
from agents.memory_agent import run as memory_run, leaderboard_summary, top_patterns
from agents.tracker import get_activity, get_jenkins_status, get_jenkins_builds, validate_dependencies

# ─── APP ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SciGate API",
    description="Scientific Reproducibility Intelligence Platform — 100% open source",
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


class HealthResponse(BaseModel):
    status: str
    version: str
    agents: dict
    infrastructure: dict


class LeaderboardResponse(BaseModel):
    leaderboard: list
    top_patterns: list


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

@app.post("/v1/scan", response_model=ScanResponse)
@app.post("/scan", response_model=ScanResponse, include_in_schema=False)
def scan(req: ScanRequest, background_tasks: BackgroundTasks):
    if not req.local_path and not req.github_repo and not req.gitea_repo:
        raise HTTPException(
            status_code=422,
            detail="Provide local_path, github_repo, or gitea_repo",
        )

    if req.local_path:
        path = Path(req.local_path)
        if not path.exists() or not path.is_dir():
            raise HTTPException(status_code=404, detail=f"Local path not found: {req.local_path}")
        reader = RepoReader(mode="local", path=str(path))
        repo_name = req.repo_name or path.name
    else:
        remote_repo = req.github_repo or req.gitea_repo
        reader = RepoReader(mode="github", repo=remote_repo, ref=req.ref)
        repo_name = req.repo_name or remote_repo

    try:
        score = audit(reader, commit_sha=req.commit_sha, trigger=req.trigger)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Audit agent failed: {exc}")

    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    tmp.write(json.dumps(score))
    tmp.close()
    score_path = Path(tmp.name)

    fix_pr_url: Optional[str] = None

    if req.run_fix_agent and os.environ.get("ANTHROPIC_API_KEY"):
        gh_repo = req.github_repo or req.gitea_repo or ""
        if gh_repo:
            background_tasks.add_task(_run_fix_agent_bg, score_path=str(score_path), repo=gh_repo)

    mem_result = None
    try:
        mem_result = memory_run(score, repo_name)
    except Exception as exc:
        print(f"[Server] Memory agent failed (non-fatal): {exc}")

    regression_result = None
    try:
        from agents.regression_agent import check_regression
        reg = check_regression(score, repo_name)
        regression_result = reg.to_dict()
    except Exception as exc:
        print(f"[Server] Regression check failed (non-fatal): {exc}")

    background_tasks.add_task(_run_notify_bg, score, repo_name, fix_pr_url)

    result = {k: v for k, v in score.items() if not k.startswith("_")}
    return {
        **result,
        "fix_pr_url":  fix_pr_url,
        "memory":      mem_result,
        "regression":  regression_result,
    }


# ─── LEADERBOARD & HISTORY ──────────────────────────────────────────────────

@app.get("/v1/leaderboard", response_model=LeaderboardResponse)
@app.get("/leaderboard", response_model=LeaderboardResponse, include_in_schema=False)
def leaderboard():
    return {
        "leaderboard":  leaderboard_summary(20),
        "top_patterns": top_patterns(10),
    }


@app.get("/v1/repo/{repo_slug}/history")
@app.get("/repo/{repo_slug}/history", include_in_schema=False)
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
@app.get("/activity/{owner}/{repo}", include_in_schema=False)
def activity(owner: str, repo: str, limit: int = 10):
    full_repo = f"{owner}/{repo}"
    try:
        return get_activity(full_repo, limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Activity fetch failed: {exc}")


@app.get("/v1/activity/{owner}/{repo}/commits")
@app.get("/activity/{owner}/{repo}/commits", include_in_schema=False)
def activity_commits(owner: str, repo: str, limit: int = 15, branch: str = "main"):
    from agents.tracker import GitHubTracker
    tracker = GitHubTracker(f"{owner}/{repo}")
    return {"commits": tracker.recent_commits(limit, branch)}


@app.get("/v1/activity/{owner}/{repo}/prs")
@app.get("/activity/{owner}/{repo}/prs", include_in_schema=False)
def activity_prs(owner: str, repo: str, limit: int = 10):
    from agents.tracker import GitHubTracker
    tracker = GitHubTracker(f"{owner}/{repo}")
    return {"pull_requests": tracker.recent_prs(limit)}


@app.get("/v1/activity/{owner}/{repo}/diff/{sha}")
@app.get("/activity/{owner}/{repo}/diff/{sha}", include_in_schema=False)
def activity_diff(owner: str, repo: str, sha: str):
    from agents.tracker import GitHubTracker
    tracker = GitHubTracker(f"{owner}/{repo}")
    return tracker.commit_diff(sha)


@app.get("/v1/activity/{owner}/{repo}/compare/{base}/{head}")
@app.get("/activity/{owner}/{repo}/compare/{base}/{head}", include_in_schema=False)
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


@app.get("/jenkins/{job_name}", include_in_schema=False)
def jenkins_status(job_name: str):
    return get_jenkins_status(job_name)


@app.get("/jenkins/{job_name}/builds", include_in_schema=False)
def jenkins_builds(job_name: str, limit: int = 10):
    return {"builds": get_jenkins_builds(job_name, limit)}


# ─── DEPENDENCY VALIDATION ───────────────────────────────────────────────────

class DepsRequest(BaseModel):
    local_path:  Optional[str] = None
    github_repo: Optional[str] = None
    ref:         str = "main"


@app.post("/v1/dependencies")
@app.post("/dependencies", include_in_schema=False)
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


# ─── POLICY ──────────────────────────────────────────────────────────────────

@app.get("/v1/policy/{tenant_id}")
def get_policy(tenant_id: str):
    try:
        from policy.loader import load_policy
        return load_policy(tenant_id)
    except Exception:
        return {
            "tenant_id": tenant_id,
            "gate_threshold": int(os.environ.get("SCIGATE_THRESHOLD", "75")),
            "regression_gate": False,
            "notify_channels": os.environ.get("SCIGATE_NOTIFY_CHANNELS", "").split(","),
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
            pass

    payload = json.loads(body)
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
    payload = json.loads(body)
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

@app.get("/v1/certificate/{owner}/{repo}")
def get_certificate(owner: str, repo: str):
    """Generate an HTML reproducibility certificate for the last scan."""
    slug = f"{owner}__{repo}".replace("/", "__")
    history_path = Path("memory/scans") / f"{slug}.jsonl"
    if not history_path.exists():
        slug_alt = f"{owner}/{repo}".replace("/", "__")
        history_path = Path("memory/scans") / f"{slug_alt}.jsonl"
    if not history_path.exists():
        raise HTTPException(status_code=404, detail="No scan history found")

    lines = [l.strip() for l in history_path.read_text().splitlines() if l.strip()]
    if not lines:
        raise HTTPException(status_code=404, detail="No scans recorded")

    last = json.loads(lines[-1])
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
<a href="https://github.com/parthassamal/SciGate">github.com/parthassamal/SciGate</a>
</div></body></html>"""

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


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

def _run_fix_agent_bg(score_path: str, repo: str) -> None:
    try:
        from agents.fix_agent import run as fix_run
        with open(score_path) as f:
            score = json.load(f)
        fix_run(score, repo)
    except Exception as exc:
        print(f"[Server] Fix agent background task failed: {exc}")
    finally:
        Path(score_path).unlink(missing_ok=True)


def _run_notify_bg(scan: dict, repo: str, pr_url: str | None) -> None:
    try:
        from agents.notify_agent import notify
        notify(scan, repo, pr_url)
    except Exception as exc:
        print(f"[Server] Notify agent failed (non-fatal): {exc}")


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
            print(f"[Server] VCS status post failed (non-fatal): {exc}")

        # On PR events, run fix agent if score is below threshold
        if trigger == "pr" and score["gate_blocked"] and os.environ.get("ANTHROPIC_API_KEY"):
            try:
                from agents.fix_agent import run as fix_run
                fix_run(score, repo)
            except Exception as exc:
                print(f"[Server] Fix agent failed on PR (non-fatal): {exc}")

        from agents.notify_agent import notify
        notify(score, repo)

        print(f"[Server] Webhook scan complete: {repo}@{ref} = {total}/100 ({grade})")
    except Exception as exc:
        print(f"[Server] Webhook scan failed: {exc}")
        # Post failure status so the PR isn't left hanging
        try:
            from integrations.vcs import get_vcs_adapter
            vcs = get_vcs_adapter()
            vcs.post_check(repo, sha, "error", f"SciGate scan failed: {exc}")
        except Exception:
            pass
