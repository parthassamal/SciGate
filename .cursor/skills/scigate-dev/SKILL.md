---
name: scigate-dev
description: >-
  Develop and extend SciGate, the Reproducibility Credit Score system for
  scientific codebases. 3-agent architecture (Audit, Fix, Org Memory) with
  field-aware scoring, badge generation, and Claude-powered fix plans.
  Use when working on SciGate agents, scoring rubric, CLI commands, check
  definitions, org memory, badge generation, or scientific reproducibility
  checks. Triggers on: scigate, reproducibility, audit agent, fix agent,
  scoring engine, badge, org memory, scientific code analysis.
---

# SciGate Development

## Project Layout

```
scigate/
├── agents/audit.py      # Agent 1: field classification + domain checks
├── agents/fix.py        # Agent 2: Claude fix generation with scientific skills
├── agents/memory.py     # Agent 3: confidence-scored org memory pattern store
├── scoring/engine.py    # 0-100 Reproducibility Credit Score
├── scoring/badge.py     # shields.io badge + Markdown reports
├── utils/claude_client.py
├── utils/repo_scanner.py
└── cli.py               # Click CLI: audit | score | fix | full | memory
```

## Core Workflow

1. **Audit**: `run_audit(repo_path)` — scans repo, classifies field via Claude, runs domain checks
2. **Score**: `compute_score(report)` — computes 0-100 with field-weighted deductions and bonuses
3. **Fix**: `generate_fix_plan(report)` — Claude generates targeted fixes with scientific reasoning
4. **Memory**: `OrgMemory.record(...)` — stores {pattern, failure, fix, score_delta} tuples

## Scoring

Start at 100. Deductions: CRITICAL=-20, HIGH=-12, MEDIUM=-6, LOW=-2.
Field multipliers boost domain checks (e.g., ML-001 seed check is 1.5x for ml-training repos).
Bonuses: pinned deps +5, containerization +5, README +3, license +2.
Grades: A+ ≥90, A ≥80, B ≥70, C ≥60, D ≥45, F <45.

## Adding Checks

In `agents/audit.py`:
1. Create a `Finding` with unique ID (prefix: ML-, BIO-, CLI-, STAT-, DEP-, PATH-, DOC-, ENV-)
2. Add to the appropriate `_run_*_checks()` function
3. Update `scoring/engine.py` field multipliers if domain-critical
4. Add bonus in `_compute_bonuses()` for the inverse

## Dashboard

- `dashboard/index.html` — full interactive UI with animated gauge, fix list, badge, gate status
- `dashboard/server.py` — API server wiring frontend to real scoring engine
- API: POST `/api/scan` `{ repo_path }` returns `{ score, fixes, badge_url }`
- Launch: `scigate dashboard` or `python dashboard/server.py`
- Port: 8742 (default)

## GitLab Integration

- `gitlab/fix_agent.py` — Agent 2 for GitLab CI, opens draft MRs with Claude fixes
- `gitlab/scigate-flow.yml` — Full 3-agent orchestration for GitLab Duo Flow
- Triggers: push (audit), tag (gate), /scigate (full scan), nightly (memory)
- Agent models: Sonnet for audit/fix, Haiku for memory

## Key Conventions

- All Claude calls go through `utils/claude_client.py` — never call anthropic directly
- Findings use the `Finding` dataclass — always set check_id, title, severity, description
- Org memory auto-saves to `~/.scigate/org_memory.json`
- CLI uses Click + Rich for output formatting
- `ANTHROPIC_API_KEY` env var required for Claude calls
- Protected files: never touch train/model/loss/network/arch/backbone/encoder/decoder
