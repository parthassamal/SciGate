# SciGate

**Scientific Reproducibility Intelligence Platform**

SciGate is a 5-agent pipeline that audits every push and pull request against
a 6-dimension reproducibility rubric (0–100), auto-generates Claude-powered
fix PRs for failing repos, tracks score regressions across branches, and
maintains an org-wide memory of failure patterns — all triggered automatically
from GitHub Actions, webhooks, CLI, or the interactive dashboard.

Self-hostable. 100% open-source infrastructure. No vendor lock-in.

---

## Architecture

### System Overview

```mermaid
graph TB
    subgraph Ingest["Ingest Layer"]
        WH["GitHub / Gitea<br/>Webhook"]
        CLI["CLI"]
        API["REST API"]
        DASH["Dashboard"]
    end

    subgraph Pipeline["Agent Pipeline"]
        A1["Agent 1: Audit<br/>Score 6 dimensions → ScanReport"]
        A2["Agent 2: Fix<br/>Claude patches → Draft PR"]
        A3["Agent 3: Memory<br/>Pattern index → Leaderboard"]
        A4["Agent 4: Regression<br/>Score drops → Block merge"]
        A5["Agent 5: Notify<br/>Fan-out → Badge SVG"]
        TR["Tracker<br/>PRs · Commits · CI · Deps"]
    end

    subgraph Integrations["Integration Adapters"]
        VCS["VCS<br/>GitHub · Gitea"]
        CI["CI<br/>Jenkins · Woodpecker · GHA"]
        NF["Notify<br/>ntfy · Mattermost"]
    end

    subgraph Storage["Persistence"]
        MEM["Memory<br/>JSONL + JSON"]
        POL["Policy<br/>.scigate/policy.yml"]
    end

    WH & CLI & API & DASH --> A1
    A1 -->|score < threshold| A2
    A1 --> A3
    A1 --> A4
    A1 --> A5
    A1 --> TR
    A2 --> VCS
    A3 --> MEM
    A4 --> MEM
    A5 --> NF
    A5 --> VCS
    TR --> CI
    TR --> VCS
    A4 --> POL
```

### Backend (FastAPI)

```mermaid
graph LR
    subgraph API["api/server.py — FastAPI v2.1.0"]
        SCAN["POST /v1/scan"]
        LB["GET /v1/leaderboard"]
        HIST["GET /v1/repo/.../history"]
        ACT["GET /v1/activity/..."]
        CIR["GET /v1/ci/{provider}/{job}"]
        DEP["POST /v1/dependencies"]
        POL["GET /v1/policy/{tenant}"]
        WHG["POST /v1/webhooks/github"]
        WHT["POST /v1/webhooks/gitea"]
        HP["GET /health"]
    end

    SCAN --> AUD["audit_agent"]
    SCAN --> FIX["fix_agent"]
    SCAN --> MMA["memory_agent"]
    SCAN --> REG["regression_agent"]
    SCAN --> NOT["notify_agent"]
    ACT --> TRK["tracker"]
    CIR --> CIA["CI adapters"]
    WHG --> AUD
    WHT --> AUD
```

### Frontend (Dashboard SPA)

```mermaid
graph TD
    subgraph Dashboard["dashboard/index.html"]
        INP["Repo Input<br/>owner/repo · branch URL · local path"]
        GAU["Score Gauge<br/>0–100 animated"]
        DIM["6 Dimension Cards<br/>env · seeds · data · docs · testing · compliance"]
        FIX["Fix List<br/>ranked suggestions"]
        MEM["Memory Panel<br/>patterns · leaderboard"]
        ACT["Activity Panel<br/>PRs · commits · diffs"]
        DEP["Dependency Health<br/>pinning · CVEs · deprecated"]
        CIS["CI Status<br/>Jenkins · Woodpecker · GHA"]
        BDG["Badge Embed<br/>shields.io copy-to-clipboard"]
    end

    INP -->|POST /v1/scan| GAU
    GAU --> DIM
    GAU --> FIX
    GAU --> MEM
    INP -->|GET /v1/activity| ACT
    INP -->|POST /v1/dependencies| DEP
    INP -->|GET /v1/ci| CIS
    GAU --> BDG
```

