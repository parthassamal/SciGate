---
name: scigate-dev
description: >-
  Develop and extend SciGate, the Scientific Reproducibility Intelligence
  Platform. 5-agent pipeline (Audit, Fix, Memory, Regression, Notify) with
  6-dimension scoring, adapter-based integrations (VCS, CI, Notifications),
  policy-as-code, and 100% open-source infrastructure.
  Triggers on: scigate, reproducibility, audit agent, fix agent, scoring,
  regression, notify, tracker, integrations, policy, dashboard.
---

# SciGate Development v2.1

## Project Layout

```
scigate/
├── agents/
│   ├── audit_agent.py       # Agent 1: 6-dimension scoring
│   ├── fix_agent.py         # Agent 2: Claude fix generation + PR
│   ├── memory_agent.py      # Agent 3: org memory patterns
│   ├── regression_agent.py  # Agent 4: score regression detection
│   ├── notify_agent.py      # Agent 5: notification fan-out
│   └── tracker.py           # PRs, commits, CI, dependencies
├── integrations/
│   ├── vcs/                 # VCS adapters (GitHub, Gitea)
│   ├── ci/                  # CI adapters (Jenkins, Woodpecker, GHA)
│   └── notify/              # Notification adapters (ntfy, Mattermost)
├── api/server.py            # FastAPI server (/v1 prefix)
├── policy/loader.py         # Policy-as-code reader
├── dashboard/index.html     # Interactive SPA dashboard
└── scigate/cli.py           # CLI: audit | scan | dashboard
```

## Scoring (6 Dimensions = 100 pts)

| Dim | Max | Key |
|---|---|---|
| Environment | 17 | env |
| Seeds | 17 | seeds |
| Data Provenance | 17 | data |
| Documentation | 17 | docs |
| Testing | 17 | testing |
| Compliance | 15 | compliance |

Grades: EXCELLENT >= 90, GOOD >= 75, FAIR >= 50, POOR >= 25, CRITICAL < 25.

## Integration Adapters

- `VCS_PROVIDER=github|gitea` → `integrations/vcs/`
- `CI_PROVIDER=jenkins|woodpecker|gha` → `integrations/ci/`
- `SCIGATE_NOTIFY_CHANNELS=ntfy,mattermost` → `integrations/notify/`

## API (v1 prefix + legacy compat)

- POST `/v1/scan` → audit + memory + regression + notify
- GET `/v1/ci/{provider}/{job}` → CI status
- POST `/v1/dependencies` → dependency health
- POST `/v1/webhooks/github` → webhook receiver
- GET `/v1/policy/{tenant}` → policy config

## Key Conventions

- All Claude calls: system prompt ALWAYS required
- Protected files: NEVER touch train/model/loss/network/arch/backbone
- Policy: `.scigate/policy.yml` in repo root
- Launch: `uvicorn api.server:app --reload --port 8000`
