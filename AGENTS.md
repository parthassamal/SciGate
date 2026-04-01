# SciGate Agent Instructions

> These instructions apply to any AI agent (Claude Code, Cursor, GitHub Copilot)
> working on the SciGate codebase.

## System Overview

SciGate is a Scientific Reproducibility Intelligence Platform that scores
repositories across 6 dimensions (Environment, Seeds, Data, Documentation,
Testing, Compliance) and generates AI-authored fixes. 100% open-source stack.

## Agent Pipeline

1. **Agent 1 — Audit** (`agents/audit_agent.py`)
   - Classify scientific domain via heuristics
   - Score 6 dimensions (total 0–100)
   - Output structured JSON report (ScanReport v2)

2. **Agent 2 — Fix** (`agents/fix_agent.py`)
   - Read audit score, call Claude to generate fixes
   - Safety-filter: NEVER touch protected patterns (train, model, loss, etc.)
   - Open a draft PR via the VCS adapter (GitHub / Gitea)

3. **Agent 3 — Org Memory** (`agents/memory_agent.py`)
   - Persist scan to JSON history
   - Update pattern frequency index
   - Raise GitHub/Gitea issues on pattern spikes

4. **Agent 4 — Regression** (`agents/regression_agent.py`)
   - Compare current scan vs. last N scans
   - Detect score regression (-5 pts threshold per dimension)
   - Optionally block merge if regression_gate is enabled in policy

5. **Agent 5 — Notify** (`agents/notify_agent.py`)
   - Fan-out: VCS commit check, Mattermost, ntfy, Email
   - Badge URL generation
   - Grafana OnCall escalation for CRITICAL grade

6. **Tracker** (`agents/tracker.py`)
   - Pull Requests (open, merged, draft)
   - Commits (recent log, diffs, comparisons)
   - CI Jobs: Jenkins, Woodpecker CI, GitHub Actions
   - Dependency health (pin ratio, CVEs, deprecated, SBOM)

## Integration Layer

All external service calls go through adapter interfaces:

- `integrations/vcs/` — VCS adapter (GitHub, Gitea)
- `integrations/ci/` — CI adapter (Jenkins, Woodpecker, GitHub Actions)
- `integrations/notify/` — Notification adapter (ntfy, Mattermost, etc.)

## Policy-as-Code

Repository-level config at `.scigate/policy.yml` controls gate thresholds,
regression gates, notification channels, and dimension weights.

## Critical Rules

- NEVER call Anthropic API without a system prompt
- NEVER touch protected file patterns in fix generation
- NEVER store API keys in plaintext
- ALWAYS cap score_projected = min(total, 100)
- ALWAYS sort leaderboard by latest_score DESC
- ALWAYS include scan_id in log lines
