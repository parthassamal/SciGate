---
name: scigate
version: 2.2.0
description: |
  Invoke for any SciGate file across agents/, api/, dashboard/, workers/,
  integrations/, policy/, or infra/. Loads the full domain classification
  rubric, multi-dimensional scoring rules, fix generation templates,
  open-source VCS conventions, Anthropic API patterns, async worker
  contracts, and enterprise observability standards.
requires:
  - anthropic>=0.25
  - fastapi>=0.110
  - celery>=5.3
  - redis>=5.0
  - neo4j>=5.0          # org memory graph (Community Edition)
  - qdrant-client>=1.9  # semantic vector memory (open source)
  - prometheus-client>=0.20
  - opentelemetry-sdk>=1.24
stack: fully open source — self-hostable, cloud-portable
tags: [reproducibility, science, agents, enterprise, open-source]
---

# SciGate — Scientific Reproducibility Intelligence Platform

> **Mission:** Assign a defensible reproducibility credit score (0–100) to
> any scientific repository, generate targeted AI-authored fixes, enforce
> org-level quality gates, and learn continuously from every scan.
> **100% open-source infrastructure. No vendor lock-in.**

---

## 1. Open Source Infrastructure Stack

```
┌─────────────────────────────────────────────────────────────────────┐
│                    INFRASTRUCTURE MAP                               │
│                                                                     │
│  Layer              OSS Choice              Scale Path              │
│  ──────────────     ────────────────────    ──────────────────────  │
│  Message Queue      Redis (Celery broker)   → RabbitMQ cluster      │
│  Task Workers       Celery                  → Kubernetes HPA        │
│  Relational DB      PostgreSQL              → Citus (sharding)      │
│  Graph DB           Neo4j Community Ed.     → Neo4j Cluster (EE)   │
│  Vector Store       Qdrant                  → Qdrant Distributed    │
│  Object Storage     MinIO                   → MinIO Distributed     │
│  Identity / SSO     Keycloak                → Keycloak HA cluster   │
│  CI Integration     Woodpecker CI / Jenkins → self-hosted cluster   │
│  Notifications      Ntfy + Mattermost       → sharded webhooks      │
│  Alerting           Grafana OnCall          → multi-zone routing    │
│  Metrics            Prometheus + Grafana    → Thanos (long-term)    │
│  Tracing            Tempo + OpenTelemetry   → distributed tracing   │
│  Logs               Loki + Promtail        → Loki cluster          │
│  Email              Postal (SMTP)           → MX failover           │
│  PDF Reports        WeasyPrint              → worker pool           │
│  VCS (hosted)       GitHub / Gitea          → Gitea cluster         │
└─────────────────────────────────────────────────────────────────────┘
```

**Principle:** every component runs in Docker Compose locally and
graduates to Kubernetes via Helm charts in production. No proprietary
SaaS dependency in the critical path.

---

## 2. Full System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          INGEST LAYER                                │
│   GitHub App Webhook  │  Gitea Webhook  │  CLI  │  REST API          │
│   (HMAC-SHA256 verified on all webhook paths)                        │
└──────────────┬───────────────────────────────────────────────────────┘
               │ enqueue(ScanJob)
               ▼
