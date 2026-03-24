"""
SciGate — Agent 1: Audit Engine
────────────────────────────────
Classifies a research repository's domain and scores it across four
reproducibility dimensions (Environment, Seeds, Data, Documentation).

Works in two modes:
  • local   — scan a directory on disk (dev, Cursor, Claude Code)
  • gitlab  — scan a remote GitLab repo via API (GitLab Duo Flow)

Usage:
    # Local scan
    python audit_agent.py --path /path/to/repo

    # GitLab scan
    python audit_agent.py --gitlab-project my-lab/neuralsde --ref main

    # Diff-only scan (fast, on push)
    python audit_agent.py --gitlab-project my-lab/neuralsde --diff abc123..def456

Output: JSON score object matching the SciGate contract (see SKILL.md)
"""

import os
import re
import sys
import json
import time
import fnmatch
import argparse
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

GATE_THRESHOLD = int(os.getenv("SCIGATE_THRESHOLD", "75"))

ENV_FILES = [
    "requirements.txt", "requirements-dev.txt", "requirements-base.txt",
    "environment.yml", "environment.yaml", "conda.yml", "conda.yaml",
    "Pipfile", "pyproject.toml", "setup.cfg", "setup.py",
    "Dockerfile", "docker-compose.yml", "renv.lock",
]

README_PATTERNS = ["README.md", "README.rst", "README.txt", "README"]
DATA_SCRIPT_PATTERNS = [
    "download*.sh", "download*.py", "get_data*", "fetch_data*",
    "prepare_data*", "data/download*", "scripts/download*",
    "scripts/get_data*", "Makefile",
]

EXPERIMENT_PATTERNS = [
    "*train*.py", "*experiment*.py", "*eval*.py", "*run*.py",
    "*main*.py", "*fit*.py", "*finetune*.py", "*pretrain*.py",
    "*.R", "*.jl",
]

SEED_SKIP_PATTERNS = [
    "test_*.py", "*_test.py", "conftest.py",
    "setup.py", "setup.cfg", "__init__.py",
    "*util*.py", "*helper*.py", "*config*.py",
]

ABS_PATH_RE = re.compile(
    r'["\'](?:/home/|/mnt/|/root/|/Users/|/opt/|/data/|C:\\Users\\|D:\\)[^"\']{3,}["\']'
)

UNPINNED_DEP_RE = re.compile(
    r"^([a-zA-Z][a-zA-Z0-9_\-]*)\s*(?:>=|<=|>|<|~=|!=|\^)[^=]|^([a-zA-Z][a-zA-Z0-9_\-]*)\s*$",
    re.MULTILINE,
)

SEED_PATTERNS = {
    "py": [
        re.compile(r"torch\.manual_seed\s*\("),
        re.compile(r"torch\.cuda\.manual_seed"),
        re.compile(r"np(?:umpy)?\.random\.seed\s*\("),
        re.compile(r"random\.seed\s*\("),
        re.compile(r"tf\.random\.set_seed\s*\("),
        re.compile(r"jax\.random\.PRNGKey\s*\("),
        re.compile(r"Random\.seed!\s*\("),
    ],
    "R":  [re.compile(r"set\.seed\s*\(")],
    "jl": [re.compile(r"Random\.seed!\s*\(")],
}

DOMAIN_SIGNALS = [
    (r"torch|tensorflow|keras|jax|flax|paddle|mxnet", "ml-training", 3),
    (r"\.fit\(|\.train\(|DataLoader|nn\.Module|trainer\.", "ml-training", 2),
    (r"cuda|gpu|batch_size|learning_rate|optimizer", "ml-training", 1),
    (r"biopython|pysam|snakemake|nextflow|bioconductor", "bioinformatics", 3),
    (r"\.fastq|\.vcf|\.bam|\.sam|genome|chromosome|transcript", "bioinformatics", 2),
    (r"samtools|bwa|bowtie|star\b|hisat|gatk", "bioinformatics", 2),
    (r"netcdf4|xarray|iris\b|cartopy|cmip|cesm|wrf\b|gfdl", "climate-model", 3),
    (r"\.nc\b|\.nc4\b|lat.*lon|longitude|latitude|reanalysis", "climate-model", 2),
    (r"fixest|plm\b|stata|panel.*data|did\b|difference.in.difference", "econometrics", 3),
    (r"\.dta\b|instrument.*variable|regression.*table|stargazer", "econometrics", 2),
]


