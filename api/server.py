"""
SciGate — FastAPI Server
─────────────────────────
Connects the SciGate dashboard UI to the real audit engine.
One primary route: POST /scan — accepts a repo path or GitLab URL,
runs Agent 1, optionally triggers Agent 2, returns live score JSON.

Start:
    pip install fastapi uvicorn httpx anthropic
    uvicorn api.server:app --reload --port 8000

Environment variables:
    ANTHROPIC_API_KEY   — for Agent 2 fix generation
    GITLAB_TOKEN        — for remote repo scanning
    GITLAB_URL          — default: https://gitlab.com
    SCIGATE_THRESHOLD   — default: 75
    SCIGATE_MEMORY_DIR  — default: ./memory
"""

import os
import sys
import json
import tempfile
from pathlib import Path
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.audit_agent  import RepoReader, audit, assign_grade
from agents.memory_agent import run as memory_run, leaderboard_summary, top_patterns

# ─── APP ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SciGate API",
    description="Reproducibility credit scoring for scientific repositories",
    version="1.0.0",
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
    gitlab_project:  Optional[str] = Field(None, description="GitLab project path (group/repo)")
    ref:             str            = Field("main",    description="Git ref to scan")
    commit_sha:      str            = Field("unknown", description="Commit SHA for tracking")
    trigger:         Literal["push", "tag", "slash_command", "schedule", "api"] = "api"
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
    fix_mr_url:    Optional[str] = None
    memory:        Optional[dict] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    agents: dict


class LeaderboardResponse(BaseModel):
    leaderboard: list
    top_patterns: list


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
def health():
    return {
        "status":  "ok",
        "version": "1.0.0",
        "agents": {
            "audit":  "ready",
            "fix":    "ready" if os.environ.get("ANTHROPIC_API_KEY") else "no_api_key",
            "memory": "ready",
        },
    }


@app.post("/scan", response_model=ScanResponse)
def scan(req: ScanRequest, background_tasks: BackgroundTasks):
    if not req.local_path and not req.gitlab_project:
        raise HTTPException(
            status_code=422,
            detail="Provide either local_path or gitlab_project",
        )

    if req.local_path:
        path = Path(req.local_path)
        if not path.exists() or not path.is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"Local path not found: {req.local_path}",
            )
        reader = RepoReader(mode="local", path=str(path))
        repo_name = req.repo_name or path.name
    else:
        reader = RepoReader(
            mode="gitlab",
            project=req.gitlab_project,
            ref=req.ref,
        )
        repo_name = req.repo_name or req.gitlab_project

    try:
        score = audit(
            reader,
            commit_sha=req.commit_sha,
            trigger=req.trigger,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Audit agent failed: {exc}",
        )

    score_path = Path(tempfile.mktemp(suffix=".json"))
    score_path.write_text(json.dumps(score))

    fix_mr_url: Optional[str] = None

    if req.run_fix_agent and os.environ.get("ANTHROPIC_API_KEY"):
        project_id = os.environ.get("GITLAB_PROJECT_ID", "")
        if project_id:
            background_tasks.add_task(
                _run_fix_agent_bg,
                score_path=str(score_path),
                project_id=project_id,
            )

    background_tasks.add_task(_run_memory_agent_bg, score=score, repo_name=repo_name)

    return {
        **score,
        "fix_mr_url": fix_mr_url,
        "memory":     None,
    }


@app.get("/leaderboard", response_model=LeaderboardResponse)
def leaderboard():
    return {
        "leaderboard":  leaderboard_summary(20),
        "top_patterns": top_patterns(10),
    }


@app.get("/repo/{repo_slug}/history")
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
    return {"history": sorted(records, key=lambda r: r["ts"], reverse=True)}


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

def _run_memory_agent_bg(score: dict, repo_name: str) -> None:
    try:
        memory_run(score, repo_name)
    except Exception as exc:
        print(f"[Server] Memory agent background task failed: {exc}")


def _run_fix_agent_bg(score_path: str, project_id: str) -> None:
    try:
        from agents.fix_agent import run as fix_run
        with open(score_path) as f:
            score = json.load(f)
        fix_run(score, project_id)
    except Exception as exc:
        print(f"[Server] Fix agent background task failed: {exc}")
    finally:
        Path(score_path).unlink(missing_ok=True)