Every PR and push to `main` triggers the full pipeline automatically via
GitHub Actions or webhook. PRs receive a commit status check with the score
and grade, and a draft fix PR is opened when the score falls below the gate
threshold.

---

## Scoring (6 Dimensions = 100 pts)

| Dimension | Max | What it checks |
|---|---|---|
| Environment | 17 | `requirements.txt` / `environment.yml`, pinned deps, Dockerfile tag |
| Seeds & Determinism | 17 | Unseeded `random`, `np.random`, `torch.manual_seed`, etc. |
| Data Provenance | 17 | Hardcoded paths, download scripts, raw data committed |
| Documentation | 17 | Run instructions, hardware, runtime, expected outputs, citation |
| Testing & Validation | 17 | Test suite presence, coverage ratio, assertions, smoke tests |
| License & Compliance | 15 | LICENSE file, copyleft conflicts, NOTICE file |

### Grades

| Grade | Range | Gate Behavior |
|---|---|---|
| EXCELLENT | 90–100 | Auto-approve |
| GOOD | 75–89 | Approve with suggestions |
| FAIR | 50–74 | Block merge; draft fix PR opened |
| POOR | 25–49 | Block merge; notify team lead |
| CRITICAL | 0–24 | Block merge; escalation |

---

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/parthassamal/SciGate && cd SciGate
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY, GITHUB_TOKEN

docker compose up --build
# Dashboard at http://localhost:8000

# With observability stack (Prometheus + Grafana):
docker compose --profile observability up --build
```

### Local Python

```bash
pip install -r requirements.txt
uvicorn api.server:app --reload --port 8000
# Dashboard at http://localhost:8000
```

### CLI

```bash
pip install -e .

scigate audit /path/to/repo           # rich terminal output
scigate audit /path/to/repo --json-out # JSON output
scigate scan /path/to/repo            # JSON for piping
scigate dashboard                     # launch web UI on :8000
```

### Standalone agents

```bash
# Audit a local repo
python agents/audit_agent.py --path /path/to/repo --pretty

# Audit a GitHub repo at a specific branch
python agents/audit_agent.py --github-repo owner/repo --ref feature-branch --out report.json

# Run fix agent (requires ANTHROPIC_API_KEY)
python agents/fix_agent.py --score-json report.json --repo owner/repo

# Update org memory
python agents/memory_agent.py --score-json report.json --repo-name owner/repo

