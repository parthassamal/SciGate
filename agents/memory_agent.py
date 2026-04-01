"""
SciGate — Agent 3: Org Memory
───────────────────────────────
Persists scan results to a JSONL history file, maintains a pattern index
of recurring reproducibility failures, updates the org leaderboard, and
raises GitHub issues when a failure pattern spikes across repos.

Storage (flat-file, no DB dependency):
    memory/scans/{repo_slug}.jsonl   — append-only scan history per repo
    memory/patterns.json             — cross-repo failure pattern index
    memory/leaderboard.json          — latest scores, sorted desc

Usage:
    python memory_agent.py --score-json score.json --repo-name owner/repo
    python memory_agent.py --consolidate  # nightly re-index
"""

import os
import json
import time
import hashlib
import argparse
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MEMORY_DIR   = Path(os.getenv("SCIGATE_MEMORY_DIR", "memory"))
ALERT_THRESH = int(os.getenv("SCIGATE_ALERT_THRESHOLD", "5"))


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def repo_slug(name: str) -> str:
    return name.replace("/", "__").replace(" ", "_")

def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default

def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))

def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


# ─── SCAN HISTORY ────────────────────────────────────────────────────────────

def persist_scan(score: dict, repo_name: str) -> None:
    scores = score["scores"]
    record = {
        "ts":         now_iso(),
        "repo":       repo_name,
        "domain":     score["domain"],
        "total":      scores["total"],
        "env":        scores.get("env", 0),
        "seeds":      scores.get("seeds", 0),
        "data":       scores.get("data", 0),
        "docs":       scores.get("docs", 0),
        "testing":    scores.get("testing", 0),
        "compliance": scores.get("compliance", 0),
        "grade":      score["grade"],
        "commit_sha": score["commit_sha"],
        "trigger":    score["trigger"],
    }
    path = MEMORY_DIR / "scans" / f"{repo_slug(repo_name)}.jsonl"
    append_jsonl(path, record)
    print(f"[Memory] Scan persisted -> {path}")


# ─── PATTERN INDEX ───────────────────────────────────────────────────────────

def update_patterns(score: dict, repo_name: str) -> list[dict]:
    patterns_path = MEMORY_DIR / "patterns.json"
    patterns: dict[str, dict] = load_json(patterns_path, {})

    newly_alerted: list[dict] = []

    for fix in score.get("fixes", []):
        pid = hashlib.md5(
            f"{fix['dimension']}:{fix['title'][:60]}".encode()
        ).hexdigest()[:12]

        if pid not in patterns:
            patterns[pid] = {
                "id":          pid,
                "description": fix["title"],
                "dimension":   fix["dimension"],
                "count":       0,
                "repos":       [],
                "alert":       False,
                "first_seen":  now_iso(),
                "last_seen":   now_iso(),
            }

        p = patterns[pid]
        p["count"]     += 1
        p["last_seen"]  = now_iso()
        if repo_name not in p["repos"]:
            p["repos"].append(repo_name)

        was_alerted = p["alert"]
        p["alert"] = p["count"] >= ALERT_THRESH
        if p["alert"] and not was_alerted:
            newly_alerted.append(p)
            print(f"[Memory] ALERT: pattern '{p['description']}' hit {p['count']} repos")

    save_json(patterns_path, patterns)
    print(f"[Memory] Pattern index updated ({len(patterns)} patterns)")
    return newly_alerted


# ─── LEADERBOARD ─────────────────────────────────────────────────────────────

def update_leaderboard(score: dict, repo_name: str) -> None:
    lb_path = MEMORY_DIR / "leaderboard.json"
    leaderboard: list[dict] = load_json(lb_path, [])

    entry = next((e for e in leaderboard if e["repo"] == repo_name), None)
    prev_score = entry["latest_score"] if entry else None

    if entry is None:
        entry = {
            "repo":          repo_name,
            "domain":        score["domain"],
            "latest_score":  score["scores"]["total"],
            "best_score":    score["scores"]["total"],
            "trend":         "stable",
            "last_scanned":  now_iso(),
            "scan_count":    1,
        }
        leaderboard.append(entry)
    else:
        new_total = score["scores"]["total"]
        entry["domain"]        = score["domain"]
        entry["latest_score"]  = new_total
        entry["best_score"]    = max(entry["best_score"], new_total)
        entry["last_scanned"]  = now_iso()
        entry["scan_count"]    = entry.get("scan_count", 0) + 1
        if prev_score is not None:
            if new_total > prev_score + 2:
                entry["trend"] = "up"
            elif new_total < prev_score - 2:
                entry["trend"] = "down"
            else:
                entry["trend"] = "stable"

    leaderboard.sort(key=lambda e: e["latest_score"], reverse=True)
    save_json(lb_path, leaderboard)
    print(f"[Memory] Leaderboard updated — {repo_name}: {score['scores']['total']}/100")


# ─── GITHUB ISSUE ALERT ──────────────────────────────────────────────────────