# ─── DATA CLASSES ─────────────────────────────────────────────────────────────

@dataclass
class DimScore:
    raw: int
    max: int = 25
    deductions: list[dict] = field(default_factory=list)

    @property
    def value(self) -> int:
        return max(0, min(self.max, self.raw))


@dataclass
class Fix:
    rank: int
    title: str
    files: list[str]
    dimension: str
    points_recoverable: int
    claude_fix_hint: str


@dataclass
class ScoreObject:
    domain: str
    scores: dict[str, int]
    grade: str
    commit_sha: str
    trigger: str
    fixes: list[dict]
    gate_blocked: bool
    gate_threshold: int
    scan_duration_ms: int


# ─── FILE READER ──────────────────────────────────────────────────────────────

class RepoReader:
    """Abstracts local vs GitLab file access."""

    def __init__(self, mode: str, path: str = "",
                 project: str = "", ref: str = "main"):
        self.mode = mode
        self.root = Path(path) if path else None
        self.project = project
        self.ref = ref
        self._cache: dict[str, str] = {}

        if mode == "gitlab":
            if not HAS_HTTPX:
                raise ImportError("httpx is required for gitlab mode: pip install httpx")
            self._gl_base = os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/")
            self._gl_token = os.environ.get("GITLAB_TOKEN", "")
            self._http = httpx.Client(
                headers={"PRIVATE-TOKEN": self._gl_token},
                timeout=20,
            )

    def list_files(self, path: str = "", recursive: bool = True) -> list[str]:
        if self.mode == "local":
            return self._local_list(path, recursive)
        return self._gitlab_list(path, recursive)

    def read(self, path: str) -> str | None:
        if path in self._cache:
            return self._cache[path]
        content = (
            self._local_read(path)
            if self.mode == "local"
            else self._gitlab_read(path)
        )
        if content is not None:
            self._cache[path] = content
        return content

    def exists(self, path: str) -> bool:
        return self.read(path) is not None

    def _local_list(self, subdir: str, recursive: bool) -> list[str]:
        base = self.root / subdir if subdir else self.root
        result = []
        if not base.exists():
            return result
        pattern = "**/*" if recursive else "*"
        for p in base.glob(pattern):
            if p.is_file() and ".git" not in p.parts:
                result.append(str(p.relative_to(self.root)))
        return result

    def _local_read(self, path: str) -> str | None:
        full = self.root / path
        if not full.exists():
            return None
        try:
            return full.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

    def _gitlab_list(self, path: str, recursive: bool) -> list[str]:
        params = {"ref": self.ref, "recursive": recursive, "per_page": 100}
        if path:
            params["path"] = path
        enc_proj = self.project.replace("/", "%2F")
        r = self._http.get(
            f"{self._gl_base}/api/v4/projects/{enc_proj}/repository/tree",
            params=params,
        )
        if r.status_code != 200:
            return []
        return [item["path"] for item in r.json() if item["type"] == "blob"]

    def _gitlab_read(self, path: str) -> str | None:
        import base64
        enc_proj = self.project.replace("/", "%2F")
        enc_path = path.replace("/", "%2F")
        r = self._http.get(
            f"{self._gl_base}/api/v4/projects/{enc_proj}/repository/files/{enc_path}",
            params={"ref": self.ref},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return base64.b64decode(r.json()["content"]).decode("utf-8", errors="replace")


# ─── DOMAIN CLASSIFIER ───────────────────────────────────────────────────────

def classify_domain(reader: RepoReader) -> str:
    votes: dict[str, int] = {
        "ml-training": 0,
        "bioinformatics": 0,
        "climate-model": 0,
        "econometrics": 0,
    }

    all_files = reader.list_files(recursive=True)
    sample = [
        f for f in all_files
        if f.endswith((".py", ".R", ".jl", ".f90", ".f", ".do", ".ipynb"))
    ][:40]

    corpus = ""
    for path in sample:
        content = reader.read(path)
        if content:
            corpus += content[:3000]

    corpus_lower = corpus.lower()

    for pattern, domain, weight in DOMAIN_SIGNALS:
        if re.search(pattern, corpus_lower):
            votes[domain] += weight

    best = max(votes, key=lambda d: votes[d])
    return best if votes[best] > 0 else "general-science"


# ─── DIMENSION: ENVIRONMENT ──────────────────────────────────────────────────

def score_environment(reader: RepoReader) -> DimScore:
    dim = DimScore(raw=25)
    all_files = reader.list_files(recursive=False)
    all_files_lower = [f.lower() for f in all_files]

    has_env_file = False
    has_dockerfile = False

    for ef in ENV_FILES:
        if ef.lower() in all_files_lower:
            has_env_file = True
            if ef.lower() == "dockerfile":
                has_dockerfile = True
            break

    if not has_env_file:
        dim.raw -= 15
        dim.deductions.append({
            "issue": "No environment file found",
            "files": [],
            "points": 15,
            "hint": "Create requirements.txt with pinned versions or a conda environment.yml",
        })
        return dim

    for req_name in ["requirements.txt", "requirements-dev.txt"]:
        content = reader.read(req_name)
        if not content:
            continue
        lines = [
            l.strip() for l in content.splitlines()
            if l.strip() and not l.startswith("#") and not l.startswith("-")
        ]
        unpinned = [
            l for l in lines
            if l and "==" not in l and not l.startswith("git+") and not l.startswith("http")
        ]
        if unpinned:
            penalty = min(10, len(unpinned) * 2)
            dim.raw -= penalty
            dim.deductions.append({
                "issue": f"{len(unpinned)} unpinned dependencies in {req_name}",
                "files": [req_name],
                "points": penalty,
                "hint": f"Pin all deps with ==: e.g. numpy==1.26.4. Found: {unpinned[:3]}",
            })
            break

    if has_dockerfile:
        df_content = reader.read("Dockerfile") or ""
        from_line = next(
            (l for l in df_content.splitlines() if l.strip().upper().startswith("FROM")),
            ""
        )
        if from_line and "@sha256:" not in from_line:
            dim.raw -= 5
            dim.deductions.append({
                "issue": "Dockerfile base image not pinned to SHA digest",
                "files": ["Dockerfile"],
                "points": 5,
                "hint": (
                    f"Replace mutable tag in '{from_line.strip()}' with "
                    "python@sha256:<digest> for bit-perfect reproducibility"
                ),
            })

    return dim


# ─── DIMENSION: SEEDS ────────────────────────────────────────────────────────

def score_seeds(reader: RepoReader, domain: str) -> DimScore:
    dim = DimScore(raw=25)
    all_files = reader.list_files(recursive=True)

    def is_experiment(path: str) -> bool:
        name = Path(path).name.lower()
        if any(fnmatch.fnmatch(name, p) for p in SEED_SKIP_PATTERNS):
            return False
        return any(fnmatch.fnmatch(name, p) for p in ["*.py", "*.R", "*.jl"])

    exp_files = [f for f in all_files if is_experiment(f)]

    priority = [
        f for f in exp_files
        if any(kw in f.lower() for kw in
               ("train", "experiment", "eval", "run", "main", "fit"))
    ]
    candidates = (priority + [f for f in exp_files if f not in priority])[:20]

    unseeded_files: list[str] = []

    for path in candidates:
        content = reader.read(path)
        if not content:
            continue

        ext = Path(path).suffix.lstrip(".")
        lang = "R" if ext == "R" else ("jl" if ext == "jl" else "py")
        patterns = SEED_PATTERNS.get(lang, SEED_PATTERNS["py"])

        has_random = bool(
            re.search(
                r"random\.|np\.random\.|torch\.|tf\.random\.|jax\.random\.|set\.seed|Random\.seed",
                content,
            )
        )
        if not has_random:
            continue

        has_seed = any(p.search(content) for p in patterns)
        if not has_seed:
            unseeded_files.append(path)

    if unseeded_files:
        penalty = min(25, len(unseeded_files) * 5)
        dim.raw -= penalty
        dim.deductions.append({
            "issue": f"{len(unseeded_files)} script(s) use randomness without seeding",
            "files": unseeded_files[:5],
            "points": penalty,
            "hint": (
                "Add seed block at top of each file: "
                "torch.manual_seed(42), np.random.seed(42), random.seed(42)"
                if domain == "ml-training" else
                "Add set.seed(42) before any random calls"
                if domain in ("econometrics", "bioinformatics") else
                "Add np.random.seed(42) and random.seed(42) at file top"
            ),
        })

    return dim


# ─── DIMENSION: DATA PROVENANCE ──────────────────────────────────────────────

def score_data(reader: RepoReader) -> DimScore:
    dim = DimScore(raw=25)
    all_files = reader.list_files(recursive=True)

    source_files = [
        f for f in all_files
        if f.endswith((".py", ".R", ".jl", ".sh", ".yaml", ".yml"))
    ]
    abs_path_files: list[str] = []

    for path in source_files[:50]:
        content = reader.read(path)
        if content and ABS_PATH_RE.search(content):
            abs_path_files.append(path)

    if abs_path_files:
        penalty = min(15, len(abs_path_files) * 5)
        dim.raw -= penalty
        dim.deductions.append({
            "issue": f"Hardcoded absolute paths in {len(abs_path_files)} file(s)",
            "files": abs_path_files[:4],
            "points": penalty,
            "hint": "Replace with os.path.join(os.path.dirname(__file__), '..', 'data', filename)",
        })

    has_download = any(
        fnmatch.fnmatch(Path(f).name.lower(), p)
        for f in all_files
        for p in DATA_SCRIPT_PATTERNS
    )
    if not has_download:
        dim.raw -= 10
        dim.deductions.append({
            "issue": "No data download or preparation script found",
            "files": [],
            "points": 10,
            "hint": "Create scripts/download_data.sh or data/README.md with provenance and checksums",
        })

    raw_data_exts = {".csv", ".tsv", ".json", ".parquet", ".h5", ".hdf5",
                     ".pkl", ".pickle", ".npy", ".npz", ".mat",
                     ".fastq", ".vcf", ".bam", ".nc", ".nc4"}
    committed_data = [
        f for f in all_files
        if Path(f).suffix.lower() in raw_data_exts
        and not f.startswith("tests/")
    ]
    if committed_data:
        dim.raw -= 5
        dim.deductions.append({
            "issue": f"{len(committed_data)} raw data file(s) committed to repo",
            "files": committed_data[:3],
            "points": 5,
            "hint": "Add data files to .gitignore; store in external storage with checksums",
        })

    return dim


# ─── DIMENSION: DOCUMENTATION ────────────────────────────────────────────────

def score_docs(reader: RepoReader) -> DimScore:
    dim = DimScore(raw=25)

    readme_content = ""
    for rname in README_PATTERNS:
        content = reader.read(rname)
        if content:
            readme_content = content.lower()
            break

    if not readme_content:
        dim.raw -= 25
        dim.deductions.append({
            "issue": "No README file found",
            "files": [],
            "points": 25,
            "hint": "Create README.md with: run instructions, hardware requirements, expected outputs",
        })
        return dim

    has_run_instructions = any(
        kw in readme_content
        for kw in ("python ", "bash ", "sh ", "pip install", "conda", "docker",
                   "```", "step 1", "## usage", "## quickstart", "## run",
                   "how to run", "to run", "getting started")
    )
    if not has_run_instructions:
        dim.raw -= 8
        dim.deductions.append({
            "issue": "README missing step-by-step run instructions",
            "files": ["README.md"],
            "points": 8,
            "hint": "Add a ## Usage section with copy-paste commands to reproduce results",
        })

    has_hardware = any(
        kw in readme_content
        for kw in ("gpu", "cuda", "ram", "memory", "cpu", "vram",
                   "v100", "a100", "rtx", "hardware", "requirement")
    )
    if not has_hardware:
        dim.raw -= 6
        dim.deductions.append({
            "issue": "README missing hardware requirements",
            "files": ["README.md"],
            "points": 6,
            "hint": "Add GPU model, RAM, and storage requirements needed to reproduce",
        })

    has_runtime = any(
        kw in readme_content
        for kw in ("hour", "minute", "second", "runtime", "time", "~", "approx",
                   "takes ", "took ")
    )
    if not has_runtime:
        dim.raw -= 5
        dim.deductions.append({
            "issue": "README missing estimated runtime",
            "files": ["README.md"],
            "points": 5,
            "hint": "Add approximate wall-clock time for full reproduction (e.g. '~4h on A100')",
        })

    has_outputs = any(
        kw in readme_content
        for kw in ("result", "output", "accuracy", "score", "metric",
                   "performance", "table", "figure", "plot", "expected")
    )
    if not has_outputs:
        dim.raw -= 4
        dim.deductions.append({
            "issue": "README missing expected outputs or results",
            "files": ["README.md"],
            "points": 4,
            "hint": "Add expected metrics or outputs so others can verify their reproduction",
        })

    has_citation = any(
        kw in readme_content
        for kw in ("citation", "bibtex", "arxiv", "doi", "paper", "cite",
                   "@article", "@inproceedings", "published in")
    )
    if not has_citation:
        dim.raw -= 2
        dim.deductions.append({
            "issue": "README missing paper citation or reference",
            "files": ["README.md"],
            "points": 2,
            "hint": "Add a ## Citation section with BibTeX or DOI link",
        })

    return dim


# ─── GRADE ASSIGNMENT ────────────────────────────────────────────────────────

def assign_grade(total: int) -> str:
    if total >= 90: return "EXCELLENT"
    if total >= 75: return "GOOD"
    if total >= 50: return "FAIR"
    if total >= 25: return "POOR"
    return "CRITICAL"


# ─── FIX BUILDER ─────────────────────────────────────────────────────────────

def build_fixes(
    env: DimScore, seeds: DimScore, data: DimScore, docs: DimScore
) -> list[Fix]:
    candidates: list[Fix] = []
    rank = 0

    for dim_name, dim in [("env", env), ("seeds", seeds),
                           ("data", data), ("docs", docs)]:
        for d in dim.deductions:
            rank += 1
            candidates.append(Fix(
                rank=rank,
                title=d["issue"],
                files=d.get("files", []),
                dimension=dim_name,
                points_recoverable=d["points"],
                claude_fix_hint=d["hint"],
            ))

    dim_priority = {"data": 0, "seeds": 1, "env": 2, "docs": 3}
    candidates.sort(
        key=lambda f: (-f.points_recoverable, dim_priority.get(f.dimension, 9))
    )

    for i, fix in enumerate(candidates):
        fix.rank = i + 1

    return candidates[:7]


# ─── MAIN AUDIT ──────────────────────────────────────────────────────────────

def audit(
    reader: RepoReader,
    commit_sha: str = "unknown",
    trigger: str = "push",
) -> dict:
    t0 = time.perf_counter()

    domain    = classify_domain(reader)
    env_dim   = score_environment(reader)
    seed_dim  = score_seeds(reader, domain)
    data_dim  = score_data(reader)
    docs_dim  = score_docs(reader)

    total = env_dim.value + seed_dim.value + data_dim.value + docs_dim.value
    grade = assign_grade(total)
    fixes = build_fixes(env_dim, seed_dim, data_dim, docs_dim)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    result = {
        "domain": domain,
        "scores": {
            "env":   env_dim.value,
            "seeds": seed_dim.value,
            "data":  data_dim.value,
            "docs":  docs_dim.value,
            "total": total,
        },
        "grade": grade,
        "commit_sha": commit_sha,
        "trigger": trigger,
        "fixes": [asdict(f) for f in fixes],
        "gate_blocked": total < GATE_THRESHOLD,
        "gate_threshold": GATE_THRESHOLD,
        "scan_duration_ms": elapsed_ms,
        "_deductions": {
            "env":   env_dim.deductions,
            "seeds": seed_dim.deductions,
            "data":  data_dim.deductions,
            "docs":  docs_dim.deductions,
        },
    }
    return result


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SciGate Audit Agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--path", help="Local repo directory to scan")
    group.add_argument("--gitlab-project", help="GitLab project path (group/repo)")

    parser.add_argument("--ref",    default="main",    help="Git ref (branch/tag/SHA)")
    parser.add_argument("--sha",    default="unknown", help="Commit SHA for output")
    parser.add_argument("--trigger",default="push",
                        choices=["push", "tag", "slash_command", "schedule"])
    parser.add_argument("--out",    default="-",       help="Output file path (- = stdout)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    if args.path:
        reader = RepoReader(mode="local", path=args.path)
    else:
        reader = RepoReader(mode="gitlab", project=args.gitlab_project, ref=args.ref)

    result = audit(reader, commit_sha=args.sha, trigger=args.trigger)

    output = json.dumps(result, indent=2 if args.pretty else None)
    if args.out == "-":
        print(output)
    else:
        Path(args.out).write_text(output)
        print(f"Score written to {args.out}", file=sys.stderr)
        print(
            f"Domain: {result['domain']} | Score: {result['scores']['total']}/100 "
            f"| Grade: {result['grade']} | {result['scan_duration_ms']}ms",
            file=sys.stderr,
        )