# Check for score regression
python agents/regression_agent.py --score-json report.json --repo-name owner/repo
```

---

## Scanning PRs & Branches

SciGate audits any PR or branch through three paths:

| Path | Trigger | What happens |
|---|---|---|
| **GitHub Actions** | Push to `main` or PR opened/updated | Audit → Fix (if score < 75) → Memory → Regression (on PRs) → Gate |
| **Webhook** | `POST /v1/webhooks/github` | Scans PR head branch, posts commit status check, opens fix PR if below threshold |
| **Dashboard / API** | Paste `owner/repo/tree/my-branch` or call `POST /v1/scan` with `ref` | Scans the specified branch and returns full report |

Webhooks handle both `push` and `pull_request` events. For PRs, only
`opened`, `synchronize`, and `reopened` actions trigger a scan — label
changes, reviews, and other events are ignored.

---

## Integration Layer

SciGate uses an adapter pattern for all external services. Adding a new
provider means implementing one class.

### VCS Adapters (`integrations/vcs/`)
- **GitHub** — REST API v3 (default)
- **Gitea** — REST API v1 (self-hosted)
- Set `VCS_PROVIDER=github|gitea`

### CI Adapters (`integrations/ci/`)
- **Jenkins** — REST API
- **Woodpecker CI** — REST API (recommended OSS CI)
- **GitHub Actions** — REST API
- Set `CI_PROVIDER=jenkins|woodpecker|gha`

### Notification Adapters (`integrations/notify/`)
- **ntfy** — self-hostable push notifications
- **Mattermost** — open-source Slack alternative
- Set `SCIGATE_NOTIFY_CHANNELS=ntfy,mattermost`

---

## API Endpoints

All endpoints are available under the `/v1/` prefix. Legacy routes without
the prefix are maintained for backward compatibility.

### Scan & Score
| Method | Path | Description |
|---|---|---|
| POST | `/v1/scan` | Audit a repo (local path, GitHub URL, or Gitea) |
| GET | `/v1/leaderboard` | Org memory leaderboard + pattern index |
| GET | `/v1/repo/{slug}/history` | Scan history for a specific repo |
| GET | `/health` | Service health + agent status |

### Activity & Code Tracking
| Method | Path | Description |
|---|---|---|
| GET | `/v1/activity/{owner}/{repo}` | Combined PR + commit summary |
| GET | `/v1/activity/{owner}/{repo}/commits` | Recent commits |
| GET | `/v1/activity/{owner}/{repo}/prs` | Open + recent PRs |
| GET | `/v1/activity/{owner}/{repo}/diff/{sha}` | Diff for a single commit |
| GET | `/v1/activity/{owner}/{repo}/compare/{base}/{head}` | Diff between two refs |

### CI Status
| Method | Path | Description |
|---|---|---|
| GET | `/v1/ci/{provider}/{job}` | Job status (`jenkins`, `woodpecker`, `gha`) |
| GET | `/v1/ci/{provider}/{job}/builds` | Build history |

### Dependencies
| Method | Path | Description |
|---|---|---|
| POST | `/v1/dependencies` | Dependency health analysis (pinning, CVEs, deprecated) |

### Policy & Webhooks
| Method | Path | Description |
|---|---|---|
| GET | `/v1/policy/{tenant}` | Load tenant policy from `.scigate/policy.yml` |
| POST | `/v1/webhooks/github` | GitHub webhook receiver (HMAC-SHA256 verified) |
| POST | `/v1/webhooks/gitea` | Gitea webhook receiver |

---

## Policy-as-Code

Place `.scigate/policy.yml` in your repo root to configure gate behavior:

```yaml
gate_threshold: 75
regression_gate: true
regression_threshold: -5
notify_channels: [ntfy, mattermost]
protected_branches: [main, release/*]
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | For fixes | — | Anthropic API key (Agent 2) |
| `GITHUB_TOKEN` | For remote scans | — | GitHub PAT or App token |
| `VCS_PROVIDER` | No | `github` | `github` or `gitea` |
| `CI_PROVIDER` | No | `jenkins` | `jenkins`, `woodpecker`, or `gha` |
| `SCIGATE_THRESHOLD` | No | `75` | Gate threshold score |
| `SCIGATE_NOTIFY_CHANNELS` | No | — | Comma-separated: `ntfy,mattermost` |
| `JENKINS_URL` | For Jenkins CI | — | Jenkins base URL |
| `JENKINS_USER` / `JENKINS_TOKEN` | For Jenkins CI | — | Jenkins auth |
| `WOODPECKER_URL` / `WOODPECKER_TOKEN` | For Woodpecker | — | Woodpecker CI auth |
| `GITEA_URL` / `GITEA_TOKEN` | For Gitea | — | Gitea instance auth |
| `GITHUB_WEBHOOK_SECRET` | For webhooks | — | HMAC secret for webhook verification |
| `NTFY_URL` / `NTFY_TOPIC` | For ntfy | — | ntfy push notifications |
| `MATTERMOST_WEBHOOK_URL` | For Mattermost | — | Mattermost incoming webhook |

See `.env.example` for the full list.

---

## GitHub Actions CI/CD

The `.github/workflows/scigate.yml` workflow runs on every push to `main`
and on every pull request:

```mermaid
graph LR
    PUSH["push / PR"] --> AUDIT["scigate-audit<br/>Score 6 dims"]
    PUSH --> TEST["scigate-test<br/>Compile check"]

    AUDIT --> MEM["scigate-memory<br/>Update patterns"]
    AUDIT -->|"PR + score < 75"| FIX["scigate-fix<br/>Claude fix PR"]
    AUDIT -->|"PR only"| REG["scigate-regression<br/>Detect score drops"]
    AUDIT -->|"v*-submission tag"| GATE["scigate-gate<br/>Block if below threshold"]

    style AUDIT fill:#2563eb,color:#fff
    style FIX fill:#f59e0b,color:#000
    style REG fill:#8b5cf6,color:#fff
    style GATE fill:#ef4444,color:#fff
```

| Job | Trigger | Purpose |
|---|---|---|
| **scigate-audit** | All pushes + PRs | Run audit agent, output score, upload report artifact |
| **scigate-test** | All pushes + PRs | Compile-check all Python files |
| **scigate-fix** | PRs + tags when score < 75 | Run Claude fix agent, open fix PR |
| **scigate-memory** | All pushes + PRs | Update org memory patterns and leaderboard |
| **scigate-regression** | PRs only | Compare against previous scores, flag regressions |
| **scigate-gate** | `v*-submission` tags only | Block tag if score < threshold |

---

## Project Structure

```mermaid
graph LR
    subgraph Agents["agents/"]
        AA["audit_agent.py<br/><i>6-dimension scoring</i>"]
        FA["fix_agent.py<br/><i>Claude fix generation + PR</i>"]
        MA["memory_agent.py<br/><i>pattern tracking + leaderboard</i>"]
        RA["regression_agent.py<br/><i>score regression detection</i>"]
        NA["notify_agent.py<br/><i>notification fan-out + badge</i>"]
        TK["tracker.py<br/><i>PR / commit / CI / deps</i>"]
    end

    subgraph API["api/"]
        SV["server.py<br/><i>FastAPI v2.1 · /v1/ routes</i>"]
    end

    subgraph Front["dashboard/"]
        DH["index.html<br/><i>Interactive SPA</i>"]
    end

    subgraph Int["integrations/"]
        VCS["vcs/<br/><i>GitHub · Gitea</i>"]
        CID["ci/<br/><i>Jenkins · Woodpecker · GHA</i>"]
        NTF["notify/<br/><i>ntfy · Mattermost</i>"]
    end

    subgraph Pol["policy/"]
        PL["loader.py<br/><i>.scigate/policy.yml reader</i>"]
    end

    subgraph CLi["scigate/"]
        CL["cli.py<br/><i>scigate audit / scan / dashboard</i>"]
    end

    subgraph Mem["memory/"]
        SC["scans/<br/><i>per-repo JSONL history</i>"]
        LB["leaderboard.json"]
        PT["patterns.json"]
    end

    subgraph Infra["infra & config"]
        DF["Dockerfile"]
        DC["docker-compose.yml<br/><i>API + Redis + Prometheus + Grafana</i>"]
        PR["prometheus.yml"]
        GH[".github/workflows/scigate.yml<br/><i>CI/CD pipeline</i>"]
        SG[".scigate/policy.yml<br/><i>policy-as-code</i>"]
    end
```

| Directory | Purpose |
|---|---|
| `agents/` | 5 pipeline agents + tracker module |
| `api/` | FastAPI server with versioned routes (`/v1/`) |
| `dashboard/` | Single-page web UI with score gauge, dimension cards, activity panels |
| `integrations/` | Adapter pattern: VCS (GitHub, Gitea), CI (Jenkins, Woodpecker, GHA), Notify (ntfy, Mattermost) |
| `policy/` | Policy-as-code loader for `.scigate/policy.yml` |
| `scigate/` | CLI package (`scigate audit`, `scigate scan`, `scigate dashboard`) |
| `memory/` | Flat-file persistence: per-repo scan history, leaderboard, patterns |
| `infra/` | Prometheus config, Docker Compose with observability profile |
| `tests/` | Scoring engine tests |

---

## License

MIT