def raise_github_alert(pattern: dict) -> None:
    gh_repo  = os.environ.get("SCIGATE_ORG_REPO")
    gh_token = os.environ.get("GITHUB_TOKEN")
    gh_base  = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    if not gh_repo or not gh_token:
        print("[Memory] Skipping alert issue (SCIGATE_ORG_REPO or GITHUB_TOKEN not set)")
        return

    try:
        import httpx
        repos_str = ", ".join(f"`{r}`" for r in pattern["repos"])
        body = textwrap.dedent(f"""
            ## SciGate pattern alert

            **Pattern:** {pattern['description']}
            **Dimension:** {pattern['dimension']}
            **Affected repos:** {pattern['count']} ({repos_str})

            This failure pattern has appeared across {pattern['count']} repositories.
            Run the SciGate audit in each affected repo to get targeted fixes.

            ---
            _Raised automatically by SciGate Org Memory · {now_iso()}_
        """).strip()

        r = httpx.post(
            f"{gh_base}/repos/{gh_repo}/issues",
            headers={
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "title":  f"SciGate pattern spike: {pattern['description'][:80]}",
                "body": body,
                "labels": ["scigate", "scigate-alert"],
            },
            timeout=15,
        )
        if r.status_code in (200, 201):
            print(f"[Memory] Alert issue created: {r.json().get('html_url')}")
        else:
            print(f"[Memory] Alert issue failed: {r.status_code} {r.text[:200]}")
    except Exception as exc:
        print(f"[Memory] Alert issue error: {exc}")


# ─── NIGHTLY CONSOLIDATION ───────────────────────────────────────────────────

def consolidate() -> dict:
    scans_dir = MEMORY_DIR / "scans"
    if not scans_dir.exists():
        return {"status": "nothing_to_consolidate"}

    all_records: list[dict] = []
    for jl_file in scans_dir.glob("*.jsonl"):
        for line in jl_file.read_text().splitlines():
            if line.strip():
                try:
                    all_records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    latest: dict[str, dict] = {}
    for rec in all_records:
        repo = rec["repo"]
        if repo not in latest or rec["ts"] > latest[repo]["ts"]:
            latest[repo] = rec

    leaderboard = [
        {
            "repo":         repo,
            "domain":       rec["domain"],
            "latest_score": rec["total"],
            "best_score":   max(r["total"] for r in all_records if r["repo"] == repo),
            "trend":        "stable",
            "last_scanned": rec["ts"],
            "scan_count":   sum(1 for r in all_records if r["repo"] == repo),
        }
        for repo, rec in latest.items()
    ]
    leaderboard.sort(key=lambda e: e["latest_score"], reverse=True)
    save_json(MEMORY_DIR / "leaderboard.json", leaderboard)

    patterns: dict = load_json(MEMORY_DIR / "patterns.json", {})
    active_repos = set(latest.keys())
    for pid, p in patterns.items():
        p["repos"] = [r for r in p["repos"] if r in active_repos]
        p["count"] = len(p["repos"])
        p["alert"] = p["count"] >= ALERT_THRESH

    save_json(MEMORY_DIR / "patterns.json", patterns)

    print(f"[Memory] Consolidation complete: {len(leaderboard)} repos, {len(patterns)} patterns")
    return {
        "status":   "consolidated",
        "repos":    len(leaderboard),
        "patterns": len(patterns),
    }


# ─── SUMMARY BUILDERS ────────────────────────────────────────────────────────

def top_patterns(n: int = 3) -> list[dict]:
    patterns: dict = load_json(MEMORY_DIR / "patterns.json", {})
    sorted_p = sorted(patterns.values(), key=lambda p: p["count"], reverse=True)
    return sorted_p[:n]


def leaderboard_summary(n: int = 5) -> list[dict]:
    lb: list = load_json(MEMORY_DIR / "leaderboard.json", [])
    return lb[:n]


# ─── MAIN ────────────────────────────────────────────────────────────────────

def run(score: dict, repo_name: str) -> dict:
    persist_scan(score, repo_name)
    newly_alerted = update_patterns(score, repo_name)
    update_leaderboard(score, repo_name)

    for pattern in newly_alerted:
        raise_github_alert(pattern)

    return {
        "status":        "updated",
        "repo":          repo_name,
        "score":         score["scores"]["total"],
        "patterns_total": len(load_json(MEMORY_DIR / "patterns.json", {})),
        "newly_alerted": [p["description"] for p in newly_alerted],
        "top_patterns":  top_patterns(3),
        "leaderboard":   leaderboard_summary(5),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SciGate Memory Agent")
    parser.add_argument("--score-json", help="Path to Agent 1 score JSON")
    parser.add_argument("--repo-name",  help="Repo identifier, e.g. lab/neuralsde")
    parser.add_argument("--consolidate", action="store_true",
                        help="Rebuild index from scan history (nightly)")
    parser.add_argument("--top-patterns", action="store_true",
                        help="Print current top patterns and exit")
    parser.add_argument("--leaderboard",  action="store_true",
                        help="Print leaderboard and exit")
    args = parser.parse_args()

    if args.consolidate:
        result = consolidate()
    elif args.top_patterns:
        result = {"top_patterns": top_patterns(10)}
    elif args.leaderboard:
        result = {"leaderboard": leaderboard_summary(20)}
    else:
        if not args.score_json or not args.repo_name:
            parser.error("--score-json and --repo-name are required unless using a flag")
        with open(args.score_json) as f:
            score = json.load(f)
        result = run(score, args.repo_name)

    print(json.dumps(result, indent=2))