┌──────────────────────────┐    ┌──────────────────────────────────┐
│  Redis / RabbitMQ Queue  │    │  Auth Middleware (Keycloak)       │
│  (Celery broker)         │    │  JWT / API-Key / OIDC / SAML SSO  │
└──────────┬───────────────┘    └──────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         AGENT PIPELINE                               │
│                                                                      │
│  Agent 1: Audit         agents/audit_agent.py                        │
│  ├── Classify domain (heuristics + Qdrant embedding similarity)      │
│  ├── Score 6 dimensions                                              │
│  ├── Emit ScanReport (JSON contract v2)                              │
│  ├── Diff-aware: skip unchanged files vs. last scan hash             │
│  └── Trigger Agent 2 if score < gate_threshold                       │
│                            │                                         │
│  Agent 2: Fix              agents/fix_agent.py                       │
│  ├── Build dynamic system prompt via build_skill_context()           │
│  ├── Retrieve top-5 similar past fixes from Qdrant (few-shot)        │
│  ├── Generate minimal, targeted file patches                         │
│  ├── Safety-filter protected files                                   │
│  ├── Validate patch syntax (ast.parse for Python)                    │
│  └── Open draft PR via VCS adapter (GitHub / Gitea)                  │
│                            │                                         │
│  Agent 3: Org Memory       agents/memory_agent.py                    │
│  ├── Persist scan to Neo4j (repo → scan → findings graph)            │
│  ├── Upsert vectors to Qdrant (semantic pattern search)              │
│  ├── Update pattern frequency index in PostgreSQL                    │
│  ├── Alert on pattern spikes → Grafana OnCall / Ntfy                 │
│  └── Maintain scored leaderboard                                     │
│                            │                                         │
│  Agent 4: Regression       agents/regression_agent.py                │
│  ├── Compare current scan vs. N previous scans                       │
│  ├── Detect score regression (threshold: -5 pts any dimension)       │
│  ├── Attribute regression to commit via git blame                    │
│  └── Block merge if regression_gate enabled in policy                │
│                            │                                         │
│  Agent 5: Notify           agents/notify_agent.py                    │
│  ├── Fan-out: VCS Check, Mattermost, Ntfy, Email (Postal), Teams     │
│  ├── Grafana OnCall escalation for CRITICAL grade                    │
│  ├── Render badge SVG → store in MinIO                               │
│  └── Generate PDF compliance report via WeasyPrint → MinIO           │
│                            │                                         │
│  Tracker                   agents/tracker.py                         │
│  ├── Pull requests (open, merged, draft, SciGate-authored)           │
│  ├── Commits (log, diffs, blame attribution)                         │
│  ├── CI jobs: Woodpecker CI, Jenkins, GitHub Actions                 │
│  └── Dependency health (pin ratio, CVEs, deprecated, SBOM)          │
└──────────────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       PERSISTENCE LAYER                              │
│                                                                      │
│  PostgreSQL  — scan history, tenants, users, audit log, leaderboard  │
│  Neo4j CE    — org knowledge graph: repos, findings, patterns        │
│  Qdrant      — semantic vectors for fix retrieval & memory search    │
│  Redis       — queue, cache, rate-limit counters, session store      │
│  MinIO       — patch artefacts, PDF reports, badge SVGs, SBOMs       │
└──────────────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      DASHBOARD & API                                 │
│  dashboard/index.html    — SPA (React + Vite, served via Nginx)      │
│  api/server.py           — FastAPI (versioned: /v1, /v2)             │
│  api/auth.py             — Keycloak OIDC adapter, API-key mgmt       │
│  api/webhooks.py         — VCS event router (HMAC-SHA256 verified)   │
│  api/cost.py             — LLM token usage tracking per tenant       │
└──────────────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     OBSERVABILITY STACK (OSS)                        │
│  Prometheus + Grafana  — metrics dashboards                          │
│  Grafana Tempo         — distributed traces (OTel receiver)          │
│  Grafana Loki          — log aggregation (via Promtail)              │
│  Grafana OnCall        — on-call alerting & escalation               │
│  Thanos (optional)     — long-term metrics storage                   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. VCS Adapter Pattern

All VCS operations go through a shared interface — adding a new provider
means implementing one adapter, nothing else changes.

```python
# integrations/vcs/base.py
class VCSAdapter(ABC):
    @abstractmethod
    def open_draft_pr(self, repo, branch, title, body, files) -> str: ...
    @abstractmethod
    def post_check(self, repo, sha, status, summary) -> None: ...
    @abstractmethod
    def verify_webhook(self, payload, signature) -> bool: ...

# integrations/vcs/github_adapter.py   — GitHub REST API v3
# integrations/vcs/gitea_adapter.py    — Gitea REST API v1 (self-hosted)

# Factory — driven by env var VCS_PROVIDER=github|gitea
def get_vcs_adapter() -> VCSAdapter:
    return {"github": GitHubAdapter, "gitea": GiteaAdapter}[
        os.environ["VCS_PROVIDER"]
    ]()
```

