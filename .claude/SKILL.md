---
name: scigate
description: |
  Invoke when working on any SciGate agent file (audit.py, fix_agent.py,
  memory.py, scigate-flow.yml, or any file in agents/, gitlab/, scoring/, dashboard/).
  Loads the full domain classification rubric, scoring rules, fix generation
  templates, GitLab Flow conventions, and Anthropic API patterns.
---

# SciGate skill context

You are building SciGate — a system that assigns a reproducibility credit
score (0-100) to scientific research repositories and generates targeted
fixes via the Anthropic API.

## Architecture

```
GitLab push / tag / /scigate command
          |
          v
  Agent 1: Audit  (scigate/agents/audit.py)
  |-- Classify domain via Claude
  |-- Score 4 dimensions x 25 pts
  |-- Output structured report
  '-- Trigger Agent 2 if score < threshold
          |
          v
  Agent 2: Fix  (scigate/agents/fix.py + gitlab/fix_agent.py)
  |-- For each finding, call Claude with scientific skills
  |-- Generate minimal file changes
  |-- Safety-filter (never touch model/train/loss files)
  '-- Open draft MR (GitLab mode)
          |
          v
  Agent 3: Org Memory  (scigate/agents/memory.py)
  |-- Persist scan to JSON history
  |-- Update pattern index with confidence scores
  |-- Alert on pattern spikes
  '-- Feed hints back to Agent 1
```

## Domain classification

| Domain | Key indicators |
|---|---|
| ml-training | PyTorch, TensorFlow, JAX, CUDA, training loops |
| bioinformatics | samtools, bwa, STAR, VCF/FASTQ, R + Bioconductor |
| climate-model | NetCDF, Fortran, MPI, CESM, WRF |
| statistics | R + fixest/plm, panel data, p-values |
| general-science | any other empirical research code |

## Scoring rubric (0-25 per dimension)

### Environment
Start 25. Deduct: -15 no env file, -10 unpinned deps, -5 Dockerfile tag not SHA, -3 CUDA unspecified.

### Seeds
Start 25. Deduct 5 per unseeded random call in experiment/train/eval scripts.
ML: torch.manual_seed, np.random.seed, random.seed, tf.random.set_seed
R: set.seed; Julia: Random.seed!

### Data provenance
Start 25. Deduct: -5 per hardcoded path, -10 no download script, -5 raw data committed, -5 no checksums.

### Documentation
Start 25. Deduct: -8 no run instructions, -6 no hardware req, -5 no runtime, -4 no outputs, -2 no citation.

## Fix generation rules

1. `build_skill_context(domain, fix)` constructs Claude system prompt dynamically
2. Protected files: train, model, loss, network, arch, backbone, head, encoder, decoder
3. Seed injection: top of file, after imports, before logic
4. Path fixes: `os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', filename)`
5. Anthropic API: always use `client.messages.create()` with system prompt

## Score JSON contract

```json
{
  "domain": "ml-training",
  "scores": { "env": 0, "seeds": 0, "data": 0, "docs": 0, "total": 0 },
  "grade": "FAIR",
  "fixes": [{ "rank": 1, "title": "", "files": [], "dimension": "", "points_recoverable": 0, "claude_fix_hint": "" }],
  "gate_blocked": false,
  "gate_threshold": 75
}
```

## Check IDs (agents/audit.py)

DEP-001 unpinned deps, DEP-002 missing manifest, PATH-001 hardcoded paths,
DOC-001 missing README, DOC-002 missing license, ENV-001 no container,
ML-001 no seed, ML-002 CUDA non-determinism, ML-003 no checkpoints,
ML-004 DataLoader seeds, BIO-001 no conda env, BIO-002 no ref genome,
BIO-003 no workflow manager, CLI-001 no FP controls, CLI-002 no sci data format,
STAT-001 no statistical seed, MEM-001 org memory pattern.

## Dashboard (dashboard/)

- `index.html` — interactive single-page dashboard
- `server.py` — Flask API that connects dashboard to scoring engine
- API endpoint: POST /api/scan { repo_path } -> { score, fixes, report }

## Common mistakes to avoid

- Never call Anthropic API without a system prompt
- Never commit full file history to context — only files in fix.files
- Never let Agent 2 run on scheduled triggers
- Leaderboard sorts by latest_score desc, not best_score
- score_projected = min(total, 100) — cap at 100
