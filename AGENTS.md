# SciGate — Agent Instructions

## Overview

SciGate is a reproducibility scoring system for scientific code repositories.
It uses three cooperating agents to audit, fix, and track reproducibility.

## Agent 1: Audit

Classify the repository's scientific domain and score it across four
reproducibility dimensions (Environment, Seeds, Data Provenance, Documentation),
each worth 0–25 points for a total of 0–100.

### Domain classification

Classify into exactly one:
- `ml-training` — PyTorch / TensorFlow / JAX training scripts
- `bioinformatics` — genomics, proteomics, sequencing pipelines
- `climate-model` — NetCDF, Fortran, MPI, climate simulation
- `econometrics` — R, Stata, economic panel data analysis
- `general-science` — any other empirical research code

### Scoring rubric

**Environment (0–25):** Start at 25. Deduct -15 for no environment file,
-10 for unpinned deps, -5 for mutable Dockerfile base tag, -3 for missing
CUDA version.

**Seeds (0–25):** Start at 25. Deduct -5 per unseeded random call in
experiment/training/evaluation code.

**Data provenance (0–25):** Start at 25. Deduct -5 per hardcoded absolute path,
-10 for no data download script, -5 for raw data without provenance, -5 for
no checksums.

**Documentation (0–25):** Start at 25. Deduct -8 for no run instructions,
-6 for no hardware requirements, -5 for no runtime estimate, -4 for no
expected output description, -2 for no citation.

### Grade thresholds

90–100 EXCELLENT, 75–89 GOOD, 50–74 FAIR, 25–49 POOR, 0–24 CRITICAL.

## Agent 2: Fix

Read the audit score, generate targeted code fixes, and open a draft MR.

### Safety rules

1. NEVER modify training logic, model definitions, or loss functions.
2. Only add/modify: environment files, seed wrappers, data scripts, README sections.
3. Each fix must be minimal and reviewable.
4. Process fixes in rank order (highest points recoverable first).

## Agent 3: Org Memory

Persist scan results, detect recurring failure patterns across repos,
maintain a leaderboard, and raise GitLab issues when failure patterns spike.

## Gate rule

Block `v*-submission` tags if score < 75.