**Gitea** (https://gitea.io) is the recommended self-hosted option —
single binary, full GitHub API compatibility, runs on a $5 VM.

---

## 4. Notification Adapter Pattern

```python
# integrations/notify/base.py
class NotifyAdapter(ABC):
    @abstractmethod
    def send(self, event: ScanEvent) -> None: ...

# Implementations:
# integrations/notify/mattermost.py  — open source Slack alternative
# integrations/notify/ntfy.py        — push notifications (self-hosted)
# integrations/notify/postal.py      — transactional email via SMTP
# integrations/notify/teams.py       — MS Teams webhook (optional)
# integrations/notify/oncall.py      — Grafana OnCall (CRITICAL only)

NOTIFY_REGISTRY = {
    "mattermost": MattermostAdapter,
    "ntfy":       NtfyAdapter,         # https://ntfy.sh — self-hostable
    "email":      PostalAdapter,       # https://postalserver.io
    "teams":      TeamsAdapter,
    "oncall":     GrafanaOnCallAdapter,
}
```

---

## 5. CI Adapter Pattern

```python
# integrations/ci/base.py
class CIAdapter(ABC):
    @abstractmethod
    def get_job_status(self, job_name: str) -> CIJobStatus: ...
    @abstractmethod
    def get_build_history(self, job_name: str, limit: int) -> list: ...

# Implementations:
# integrations/ci/jenkins.py         — Jenkins REST API
# integrations/ci/woodpecker.py      — Woodpecker CI REST API (OSS)
# integrations/ci/github_actions.py  — GitHub Actions REST API
```

**Woodpecker CI** (https://woodpecker-ci.org) is the recommended
open-source CI for self-hosted deployments — lightweight, YAML-native,
Docker-based pipelines.

---

## 6. Domain Classification

| Domain | Key Indicators |
|---|---|
| `ml-training` | PyTorch, TensorFlow, JAX, CUDA, training loops |
| `ml-inference` | ONNX, TorchServe, vLLM, quantization, serving configs |
| `bioinformatics` | samtools, bwa, STAR, VCF/FASTQ, R + Bioconductor |
| `climate-model` | NetCDF, Fortran, MPI, CESM, WRF |
| `econometrics` | R + fixest/plm, panel data, Stata |
| `computational-chemistry` | GROMACS, AMBER, Gaussian, XYZ/PDB |
| `neuroimaging` | FSL, FreeSurfer, SPM, NIfTI, BIDS |
| `general-science` | any other empirical research code |

**Classification strategy:**
1. Heuristics first — file extensions + import scan (fast, free)
2. If confidence < 0.7 → embedding similarity against domain prototype
   vectors stored in Qdrant (one API call, cached 30 min in Redis)

---

## 7. Scoring Rubric (6 Dimensions = 100 pts)

### Environment (0–17)
Start 17. Deduct: `-10` no env file, `-7` unpinned deps, `-4` Dockerfile
tag not SHA, `-3` CUDA unspecified, `-2` Python/R version undeclared.

### Seeds & Determinism (0–17)
Start 17. Deduct `-4` per unseeded random call in experiment/train/eval.
Seed APIs: `torch.manual_seed`, `np.random.seed`, `random.seed`,
`tf.random.set_seed`, `set.seed()` (R), `Random.seed!()` (Julia),
`jax.random.PRNGKey`. Also check: `PYTHONHASHSEED`, `cudnn.deterministic`.

### Data Provenance (0–17)
Start 17. Deduct: `-5` per hardcoded path, `-7` no download script,
`-4` raw data committed (>1 MB binary), `-4` no checksums, `-3` no data
versioning (DVC / LFS / manifest).

### Documentation (0–17)
Start 17. Deduct: `-6` no run instructions, `-4` no hardware requirements,
`-3` no expected runtime, `-2` no expected outputs, `-2` no citation.

### Testing & Validation (0–17)
Start 17. Deduct: `-8` no test suite, `-4` coverage < 40% on non-model
files, `-3` no data shape/dtype assertions before model call, `-2` no
integration/smoke test for main pipeline entry point.

### License & Compliance (0–15)
Start 15. Deduct: `-8` no LICENSE file, `-4` dependency license conflict
(e.g. GPL leak into MIT repo), `-3` NOTICE file absent (Apache-2.0 deps).

**Floor:** `max(0, dim_score)`. **Cap:** `min(sum, 100)`.

---

## 8. Grade Thresholds

| Grade | Range | Gate Behavior |
|---|---|---|
| `EXCELLENT` | 90–100 | Auto-approve |
| `GOOD` | 75–89 | Approve with suggestions |
| `FAIR` | 50–74 | Block merge; draft PR opened |
| `POOR` | 25–49 | Block merge; notify team lead |
| `CRITICAL` | 0–24 | Block merge; Grafana OnCall escalation |

---

## 9. Fix Generation Rules

```python
PROTECTED_PATTERNS = [
    "train", "model", "loss", "network", "arch",
    "backbone", "head", "encoder", "decoder",
    "weights", "checkpoint", "pretrained"
]

SEED_TEMPLATE = """
# [SciGate] Reproducibility seeds — do not remove
import random, os
random.seed({seed})
np.random.seed({seed})
torch.manual_seed({seed})
torch.cuda.manual_seed_all({seed})
os.environ["PYTHONHASHSEED"] = str({seed})
"""

PATH_FIX = (
    "os.path.join(os.path.dirname(os.path.abspath(__file__)),"
    " '..', 'data', filename)"
)

# Anthropic API call — always with system prompt, always log tokens
response = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=4096,
    system=build_skill_context(domain, fix),  # REQUIRED — never omit
    messages=[{"role": "user", "content": fix_prompt}]
)
log_token_usage(response.usage, tenant_id, scan_id)  # cost governance

# Retrieve few-shot examples from Qdrant before calling Claude
similar_fixes = qdrant.search(
    collection="fix_history",
    query_vector=embed(fix.claude_fix_hint),
    limit=5
)
```

---

## 10. ScanReport JSON Contract v2

```json
{
  "schema_version": "2.0",
  "scan_id": "uuid-v4",
  "tenant_id": "org-slug",
  "repo": { "owner": "", "name": "", "ref": "", "commit_sha": "" },
  "domain": "ml-training",
  "domain_confidence": 0.94,
  "scores": {
    "env": 0, "seeds": 0, "data": 0,
    "docs": 0, "testing": 0, "compliance": 0,
    "total": 0
  },
  "score_delta": 0,
  "grade": "FAIR",
  "regression_detected": false,
  "fixes": [{
    "rank": 1,
    "title": "",
    "files": [],
    "dimension": "",
    "points_recoverable": 0,
    "claude_fix_hint": "",
    "protected_file_skipped": false,
    "estimated_tokens": 0
  }],
  "gate_blocked": false,
  "gate_threshold": 75,
  "regression_gate_blocked": false,
  "scan_duration_ms": 0,
  "llm_cost_usd": 0.0,
  "memory_pattern_hits": [],
  "badge_url": "",
  "sbom_url": ""
}
```

---

## 11. API Surface

```
# Scan
POST  /v1/scan                { local_path | github_repo | gitea_repo }
GET   /v1/scan/{scan_id}
GET   /v1/scan/{scan_id}/badge.svg

# History & trends
GET   /v1/repo/{owner}/{repo}/history
GET   /v1/repo/{owner}/{repo}/trend
GET   /v1/activity/{owner}/{repo}

# Dependencies
POST  /v1/dependencies        -> { health_score, cvs, sbom_url }

# CI
GET   /v1/ci/{provider}/{job} -> { status, last_build }  # provider: jenkins|woodpecker|gha

# Org memory
GET   /v1/leaderboard         ?tenant_id=&limit=&sort=latest_score
GET   /v1/patterns
POST  /v1/patterns/search     { query }   -> Qdrant semantic match

# Auth (Keycloak-backed)
POST  /v1/auth/token
POST  /v1/auth/refresh

# Admin
GET   /v1/admin/usage         -> { scans_month, tokens, cost_usd }
GET   /v1/admin/audit-log

# Webhooks
POST  /v1/webhooks/github
POST  /v1/webhooks/gitea

# Policy
GET   /v1/policy/{tenant_id}
PUT   /v1/policy/{tenant_id}
```

---

## 12. Enterprise Features

### Multi-Tenancy
Row-level security in PostgreSQL. Every query scoped by `tenant_id`.
Tenant config owns: gate thresholds, notification channels, cost budgets,
regression gate on/off.

### Auth & Authorization
- Keycloak (https://keycloak.org) as the identity provider — OIDC + SAML 2.0
- JWT (15 min) + refresh tokens
- API keys for CI pipelines (hashed in DB, never stored plaintext)
- RBAC: `viewer` / `developer` / `maintainer` / `admin`

### Async Job Processing
- Celery + Redis for job queue; RabbitMQ as a drop-in broker swap for HA
- Priority lanes: `critical` > `pr_triggered` > `scheduled` > `manual`
- Dead-letter queue, 3 retries with exponential backoff
- Status polling: GET /v1/scan/{scan_id} or WebSocket push

### Diff-Aware Rescanning
Per-file SHA stored in PostgreSQL. Rescan only touches changed files.
Reduces LLM token spend ~60–80% on incremental commits.

### LLM Cost Governance
Track `input_tokens` + `output_tokens` per call. Hard monthly cap per
tenant — reject fix jobs when `cost_usd >= cost_limit_usd`.

### Object Storage (MinIO)
All binary artefacts (patches, PDFs, badge SVGs, SBOMs) stored in MinIO
buckets. Presigned URLs for dashboard downloads. Scale path: distributed
MinIO across nodes with erasure coding.

### Policy-as-Code
`.scigate/policy.yml` in repo root. Validated on push. Controls gate
thresholds, protected branches, notification channels, budget limits.

---

## 13. Observability

```python
# OTel span per agent critical path
with tracer.start_as_current_span("audit.score") as span:
    span.set_attribute("domain", domain)
    span.set_attribute("score.total", total)

# Prometheus metrics — exposed at GET /metrics
scigate_scan_duration_seconds      # histogram: domain, grade
scigate_queue_depth                # gauge: priority
scigate_llm_tokens_total           # counter: tenant, agent, model
scigate_fix_pr_opened_total        # counter: domain, dimension
scigate_regression_detected_total  # counter: tenant

# Logs — structured JSON, shipped to Loki via Promtail
# every log line includes: scan_id, tenant_id, agent, level, msg
```

Grafana dashboards: scan throughput, queue depth, score distribution
heatmap, LLM cost by tenant, agent error rate, regression trend.

---

## 14. Scale Path

```
Tier 1 — Single node (dev / small org)
  docker compose up --build
  All services on one host. Supports ~50 scans/day.

Tier 2 — Docker Swarm / small k8s (team)
  Celery workers: 3–5 replicas
  PostgreSQL: primary + 1 read replica
  Redis: sentinel mode
  MinIO: 2-node mirror
  Supports ~500 scans/day.

Tier 3 — Kubernetes + Helm (enterprise)
  Celery workers: HPA on queue depth metric
  PostgreSQL: Citus for horizontal sharding
  Redis: Redis Cluster (6 nodes)
  Neo4j: Causal cluster (3 nodes)
  Qdrant: distributed mode (3 nodes)
  MinIO: distributed erasure coding (4+ nodes)
  Keycloak: HA cluster behind load balancer
  Prometheus: Thanos sidecar for long-term storage
  Supports 10,000+ scans/day.
```

---

## 15. Invariants & Critical Rules

```
NEVER  call Anthropic API without a system prompt
NEVER  pass full file history to Claude — only files in fix.files
NEVER  let Agent 2 run on scheduled triggers — PR/push events only
NEVER  touch protected file patterns in fix generation
NEVER  run fix agent if tenant cost budget is exceeded
NEVER  store API keys or webhook secrets in plaintext
ALWAYS validate Python patches with ast.parse() before opening PR
ALWAYS verify webhook HMAC-SHA256 before processing any event
ALWAYS scope every DB query by tenant_id
ALWAYS log token usage immediately after each API call
ALWAYS sort leaderboard by latest_score DESC, not best_score
ALWAYS cap score_projected = min(total, 100)
ALWAYS include scan_id in every log line (structured JSON)
```

---

## 16. Local Dev Quick-Start

```bash
git clone scigate && cd scigate
cp .env.example .env
# set: ANTHROPIC_API_KEY, VCS_PROVIDER=github|gitea,
#      GITHUB_APP_* or GITEA_TOKEN, KEYCLOAK_*, MINIO_*

docker compose up --build
# Starts: api:8000, celery-worker, redis:6379, postgres:5432,
#         neo4j:7474, qdrant:6333, minio:9000, keycloak:8080,
#         prometheus:9090, grafana:3000, loki:3100, tempo:4317

python -m scigate.cli scan ./my-repo --threshold 75
```

---

## 17. Integrated OSS Tools

| Tool | Integration | Scoring Dimension |
|---|---|---|
| nbstripout (1.8k stars) | Detect git filter → data provenance bonus | Data Provenance |
| jupytext (6.8k stars) | Detect config → docs bonus | Documentation |
| DVC (14k stars) | Detect dvc.yaml/.dvc → data-versioning points | Data Provenance |
| Sacred (4.2k stars) | Detect imports → auto seed points | Seeds |
| MLflow | Detect MLproject → seed mgmt bonus | Seeds |
| ReproZip (345 stars) | Detect .reprozip-trace → provenance bonus | Data Provenance |
| Snakemake | Detect Snakefile → pipeline bonus | Data Provenance |
| reviewdog (8k stars) | --format reviewdog for inline PR annotations | CI Integration |

## 18. Roadmap

| Phase | Milestone |
|---|---|
| 3 | VS Code extension — inline score + fix suggestions |
| 3 | Gitea self-hosted org-level GitHub App equivalent |
| 3 | AST-based seed/path detection via tree-sitter (PurCL/RepoAudit pattern) |
| 4 | Automated benchmark regression detection (numeric result drift) |
| 4 | LLM-authored methodology review (hallucination risk flagging) |
| 5 | Public opt-in leaderboard + DOI-linked reproducibility certificates |
| 5 | Hugging Face model card completeness scoring |
| 5 | arXiv integration via Papers with Code API |
| 5 | Forgejo support (Gitea hard fork — emerging standard) |
