# SciGate — Reproducibility Credit Score for Science Code

[![GitLab Duo Agent Flow](https://img.shields.io/badge/GitLab%20Duo-Agent%20Flow-FC6D26)](https://about.gitlab.com/blog/gitlab-duo-agent-platform-complete-getting-started-guide/)
[![Powered by Claude](https://img.shields.io/badge/Powered%20by-Claude-8B5CF6)](https://anthropic.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

> **The reproducibility crisis costs science $28 billion/year.** SciGate is a GitLab Duo
> Agent Flow that gives every research repository a living credit score — and sends Claude
> to fix what's broken.

## The problem

Over 70% of researchers cannot reproduce published results. The root cause is almost
never the science — it's the code. Hardcoded data paths. Missing random seeds. Unpinned
dependencies. No download scripts. These failures are invisible until someone else tries
to run your work.

## What SciGate does

On every push, SciGate:

1. **Audits** the repo across four dimensions (Environment, Seeds, Data, Documentation)
2. **Scores** it 0–100 with a living README badge
3. **Calls Claude** to generate targeted fixes and open a draft MR
4. **Blocks** `v*-submission` tags if the score is below 75
5. **Remembers** failure patterns across the entire org and alerts when they spike

## Architecture — three agents

```
Push / Tag / @ai-scigate mention
        │
        ▼
Agent 1: Audit  (agents/audit_agent.py)
├─ Domain classification (ml-training | bioinformatics | climate | econometrics)
├─ Environment scoring   (pinned deps, Dockerfile SHA, conda env)
├─ Seed scoring          (torch.manual_seed, np.random.seed, set.seed)
├─ Data provenance       (absolute paths, download scripts, checksums)
└─ Documentation         (run instructions, hardware, expected outputs)
        │
        ▼
Agent 2: Fix  (agents/fix_agent.py)  ← powered by Anthropic Claude
├─ Generates targeted file patches per domain
├─ Safety filter: never touches model/train/loss files
└─ Opens draft MR with projected score
        │
        ▼
Agent 3: Org Memory  (agents/memory_agent.py)
├─ JSONL scan history per repo
├─ Cross-repo failure pattern index
├─ Leaderboard (sorted by score)
└─ GitLab issue alerts on pattern spikes
```

## Quickstart — Docker (recommended)

```bash
git clone https://gitlab.com/gitlab3483113/scigate && cd scigate

# Build and launch dashboard + API
docker compose up --build

# Open http://localhost:8000 — type a GitLab project path, hit Scan

# Scan a repo mounted into the container
SCAN_REPO_PATH=/path/to/research/repo docker compose run \
  scigate python agents/audit_agent.py --path /repo --pretty
```

## Quickstart — local Python

```bash
git clone https://gitlab.com/gitlab3483113/scigate && cd scigate

pip install -r requirements.txt

# Scan a local repo (no API key needed)
python agents/audit_agent.py --path /path/to/your/research/repo --pretty

# Start the API + dashboard
uvicorn api.server:app --reload --port 8000
# Open http://localhost:8000
```

## Scan a remote GitLab repo

No need to clone — SciGate reads files via the GitLab API:

```bash
# Via CLI
python agents/audit_agent.py --gitlab-project inkscape/inkscape --pretty

# Via API
curl -X POST http://localhost:8000/scan \
  -H "Content-Type: application/json" \
  -d '{"gitlab_project": "inkscape/inkscape"}'
```

Or type `inkscape/inkscape` directly in the dashboard input and hit Scan.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes (Agent 2) | Claude API key for fix generation |
| `GITLAB_TOKEN` | Yes (remote scans) | GitLab personal access token |
| `GITLAB_URL` | No | Default: `https://gitlab.com` |
| `GITLAB_PROJECT_ID` | Yes (Agent 2 MR) | GitLab project ID for MR creation |
| `SCIGATE_THRESHOLD` | No | Min score for submission tags. Default: `75` |
| `SCIGATE_MEMORY_DIR` | No | Memory storage path. Default: `./memory` |

## Install as a GitLab Duo Flow

1. Go to your project: **Automate > Flows > New flow**
2. Paste the contents of [`gitlab/scigate-flow.yml`](gitlab/scigate-flow.yml)
3. Enable the flow and set triggers (mention, assign, or pipeline events)
4. Set `ANTHROPIC_API_KEY` as a masked CI/CD variable
5. Mention the flow's service account on any MR to trigger a scan

The flow YAML follows the [GitLab Flow Registry v1 specification](https://gitlab.com/gitlab-org/modelops/applied-ml/code-suggestions/ai-assist/-/blob/main/docs/flow_registry/v1.md).

## GitLab CI/CD Pipeline

SciGate also ships a `.gitlab-ci.yml` that runs on every push:

- **scigate-audit** — scores the repo, produces `audit-report.json`
- **scigate-fix** — runs Claude fix agent when score < threshold (on tags/MRs)
- **scigate-memory** — records scan to org memory
- **scigate-gate** — blocks `v*-submission` tags if score < 75

## Score dimensions

| Dimension | Max | Key checks |
|---|---|---|
| Environment | 25 | Pinned deps, Dockerfile SHA, conda env |
| Seeds | 25 | `torch.manual_seed`, `np.random.seed`, `set.seed` |
| Data provenance | 25 | No absolute paths, download script, checksums |
| Documentation | 25 | Run instructions, hardware, runtime, citation |

## Hardware & estimated runtime

- **Audit only:** < 3s on any hardware (pure file reading + regex)
- **With fix generation:** 15–45s depending on Claude API response time
- **Remote GitLab scan:** 5–20s depending on repo size and API latency
- **No GPU required** for any component

## Use in Cursor

Copy `.cursor/rules/scigate-agents.mdc` into your `.cursor/rules/` directory.
Claude in Cursor will automatically apply the scoring rubric and fix generation
rules whenever you open any Python, R, or YAML file in this repo.

## Use with Claude Code

The `.claude/SKILL.md` file is loaded automatically when you run `claude` in
this repo's directory. Claude Code will have full context of the scoring rubric,
domain classifications, API patterns, and protected file rules.

## License

MIT — see [LICENSE](LICENSE)

---

*Built for the [GitLab AI Hackathon](https://gitlab.devpost.com) ·
Powered by [GitLab Duo Agent Platform](https://about.gitlab.com/blog/gitlab-duo-agent-platform-complete-getting-started-guide/)
and [Anthropic Claude](https://anthropic.com)*
