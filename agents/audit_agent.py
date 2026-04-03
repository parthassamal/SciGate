"""
SciGate — Agent 1: Audit Engine
────────────────────────────────
Classifies a research repository's domain and scores it across six
reproducibility dimensions:
  1. Environment (0–17)
  2. Seeds & Determinism (0–17)
  3. Data Provenance (0–17)
  4. Documentation (0–17)
  5. Testing & Validation (0–17)
  6. License & Compliance (0–15)

Works in two modes:
  • local   — scan a directory on disk (dev, Cursor, Claude Code)
  • github  — scan a remote GitHub repo via API

Usage:
    # Local scan
    python audit_agent.py --path /path/to/repo

    # GitHub scan
    python audit_agent.py --github-repo owner/repo --ref main

Output: JSON score object matching the SciGate contract v2 (see SKILL.md)
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

SEED_SKIP_PATTERNS = [
    "test_*.py", "*_test.py", "conftest.py",
    "setup.py", "setup.cfg", "__init__.py",
    "*util*.py", "*helper*.py", "*config*.py",
]

ABS_PATH_RE = re.compile(
    r'["\'](?:/home/|/mnt/|/root/|/Users/|/opt/|/data/|C:\\Users\\|D:\\)[^"\']{3,}["\']'
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


# Known reproducibility/provenance tools — presence awards bonus points
PROVENANCE_TOOLS = {
    "dvc.yaml": ("dvc", "data", 5),
    "dvc.lock": ("dvc", "data", 5),
    ".dvc": ("dvc", "data", 5),
    "Snakefile": ("snakemake", "data", 3),
    "nextflow.config": ("nextflow", "data", 3),
    ".reprozip-trace": ("reprozip", "data", 5),
    "MLproject": ("mlflow", "seeds", 3),
    "mlflow.yaml": ("mlflow", "seeds", 3),
    "_toc.yml": ("jupyter-book", "docs", 3),
    "Makefile.repro": ("reprozip", "data", 3),
}

# Tools detected via imports in source code
PROVENANCE_IMPORTS = {
    "from sacred import": ("sacred", "seeds", 4),
    "import sacred": ("sacred", "seeds", 4),
    "import dvc": ("dvc", "data", 3),
    "import mlflow": ("mlflow", "seeds", 3),
    "import wandb": ("wandb", "seeds", 2),
    "from dvc.api": ("dvc", "data", 3),
}

# nbstripout / jupytext detection patterns
# Known copyleft packages (common ones that cause license conflicts)
KNOWN_COPYLEFT_PACKAGES = {
    "gpl": ["pyqt5", "pyqt6", "pygobject", "readline", "mysql-connector-python",
             "ghostscript", "gnuplot-py", "gsl", "fftw"],
    "lgpl": ["pyqt5-sip", "chardet"],
    "agpl": ["mongodb-driver", "itext"],
}

# SPDX license identifiers for detection
SPDX_PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC",
                    "Unlicense", "0BSD", "CC0-1.0", "Zlib"}
SPDX_COPYLEFT = {"GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only",
                  "GPL-3.0-or-later", "AGPL-3.0-only", "AGPL-3.0-or-later"}

# ─── DATA CLASSES ─────────────────────────────────────────────────────────────

LICENSE_FILES = ["LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "LICENCE.md",
                 "COPYING", "COPYING.md"]

TEST_DIR_PATTERNS = ["tests/", "test/", "spec/", "testing/"]
TEST_FILE_PATTERNS = ["test_*.py", "*_test.py", "tests.py", "conftest.py",
                      "test_*.R", "*_test.R"]


@dataclass
class DimScore:
    raw: int
    max: int = 17
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
    explanation: str = ""
    effort_minutes: int = 0
    effort_label: str = ""


EFFORT_MAP = {
    "No environment file found":                     (5,  "5 min"),
    "unpinned dependencies":                         (5,  "5 min"),
    "Dockerfile base image not pinned":              (2,  "2 min"),
    "use randomness without seeding":                (2,  "2 min"),
    "Hardcoded absolute paths":                      (15, "15 min"),
    "No data download or preparation script found":  (30, "30 min"),
    "raw data file(s) committed":                    (10, "10 min"),
    "No README file found":                          (20, "20 min"),
    "missing step-by-step run instructions":         (10, "10 min"),
    "missing hardware requirements":                 (5,  "5 min"),
    "missing estimated runtime":                     (2,  "2 min"),
    "missing expected outputs":                      (5,  "5 min"),
    "missing paper citation":                        (2,  "2 min"),
    "No test suite found":                           (60, "1 hr"),
    "Low test coverage":                             (30, "30 min"),
    "No data shape/dtype assertions":                (10, "10 min"),
    "no smoke test":                                 (15, "15 min"),
    "No LICENSE file found":                         (1,  "1 min"),
    "copyleft license conflict":                     (15, "15 min"),
    "missing NOTICE file":                           (5,  "5 min"),
    "notebook(s) have committed cell outputs":       (5,  "5 min"),
    "copyleft packages":                             (15, "15 min"),
    "Provenance tools detected":                     (0,  "—"),
    "nbstripout configured":                         (0,  "—"),
    "Experiment framework detected":                 (0,  "—"),
}

EXPLANATION_MAP = {
    "No environment file found": (
        "Without a requirements.txt or environment.yml, anyone trying to reproduce "
        "your work must guess which packages and versions you used. Dependency drift "
        "is the #1 cause of irreproducible results."
    ),
    "unpinned dependencies": (
        "Unpinned dependencies (e.g. numpy>=1.20 instead of numpy==1.26.4) mean "
        "different installs get different versions. A minor update in any dependency "
        "can silently change your results."
    ),
    "Dockerfile base image not pinned": (
        "Mutable Docker tags like python:3.12 can point to different images over time. "
        "Pinning to a SHA digest guarantees bit-perfect environment reproduction."
    ),
    "use randomness without seeding": (
        "Unseeded random calls mean every run produces different results. "
        "Your paper's tables and figures cannot be independently verified. "
        "A single seed line at the top of each file fixes this permanently."
    ),
    "Hardcoded absolute paths": (
        "Absolute paths like /home/you/data/ break on every other machine. "
        "Using relative paths makes the code portable and reproducible anywhere."
    ),
    "No data download or preparation script found": (
        "Without a script to fetch the data, other researchers cannot obtain "
        "the exact dataset you used. A download script with checksums ensures "
        "data integrity across reproductions."
    ),
    "raw data file(s) committed": (
        "Large binary data in git makes cloning slow and versioning unreliable. "
        "Store data externally with checksums and a download script."
    ),
    "No README file found": (
        "A README is the entry point for anyone trying to reproduce your work. "
        "Without it, researchers must reverse-engineer your entire workflow."
    ),
    "missing step-by-step run instructions": (
        "Researchers need exact commands to reproduce results. Without run "
        "instructions, they spend hours guessing which script to run first."
    ),
    "missing hardware requirements": (
        "Results can differ across hardware (CPU vs GPU, memory constraints). "
        "Documenting hardware lets others match your setup or adjust expectations."
    ),
    "missing estimated runtime": (
        "Knowing that training takes 4 hours vs. 4 days affects whether someone "
        "attempts reproduction. Runtime estimates set realistic expectations."
    ),
    "missing expected outputs": (
        "Without expected results to compare against, a researcher who runs your "
        "code has no way to know if their reproduction succeeded."
    ),
    "missing paper citation": (
        "A citation links code to the paper it supports. Without it, the "
        "connection between repository and published results is lost."
    ),
    "No test suite found": (
        "Tests verify that code changes don't break results. Without tests, "
        "even small refactors can silently corrupt your pipeline's output."
    ),
    "Low test coverage": (
        "Low test coverage means large parts of the codebase can break without "
        "detection. Higher coverage catches regressions before they affect results."
    ),
    "No data shape/dtype assertions": (
        "Shape/dtype mismatches are a common silent failure mode in ML pipelines. "
        "Assertions catch corrupted data before it reaches the model."
    ),
    "no smoke test": (
        "A smoke test that runs the full pipeline on a tiny sample catches "
        "integration bugs that unit tests miss — the most common failure mode."
    ),
    "No LICENSE file found": (
        "Without a license, your code is legally unusable by other researchers. "
        "Adding a LICENSE file takes one minute and removes all ambiguity."
    ),
    "copyleft license conflict": (
        "GPL-licensed dependencies in a permissively-licensed project create "
        "legal uncertainty. Downstream users may unknowingly violate terms."
    ),
    "missing NOTICE file": (
        "Apache-2.0 requires a NOTICE file listing third-party attributions. "
        "Missing it technically violates the license terms."
    ),
    "notebook(s) have committed cell outputs": (
        "Committed notebook outputs can leak sensitive data (API keys, patient IDs, "
        "file paths) and bloat the repo. Clear outputs before committing."
    ),
    "copyleft packages": (
        "Known copyleft-licensed packages in a permissively-licensed project may "
        "legally require you to relicense your entire codebase under GPL."
    ),
    "Provenance tools detected": (
        "Data versioning tools (DVC, Snakemake, etc.) ensure datasets are tracked, "
        "versioned, and reproducible across machines."
    ),
    "nbstripout configured": (
        "nbstripout automatically strips notebook outputs before commit, "
        "preventing accidental data leakage and keeping diffs clean."
    ),
    "Experiment framework detected": (
        "Experiment frameworks like Sacred and MLflow handle seed management, "
        "config logging, and result tracking natively."
    ),
}


def _lookup_effort(issue: str) -> tuple[int, str]:
    for key, val in EFFORT_MAP.items():
        if key.lower() in issue.lower():
            return val
    return (10, "10 min")


def _lookup_explanation(issue: str) -> str:
    for key, val in EXPLANATION_MAP.items():
        if key.lower() in issue.lower():
            return val
    return ""


# ─── FILE READER ──────────────────────────────────────────────────────────────

class RepoReader:
    """Abstracts local vs GitHub file access."""

    def __init__(self, mode: str, path: str = "",
                 repo: str = "", ref: str = "main"):
        self.mode = mode
        self.root = Path(path) if path else None
        self.repo = repo
        self.ref = ref
        self._cache: dict[str, str] = {}

        if mode == "github":
            if not HAS_HTTPX:
                raise ImportError("httpx is required for github mode: pip install httpx")
            self._gh_base = os.environ.get(
                "GITHUB_API_URL", "https://api.github.com"
            ).rstrip("/")
            self._gh_token = os.environ.get("GITHUB_TOKEN", "")
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if self._gh_token:
                headers["Authorization"] = f"Bearer {self._gh_token}"
            self._http = httpx.Client(headers=headers, timeout=20)

    def list_files(self, path: str = "", recursive: bool = True) -> list[str]:
        if self.mode == "local":
            return self._local_list(path, recursive)
        return self._github_list(path, recursive)

    def read(self, path: str) -> str | None:
        if path in self._cache:
            return self._cache[path]
        content = (
            self._local_read(path)
            if self.mode == "local"
            else self._github_read(path)
        )
        if content is not None:
            self._cache[path] = content
        return content

    def exists(self, path: str) -> bool:
        return self.read(path) is not None

    def read_notebook_source(self, path: str) -> str | None:
        """Extract concatenated source code from a .ipynb file."""
        raw = self.read(path)
        if not raw:
            return None
        try:
            nb = json.loads(raw)
            cells = nb.get("cells", [])
            source_lines = []
            for cell in cells:
                if cell.get("cell_type") == "code":
                    source_lines.extend(cell.get("source", []))
                    source_lines.append("\n")
            return "".join(source_lines)
        except (json.JSONDecodeError, KeyError):
            return None

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
        except (OSError, ValueError):
            return None

    def _github_list(self, path: str, recursive: bool) -> list[str]:
        r = self._http.get(
            f"{self._gh_base}/repos/{self.repo}/git/trees/{self.ref}",
            params={"recursive": "1" if recursive else "0"},
        )
        if r.status_code != 200:
            return []
        items = r.json().get("tree", [])
        blobs = [item["path"] for item in items if item["type"] == "blob"]
        if path:
            blobs = [b for b in blobs if b.startswith(path.rstrip("/") + "/") or b == path]
        return blobs

    def _github_read(self, path: str) -> str | None:
        import base64
        r = self._http.get(
            f"{self._gh_base}/repos/{self.repo}/contents/{path}",
            params={"ref": self.ref},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return data.get("content", "")


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
    dim = DimScore(raw=17)
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
        dim.raw -= 10
        dim.deductions.append({
            "issue": "No environment file found",
            "files": [],
            "points": 10,
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
            penalty = min(7, len(unpinned))
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
            dim.raw -= 4
            dim.deductions.append({
                "issue": "Dockerfile base image not pinned to SHA digest",
                "files": ["Dockerfile"],
                "points": 4,
                "hint": (
                    f"Replace mutable tag in '{from_line.strip()}' with "
                    "python@sha256:<digest> for bit-perfect reproducibility"
                ),
            })

    return dim


# ─── DIMENSION: SEEDS ────────────────────────────────────────────────────────

def score_seeds(reader: RepoReader, domain: str) -> DimScore:
    dim = DimScore(raw=17)
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

    # Also check Jupyter notebooks for unseeded randomness
    notebooks = [f for f in all_files if f.endswith(".ipynb")][:10]
    unseeded_notebooks: list[str] = []
    for nb_path in notebooks:
        nb_src = reader.read_notebook_source(nb_path)
        if not nb_src:
            continue
        has_random = bool(re.search(
            r"random\.|np\.random\.|torch\.|tf\.random\.", nb_src
        ))
        if has_random:
            has_seed = any(p.search(nb_src) for p in SEED_PATTERNS["py"])
            if not has_seed:
                unseeded_notebooks.append(nb_path)

    all_unseeded = unseeded_files + unseeded_notebooks

    if all_unseeded:
        penalty = min(17, len(all_unseeded) * 4)
        dim.raw -= penalty
        nb_note = f" (including {len(unseeded_notebooks)} notebook(s))" if unseeded_notebooks else ""
        dim.deductions.append({
            "issue": f"{len(all_unseeded)} script(s) use randomness without seeding{nb_note}",
            "files": all_unseeded[:5],
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

    # Detect experiment frameworks that handle seeding natively (Sacred, MLflow, wandb)
    seed_tools: list[str] = []
    for path in candidates[:10]:
        content = reader.read(path)
        if not content:
            continue
        for pattern, (tool, target_dim, _bonus) in PROVENANCE_IMPORTS.items():
            if target_dim == "seeds" and pattern in content:
                seed_tools.append(tool)

    seed_tools = list(set(seed_tools))
    if seed_tools:
        bonus = min(4, len(seed_tools) * 2)
        dim.raw = min(dim.max, dim.raw + bonus)
        dim.deductions.append({
            "issue": f"Experiment framework detected: {', '.join(seed_tools)} (+{bonus} pts bonus)",
            "files": [],
            "points": -bonus,
            "hint": f"{', '.join(seed_tools)} handles seed management natively",
        })

    return dim


# ─── DIMENSION: DATA PROVENANCE ──────────────────────────────────────────────

def score_data(reader: RepoReader) -> DimScore:
    dim = DimScore(raw=17)
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

    # Also check notebooks for hardcoded paths
    for nb_path in [f for f in all_files if f.endswith(".ipynb")][:10]:
        nb_src = reader.read_notebook_source(nb_path)
        if nb_src and ABS_PATH_RE.search(nb_src):
            abs_path_files.append(nb_path)

    if abs_path_files:
        penalty = min(10, len(abs_path_files) * 5)
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
        dim.raw -= 7
        dim.deductions.append({
            "issue": "No data download or preparation script found",
            "files": [],
            "points": 7,
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
        dim.raw -= 4
        dim.deductions.append({
            "issue": f"{len(committed_data)} raw data file(s) committed to repo",
            "files": committed_data[:3],
            "points": 4,
            "hint": "Add data files to .gitignore; store in external storage with checksums",
        })

    # Provenance tool detection — award bonus (reduce deductions)
    all_files_lower = {f.lower() for f in all_files}
    detected_tools: list[str] = []
    for marker, (tool, target_dim, _bonus) in PROVENANCE_TOOLS.items():
        if target_dim != "data":
            continue
        if any(marker.lower() in f for f in all_files_lower):
            detected_tools.append(tool)

    # Check for .dvc directory or files
    if any(f.endswith(".dvc") for f in all_files):
        detected_tools.append("dvc")

    detected_tools = list(set(detected_tools))
    if detected_tools:
        bonus = min(5, len(detected_tools) * 3)
        dim.raw = min(dim.max, dim.raw + bonus)
        dim.deductions.append({
            "issue": f"Provenance tools detected: {', '.join(detected_tools)} (+{bonus} pts bonus)",
            "files": [],
            "points": -bonus,
            "hint": f"Good practice: {', '.join(detected_tools)} provides data versioning and provenance tracking",
        })

    # Check for nbstripout in .gitattributes
    gitattr = reader.read(".gitattributes")
    if gitattr and "nbstripout" in gitattr:
        dim.raw = min(dim.max, dim.raw + 2)
        dim.deductions.append({
            "issue": "nbstripout configured (+2 pts bonus)",
            "files": [".gitattributes"],
            "points": -2,
            "hint": "nbstripout prevents raw data leakage via notebook output cells",
        })

    return dim


# ─── DIMENSION: DOCUMENTATION ────────────────────────────────────────────────

def score_docs(reader: RepoReader) -> DimScore:
    dim = DimScore(raw=17)

    readme_content = ""
    for rname in README_PATTERNS:
        content = reader.read(rname)
        if content:
            readme_content = content.lower()
            break

    if not readme_content:
        dim.raw -= 17
        dim.deductions.append({
            "issue": "No README file found",
            "files": [],
            "points": 17,
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
        dim.raw -= 6
        dim.deductions.append({
            "issue": "README missing step-by-step run instructions",
            "files": ["README.md"],
            "points": 6,
            "hint": "Add a ## Usage section with copy-paste commands to reproduce results",
        })

    has_hardware = any(
        kw in readme_content
        for kw in ("gpu", "cuda", "ram", "memory", "cpu", "vram",
                   "v100", "a100", "rtx", "hardware", "requirement")
    )
    if not has_hardware:
        dim.raw -= 4
        dim.deductions.append({
            "issue": "README missing hardware requirements",
            "files": ["README.md"],
            "points": 4,
            "hint": "Add GPU model, RAM, and storage requirements needed to reproduce",
        })

    has_runtime = any(
        kw in readme_content
        for kw in ("hour", "minute", "second", "runtime", "time", "~", "approx",
                   "takes ", "took ")
    )
    if not has_runtime:
        dim.raw -= 3
        dim.deductions.append({
            "issue": "README missing estimated runtime",
            "files": ["README.md"],
            "points": 3,
            "hint": "Add approximate wall-clock time for full reproduction (e.g. '~4h on A100')",
        })

    has_outputs = any(
        kw in readme_content
        for kw in ("result", "output", "accuracy", "score", "metric",
                   "performance", "table", "figure", "plot", "expected")
    )
    if not has_outputs:
        dim.raw -= 2
        dim.deductions.append({
            "issue": "README missing expected outputs or results",
            "files": ["README.md"],
            "points": 2,
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


# ─── DIMENSION: TESTING & VALIDATION ────────────────────────────────────────

def score_testing(reader: RepoReader) -> DimScore:
    dim = DimScore(raw=17)
    all_files = reader.list_files(recursive=True)

    test_files = [
        f for f in all_files
        if any(fnmatch.fnmatch(Path(f).name, tp) for tp in TEST_FILE_PATTERNS)
        or any(f.startswith(td) for td in TEST_DIR_PATTERNS)
    ]

    if not test_files:
        dim.raw -= 8
        dim.deductions.append({
            "issue": "No test suite found",
            "files": [],
            "points": 8,
            "hint": "Add a tests/ directory with pytest or unittest test files",
        })
    else:
        source_py = [f for f in all_files if f.endswith(".py")
                     and not any(f.startswith(td) for td in TEST_DIR_PATTERNS)
                     and not f.startswith("setup")]
        test_ratio = len(test_files) / max(len(source_py), 1)
        if test_ratio < 0.15:
            dim.raw -= 4
            dim.deductions.append({
                "issue": f"Low test coverage: {len(test_files)} test files vs {len(source_py)} source files",
                "files": [],
                "points": 4,
                "hint": "Aim for at least 1 test file per 5 source files for non-model code",
            })

    py_files = [f for f in all_files if f.endswith(".py")][:30]
    has_assertions = False
    for path in py_files:
        content = reader.read(path)
        if content and re.search(r'assert\s+.*\.(shape|dtype|ndim|size)\b', content):
            has_assertions = True
            break

    if not has_assertions and any("train" in f.lower() or "model" in f.lower() for f in all_files):
        dim.raw -= 3
        dim.deductions.append({
            "issue": "No data shape/dtype assertions found before model calls",
            "files": [],
            "points": 3,
            "hint": "Add assert tensor.shape == (batch, channels, H, W) before model forward pass",
        })

    main_entry_points = [f for f in all_files if Path(f).name in
                         ("main.py", "run.py", "train.py", "experiment.py", "pipeline.py")]
    if main_entry_points and not test_files:
        dim.raw -= 2
        dim.deductions.append({
            "issue": "Main pipeline entry point has no smoke test",
            "files": main_entry_points[:2],
            "points": 2,
            "hint": "Add an integration test that runs the main pipeline on a small sample",
        })

    # Jupyter notebook checks
    notebooks = [f for f in all_files if f.endswith(".ipynb")]
    if notebooks:
        nb_with_outputs = []
        for nb_path in notebooks[:10]:
            raw = reader.read(nb_path)
            if not raw:
                continue
            try:
                nb = json.loads(raw)
                for cell in nb.get("cells", []):
                    if cell.get("cell_type") == "code" and cell.get("outputs"):
                        nb_with_outputs.append(nb_path)
                        break
            except (json.JSONDecodeError, KeyError):
                continue

        if nb_with_outputs:
            dim.deductions.append({
                "issue": f"{len(nb_with_outputs)} notebook(s) have committed cell outputs",
                "files": nb_with_outputs[:3],
                "points": 0,
                "hint": "Clear outputs before committing (nbstripout or jupyter nbconvert --clear-output) to avoid leaking data and reduce repo size",
            })

    return dim


# ─── DIMENSION: LICENSE & COMPLIANCE ─────────────────────────────────────────

def score_compliance(reader: RepoReader) -> DimScore:
    dim = DimScore(raw=15, max=15)
    all_files = reader.list_files(recursive=False)
    all_files_lower = [f.lower() for f in all_files]

    has_license = any(
        lf.lower() in all_files_lower
        for lf in LICENSE_FILES
    )

    if not has_license:
        dim.raw -= 8
        dim.deductions.append({
            "issue": "No LICENSE file found",
            "files": [],
            "points": 8,
            "hint": "Add a LICENSE file (MIT, Apache-2.0, or BSD-3-Clause recommended for research code)",
        })

    license_content = ""
    for lf in LICENSE_FILES:
        content = reader.read(lf)
        if content:
            license_content = content.lower()
            break

    repo_is_permissive = any(kw in license_content for kw in
                             ("mit license", "apache license", "bsd", "isc license"))

    if repo_is_permissive:
        req_content = reader.read("requirements.txt") or ""
        pipfile_content = reader.read("Pipfile") or ""
        pyproject_content = reader.read("pyproject.toml") or ""
        deps_text = (req_content + pipfile_content + pyproject_content).lower()

        # Check for known copyleft packages by name
        conflicting_pkgs: list[str] = []
        for _license_type, pkg_list in KNOWN_COPYLEFT_PACKAGES.items():
            for pkg in pkg_list:
                if pkg.lower() in deps_text:
                    conflicting_pkgs.append(pkg)

        # Also check for explicit GPL/AGPL mentions
        has_gpl_mention = any(
            kw in deps_text for kw in ("gpl", "agpl", "gnu general public")
        )

        if conflicting_pkgs or has_gpl_mention:
            dim.raw -= 4
            detail = f"Known copyleft packages: {', '.join(conflicting_pkgs)}" if conflicting_pkgs else "GPL/AGPL keywords found in dependency files"
            dim.deductions.append({
                "issue": f"Potential copyleft license conflict — {detail}",
                "files": ["requirements.txt"],
                "points": 4,
                "hint": "GPL-licensed dependencies in a permissively-licensed project may create conflicts. Consider alternatives or add license compatibility notes.",
            })

    # SPDX identifier detection in LICENSE file header
    if license_content:
        spdx_match = re.search(r"SPDX-License-Identifier:\s*(\S+)", license_content, re.IGNORECASE)
        if spdx_match:
            spdx_id = spdx_match.group(1).upper()
            if any(s in spdx_id for s in ("GPL", "AGPL")) and repo_is_permissive:
                dim.deductions.append({
                    "issue": f"SPDX identifier ({spdx_id}) conflicts with permissive license body",
                    "files": [lf for lf in LICENSE_FILES if reader.read(lf)][:1],
                    "points": 0,
                    "hint": "The SPDX identifier and license body disagree — clarify which applies",
                })

    has_notice = "notice" in " ".join(all_files_lower)
    if license_content and "apache" in license_content and not has_notice:
        dim.raw -= 3
        dim.deductions.append({
            "issue": "Apache-2.0 project missing NOTICE file",
            "files": [],
            "points": 3,
            "hint": "Apache-2.0 requires a NOTICE file for attributions",
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
    env: DimScore, seeds: DimScore, data: DimScore,
    docs: DimScore, testing: DimScore, compliance: DimScore,
) -> list[Fix]:
    candidates: list[Fix] = []
    rank = 0

    for dim_name, dim in [("env", env), ("seeds", seeds), ("data", data),
                           ("docs", docs), ("testing", testing),
                           ("compliance", compliance)]:
        for d in dim.deductions:
            rank += 1
            effort_min, effort_lbl = _lookup_effort(d["issue"])
            candidates.append(Fix(
                rank=rank,
                title=d["issue"],
                files=d.get("files", []),
                dimension=dim_name,
                points_recoverable=d["points"],
                claude_fix_hint=d["hint"],
                explanation=_lookup_explanation(d["issue"]),
                effort_minutes=effort_min,
                effort_label=effort_lbl,
            ))

    dim_priority = {"compliance": 0, "data": 1, "seeds": 2,
                    "env": 3, "testing": 4, "docs": 5}
    candidates.sort(
        key=lambda f: (-f.points_recoverable, dim_priority.get(f.dimension, 9))
    )

    for i, fix in enumerate(candidates):
        fix.rank = i + 1

    return candidates[:10]


# ─── MAIN AUDIT ──────────────────────────────────────────────────────────────

def audit(
    reader: RepoReader,
    commit_sha: str = "unknown",
    trigger: str = "push",
) -> dict:
    t0 = time.perf_counter()

    domain     = classify_domain(reader)
    env_dim    = score_environment(reader)
    seed_dim   = score_seeds(reader, domain)
    data_dim   = score_data(reader)
    docs_dim   = score_docs(reader)
    test_dim   = score_testing(reader)
    comp_dim   = score_compliance(reader)

    total = min(100, (
        env_dim.value + seed_dim.value + data_dim.value +
        docs_dim.value + test_dim.value + comp_dim.value
    ))
    grade = assign_grade(total)
    fixes = build_fixes(env_dim, seed_dim, data_dim, docs_dim, test_dim, comp_dim)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    projected = min(100, total + sum(f.points_recoverable for f in fixes))
    projected_grade = assign_grade(projected)
    total_effort = sum(f.effort_minutes for f in fixes)

    result = {
        "domain": domain,
        "scores": {
            "env":        env_dim.value,
            "seeds":      seed_dim.value,
            "data":       data_dim.value,
            "docs":       docs_dim.value,
            "testing":    test_dim.value,
            "compliance": comp_dim.value,
            "total":      total,
        },
        "grade": grade,
        "projected_score": projected,
        "projected_grade": projected_grade,
        "total_effort_minutes": total_effort,
        "total_effort_label": (
            f"~{total_effort} min" if total_effort < 60
            else f"~{total_effort // 60}h {total_effort % 60}m"
        ),
        "commit_sha": commit_sha,
        "trigger": trigger,
        "fixes": [asdict(f) for f in fixes],
        "gate_blocked": total < GATE_THRESHOLD,
        "gate_threshold": GATE_THRESHOLD,
        "scan_duration_ms": elapsed_ms,
        "_deductions": {
            "env":        env_dim.deductions,
            "seeds":      seed_dim.deductions,
            "data":       data_dim.deductions,
            "docs":       docs_dim.deductions,
            "testing":    test_dim.deductions,
            "compliance": comp_dim.deductions,
        },
    }
    return result


# ─── JOURNAL CHECKLISTS ──────────────────────────────────────────────────

JOURNAL_CHECKLISTS = {
    "nature": {
        "name": "Nature Methods",
        "url": "https://www.nature.com/nature/editorial-policies/reporting-standards",
        "criteria": [
            ("Code availability", lambda r: r["scores"]["env"] >= 10),
            ("Data availability", lambda r: r["scores"]["data"] >= 10),
            ("Software dependencies specified", lambda r: r["scores"]["env"] >= 14),
            ("Random seeds documented", lambda r: r["scores"]["seeds"] >= 14),
            ("Statistical methods described", lambda r: r["scores"]["docs"] >= 10),
            ("Hardware specification", lambda r: any("hardware" not in d.get("issue", "").lower() for d in r["_deductions"].get("docs", [])) if r["_deductions"].get("docs") else True),
            ("Expected runtime", lambda r: any("runtime" not in d.get("issue", "").lower() for d in r["_deductions"].get("docs", [])) if r["_deductions"].get("docs") else True),
            ("Step-by-step reproduction", lambda r: r["scores"]["docs"] >= 11),
            ("License specified", lambda r: r["scores"]["compliance"] >= 7),
        ],
    },
    "neurips": {
        "name": "NeurIPS Reproducibility Checklist",
        "url": "https://neurips.cc/Conferences/2024/PaperInformation/PaperChecklist",
        "criteria": [
            ("Code submitted", lambda r: True),
            ("Training seeds specified", lambda r: r["scores"]["seeds"] >= 14),
            ("Dependencies pinned", lambda r: r["scores"]["env"] >= 14),
            ("Hyperparameters documented", lambda r: r["scores"]["docs"] >= 8),
            ("Compute requirements stated", lambda r: not any("hardware" in d.get("issue", "").lower() for d in r["_deductions"].get("docs", []))),
            ("Expected runtime stated", lambda r: not any("runtime" in d.get("issue", "").lower() for d in r["_deductions"].get("docs", []))),
            ("Dataset access instructions", lambda r: r["scores"]["data"] >= 10),
            ("Error bars / confidence intervals", lambda r: r["scores"]["testing"] >= 10),
            ("License included", lambda r: r["scores"]["compliance"] >= 7),
        ],
    },
    "plos-one": {
        "name": "PLOS ONE Data Availability",
        "url": "https://journals.plos.org/plosone/s/data-availability",
        "criteria": [
            ("Data deposited in public repository", lambda r: r["scores"]["data"] >= 10),
            ("Data download instructions", lambda r: not any("download" in d.get("issue", "").lower() for d in r["_deductions"].get("data", []))),
            ("Code availability statement", lambda r: r["scores"]["env"] >= 7),
            ("Software versions specified", lambda r: r["scores"]["env"] >= 14),
            ("Analysis scripts provided", lambda r: r["scores"]["docs"] >= 11),
            ("Reproducible statistical analysis", lambda r: r["scores"]["seeds"] >= 14),
            ("License specified", lambda r: r["scores"]["compliance"] >= 7),
        ],
    },
}


def journal_checklist(result: dict, journal: str) -> dict:
    spec = JOURNAL_CHECKLISTS.get(journal.lower())
    if not spec:
        return {"error": f"Unknown journal: {journal}. Available: {list(JOURNAL_CHECKLISTS.keys())}"}

    checks = []
    passed = 0
    for name, test_fn in spec["criteria"]:
        try:
            ok = test_fn(result)
        except (KeyError, TypeError, IndexError):
            ok = False
        checks.append({"criterion": name, "passed": ok})
        if ok:
            passed += 1

    total = len(spec["criteria"])
    return {
        "journal": spec["name"],
        "url": spec["url"],
        "passed": passed,
        "total": total,
        "summary": f"Your repo satisfies {passed}/{total} {spec['name']} reproducibility criteria.",
        "missing": [c["criterion"] for c in checks if not c["passed"]],
        "checks": checks,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SciGate Audit Agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--path", help="Local repo directory to scan")
    group.add_argument("--github-repo", help="GitHub repo (owner/repo)")

    parser.add_argument("--ref",    default="main",    help="Git ref (branch/tag/SHA)")
    parser.add_argument("--sha",    default="unknown", help="Commit SHA for output")
    parser.add_argument("--trigger",default="push",
                        choices=["push", "tag", "slash_command", "schedule"])
    parser.add_argument("--out",    default="-",       help="Output file path (- = stdout)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    parser.add_argument("--journal", default=None,
                        choices=list(JOURNAL_CHECKLISTS.keys()),
                        help="Check against journal reproducibility requirements")
    parser.add_argument("--format", default="json", dest="output_format",
                        choices=["json", "reviewdog"],
                        help="Output format (json or reviewdog for inline PR annotations)")
    args = parser.parse_args()

    if args.path:
        reader = RepoReader(mode="local", path=args.path)
    else:
        reader = RepoReader(mode="github", repo=args.github_repo, ref=args.ref)

    result = audit(reader, commit_sha=args.sha, trigger=args.trigger)

    if args.journal:
        result["journal_checklist"] = journal_checklist(result, args.journal)

    if args.output_format == "reviewdog":
        diagnostics = []
        for fix in result["fixes"]:
            for filepath in fix.get("files", []):
                diagnostics.append({
                    "message": f"[SciGate] {fix['title']} ({fix['dimension']}, +{fix['points_recoverable']} pts): {fix.get('explanation', fix['claude_fix_hint'])}",
                    "location": {"path": filepath, "range": {"start": {"line": 1}}},
                    "severity": "WARNING",
                })
            if not fix.get("files"):
                diagnostics.append({
                    "message": f"[SciGate] {fix['title']} ({fix['dimension']}, +{fix['points_recoverable']} pts): {fix.get('explanation', fix['claude_fix_hint'])}",
                    "location": {"path": ".", "range": {"start": {"line": 1}}},
                    "severity": "WARNING",
                })
        rdjson = {"source": {"name": "scigate", "url": "https://github.com/parthassamal/SciGate", "version": "2.1.0"},
                  "severity": "WARNING", "diagnostics": diagnostics}
        print(json.dumps(rdjson))
    else:
        output = json.dumps(result, indent=2 if args.pretty else None)
        if args.out == "-":
            print(output)
        else:
            Path(args.out).write_text(output)
            print(f"Score written to {args.out}", file=sys.stderr)
            print(
                f"Domain: {result['domain']} | Score: {result['scores']['total']}/100 "
                f"| Grade: {result['grade']} | Projected: {result['projected_score']}/100 "
                f"({result['total_effort_label']}) | {result['scan_duration_ms']}ms",
                file=sys.stderr,
            )
