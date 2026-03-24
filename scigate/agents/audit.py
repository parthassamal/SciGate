"""Agent 1 — Audit Agent.

Classifies the scientific field of a repository using heuristics (no API
required), then runs domain-tuned reproducibility checks across four
dimensions: Environment, Seeds, Data Provenance, Documentation.

Each dimension scores 0-25. Total = sum of four dimensions, capped 0-100.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from scigate.utils.repo_scanner import RepoSnapshot, scan_repo


# ── Scientific field taxonomy ────────────────────────────────────────────────

class SciField(str, Enum):
    ML_TRAINING = "ml-training"
    ML_INFERENCE = "ml-inference"
    BIOINFORMATICS = "bioinformatics"
    CLIMATE_MODEL = "climate-model"
    COMPUTATIONAL_CHEMISTRY = "computational-chemistry"
    PHYSICS_SIMULATION = "physics-simulation"
    STATISTICS = "statistics"
    DATA_ANALYSIS = "data-analysis"
    NEUROSCIENCE = "neuroscience"
    GENOMICS = "genomics"
    GENERAL_SCIENCE = "general-science"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass
class Finding:
    check_id: str
    title: str
    severity: Severity
    description: str
    dimension: str = ""          # env | seeds | data | docs
    points_deducted: float = 0   # how many points this costs in its dimension
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    suggestion: Optional[str] = None


@dataclass
class DimensionScore:
    name: str
    score: float          # 0-25
    max_score: float = 25
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class AuditReport:
    repo_path: str
    field: SciField
    field_confidence: float
    dimensions: dict[str, DimensionScore] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    languages: dict[str, int] = field(default_factory=dict)

    @property
    def total_score(self) -> float:
        return max(0.0, min(100.0, sum(d.score for d in self.dimensions.values())))

    @property
    def env_score(self) -> float:
        return self.dimensions.get("env", DimensionScore("env", 0)).score

    @property
    def seeds_score(self) -> float:
        return self.dimensions.get("seeds", DimensionScore("seeds", 0)).score

    @property
    def data_score(self) -> float:
        return self.dimensions.get("data", DimensionScore("data", 0)).score

    @property
    def docs_score(self) -> float:
        return self.dimensions.get("docs", DimensionScore("docs", 0)).score

    def to_dict(self) -> dict:
        return {
            "repo_path": self.repo_path,
            "domain": self.field.value,
            "field": self.field.value,
            "field_confidence": self.field_confidence,
            "files_scanned": self.files_scanned,
            "languages": self.languages,
            "scores": {
                "env": round(self.env_score, 1),
                "seeds": round(self.seeds_score, 1),
                "data": round(self.data_score, 1),
                "docs": round(self.docs_score, 1),
                "total": round(self.total_score, 1),
            },
            "grade": _grade(self.total_score),
            "gate_blocked": self.total_score < 75,
            "gate_threshold": 75,
            "findings": [
                {
                    "check_id": f.check_id,
                    "title": f.title,
                    "severity": f.severity.value,
                    "dimension": f.dimension,
                    "points_deducted": f.points_deducted,
                    "description": f.description,
                    "file_path": f.file_path,
                    "line_number": f.line_number,
                    "suggestion": f.suggestion,
                }
                for f in self.findings
            ],
            "fixes": [
                {
                    "rank": i + 1,
                    "title": f.title,
                    "files": [f.file_path] if f.file_path else [],
                    "dimension": f.dimension,
                    "points_recoverable": f.points_deducted,
                    "claude_fix_hint": f.suggestion or "",
                }
                for i, f in enumerate(
                    sorted(self.findings, key=lambda x: -x.points_deducted)[:5]
                )
            ],
        }


def _grade(score: float) -> str:
    if score >= 90: return "EXCELLENT"
    if score >= 75: return "GOOD"
    if score >= 50: return "FAIR"
    if score >= 25: return "POOR"
    return "CRITICAL"


# ── Heuristic field classification (no API needed) ──────────────────────────

_FIELD_SIGNALS: list[tuple[SciField, list[str], float]] = [
    (SciField.ML_TRAINING, [
        r"import torch", r"from torch", r"import tensorflow", r"from tensorflow",
        r"import keras", r"from keras", r"import jax", r"from flax",
        r"\.train\(", r"\.fit\(", r"DataLoader", r"Trainer\(",
        r"model\.parameters", r"optimizer\.", r"loss\.",
        r"epoch", r"batch_size", r"learning_rate",
    ], 0.85),
    (SciField.ML_INFERENCE, [
        r"\.predict\(", r"\.eval\(", r"inference", r"onnx",
        r"torch\.load", r"model\.load",
    ], 0.7),
    (SciField.BIOINFORMATICS, [
        r"samtools", r"bwa\s", r"STAR\b", r"hisat2", r"bowtie",
        r"\.fastq", r"\.bam", r"\.vcf", r"\.bed\b",
        r"bioconductor", r"bioconda", r"snakemake", r"nextflow",
        r"GRCh38|hg38|hg19|GRCm39",
    ], 0.85),
    (SciField.GENOMICS, [
        r"genome", r"sequencing", r"alignment", r"variant.?call",
        r"NCBI", r"SRA", r"GEO\b", r"Ensembl",
    ], 0.75),
    (SciField.CLIMATE_MODEL, [
        r"netCDF|netcdf", r"\.nc\b", r"xarray", r"cdo\b",
        r"CESM|WRF|GFDL|CMIP", r"climate", r"atmospheric",
        r"\.f90\b", r"MPI_Init", r"mpi4py",
    ], 0.85),
    (SciField.NEUROSCIENCE, [
        r"nilearn", r"mne\b", r"\.nii", r"fMRI|EEG|MEG",
        r"brain", r"neural.?imaging", r"FreeSurfer",
    ], 0.8),
    (SciField.STATISTICS, [
        r"library\(lme4\)|library\(fixest\)|library\(plm\)",
        r"statsmodels", r"scipy\.stats", r"p[._]?value",
        r"confidence.?interval", r"regression",
    ], 0.7),
    (SciField.DATA_ANALYSIS, [
        r"import pandas", r"import polars", r"\.csv\b",
        r"seaborn|matplotlib|plotly", r"jupyter",
    ], 0.6),
    (SciField.COMPUTATIONAL_CHEMISTRY, [
        r"rdkit", r"openbabel", r"SMILES", r"molecular",
        r"gaussian|orca|vasp", r"\.xyz\b",
    ], 0.8),
    (SciField.PHYSICS_SIMULATION, [
        r"simulation", r"finite.?element", r"FEM\b",
        r"openfoam", r"LAMMPS", r"particle",
    ], 0.75),
]


def classify_field(snap: RepoSnapshot) -> tuple[SciField, float]:
    """Classify scientific field using pattern matching on file contents."""
    all_text = "\n".join(
        content[:5000] for content in list(snap.file_contents.values())[:30]
    )
    file_names = " ".join(snap.file_list)
    search_corpus = all_text + "\n" + file_names

    scores: dict[SciField, float] = {}
    for sci_field, patterns, weight in _FIELD_SIGNALS:
        hits = sum(1 for p in patterns if re.search(p, search_corpus, re.IGNORECASE))
        if hits >= 1:
            raw = min(hits / 4, 1.0) * weight
            scores[sci_field] = raw

    if not scores:
        return SciField.GENERAL_SCIENCE, 0.3

    best = max(scores, key=scores.get)
    return best, min(scores[best] + 0.1, 0.95)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _check_file_exists(snap: RepoSnapshot, names: list[str]) -> Optional[str]:
    lower_files = {f.lower(): f for f in snap.file_list}
    for name in names:
        if name.lower() in lower_files:
            return lower_files[name.lower()]
    return None


def _grep(snap: RepoSnapshot, pattern: str, extensions: list[str] | None = None) -> list[tuple[str, int, str]]:
    compiled = re.compile(pattern, re.IGNORECASE)
    hits: list[tuple[str, int, str]] = []
    for fpath, content in snap.file_contents.items():
        if extensions and not any(fpath.endswith(ext) for ext in extensions):
            continue
        for i, line in enumerate(content.splitlines(), 1):
            if compiled.search(line):
                hits.append((fpath, i, line.strip()))
    return hits


def _readme_content(snap: RepoSnapshot) -> str:
    for name in ["README.md", "README.rst", "README.txt", "README"]:
        found = _check_file_exists(snap, [name])
        if found and found in snap.file_contents:
            return snap.file_contents[found]
    return ""


# ── Dimension: Environment (0-25) ───────────────────────────────────────────

def _score_environment(snap: RepoSnapshot, field: SciField) -> DimensionScore:
    dim = DimensionScore(name="env", score=25)

    has_req = _check_file_exists(snap, ["requirements.txt"])
    has_env_yml = _check_file_exists(snap, ["environment.yml", "environment.yaml", "conda-lock.yml"])
    has_pyproject = _check_file_exists(snap, ["pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "poetry.lock"])
    has_dockerfile = _check_file_exists(snap, ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"])

    has_any_env = has_req or has_env_yml or has_pyproject
    if not has_any_env:
        f = Finding(
            check_id="ENV-001", title="No environment file found",
            severity=Severity.CRITICAL, dimension="env", points_deducted=15,
            description="No requirements.txt, environment.yml, pyproject.toml, or equivalent.",
            suggestion="Create requirements.txt with pinned versions for all dependencies.",
        )
        dim.findings.append(f)
        dim.score -= 15
        dim.notes.append("No environment file.")
    else:
        if has_req and has_req in snap.file_contents:
            lines = snap.file_contents[has_req].splitlines()
            unpinned = [
                l for l in lines
                if l.strip() and not l.startswith("#") and not l.startswith("-")
                and "==" not in l
            ]
            if unpinned:
                n = len(unpinned)
                f = Finding(
                    check_id="ENV-002", title=f"{n} unpinned dependencies",
                    severity=Severity.HIGH, dimension="env", points_deducted=10,
                    description=f"{n} deps lack == pins: {', '.join(unpinned[:3])}{'...' if n > 3 else ''}",
                    file_path=has_req,
                    suggestion="Pin every dep: `numpy==1.26.4` not just `numpy`.",
                )
                dim.findings.append(f)
                dim.score -= 10
                dim.notes.append(f"{n} unpinned deps.")
            else:
                dim.notes.append("All deps pinned.")

    if not has_dockerfile:
        f = Finding(
            check_id="ENV-003", title="No Dockerfile or container definition",
            severity=Severity.MEDIUM, dimension="env", points_deducted=5,
            description="No Dockerfile found. Environment must be manually reconstructed.",
            suggestion="Add a Dockerfile that builds the full analysis environment.",
        )
        dim.findings.append(f)
        dim.score -= 5
    else:
        docker_name = has_dockerfile
        if docker_name and docker_name in snap.file_contents:
            content = snap.file_contents[docker_name]
            if re.search(r"FROM\s+\S+(?<!@sha256:\S{64})\s*$", content, re.MULTILINE):
                if "sha256:" not in content:
                    f = Finding(
                        check_id="ENV-004", title="Dockerfile base image not SHA-pinned",
                        severity=Severity.LOW, dimension="env", points_deducted=3,
                        description="Base image uses a mutable tag, not a SHA digest.",
                        file_path=docker_name,
                        suggestion="Pin with: FROM python@sha256:<digest>",
                    )
                    dim.findings.append(f)
                    dim.score -= 3

    uses_cuda = bool(_grep(snap, r"torch\.cuda|cuda|gpu|nvidia", [".py", ".yml", ".yaml", ".md"]))
    if uses_cuda and not _grep(snap, r"cuda.?version|cudatoolkit|nvidia.?driver|cu\d{2,3}", [".py", ".yml", ".yaml", ".txt", ".md", ".cfg"]):
        f = Finding(
            check_id="ENV-005", title="CUDA version not specified",
            severity=Severity.LOW, dimension="env", points_deducted=3,
            description="Code references CUDA/GPU but no CUDA version is documented.",
            suggestion="Specify CUDA version in requirements or Dockerfile.",
        )
        dim.findings.append(f)
        dim.score -= 3

    if field == SciField.BIOINFORMATICS and has_env_yml and has_dockerfile:
        dim.score = min(25, dim.score + 3)
        dim.notes.append("Bonus: conda env + container.")

    dim.score = max(0, min(25, dim.score))
    return dim


# ── Dimension: Seeds (0-25) ──────────────────────────────────────────────────

def _score_seeds(snap: RepoSnapshot, field: SciField) -> DimensionScore:
    dim = DimensionScore(name="seeds", score=25)

    py_exts = [".py"]
    r_exts = [".r", ".R"]
    jl_exts = [".jl"]

    experiment_files = []
    for fpath in snap.file_list:
        lower = fpath.lower()
        if any(kw in lower for kw in ["train", "experiment", "eval", "run", "main", "test_"]):
            if any(fpath.endswith(e) for e in py_exts + r_exts + jl_exts):
                experiment_files.append(fpath)

    if not experiment_files:
        experiment_files = [f for f in snap.file_list if any(f.endswith(e) for e in py_exts)]

    py_seed_patterns = [
        r"(np|numpy)\.random\.seed",
        r"random\.seed",
        r"torch\.manual_seed",
        r"tf\.random\.set_seed|tensorflow\.random\.set_seed",
        r"PYTHONHASHSEED",
        r"seed_everything|set_random_seed|pl\.seed_everything",
    ]
    r_seed_patterns = [r"set\.seed\("]
    jl_seed_patterns = [r"Random\.seed!"]

    unseeded_files = []
    for fpath in experiment_files:
        if fpath not in snap.file_contents:
            continue
        content = snap.file_contents[fpath]
        has_randomness = bool(re.search(
            r"random|np\.random|torch\.|shuffle|sample|dropout|noise",
            content, re.IGNORECASE
        ))
        if not has_randomness:
            continue

        has_seed = False
        if any(fpath.endswith(e) for e in py_exts):
            has_seed = any(re.search(p, content) for p in py_seed_patterns)
        elif any(fpath.endswith(e) for e in r_exts):
            has_seed = any(re.search(p, content) for p in r_seed_patterns)
        elif any(fpath.endswith(e) for e in jl_exts):
            has_seed = any(re.search(p, content) for p in jl_seed_patterns)

        if not has_seed:
            unseeded_files.append(fpath)

    for fpath in unseeded_files[:5]:
        f = Finding(
            check_id="SEED-001", title=f"Unseeded random usage in {fpath.split('/')[-1]}",
            severity=Severity.HIGH, dimension="seeds", points_deducted=5,
            description=f"Random/stochastic operations without seed setting in {fpath}.",
            file_path=fpath,
            suggestion="Add np.random.seed(42), random.seed(42), torch.manual_seed(42) at top of file.",
        )
        dim.findings.append(f)
        dim.score -= 5

    if field in (SciField.ML_TRAINING, SciField.ML_INFERENCE):
        uses_torch = bool(_grep(snap, r"import torch|from torch", py_exts))
        has_cudnn_det = bool(_grep(snap, r"cudnn\.deterministic|use_deterministic_algorithms", py_exts))
        if uses_torch and not has_cudnn_det:
            f = Finding(
                check_id="SEED-002", title="CUDA non-determinism not addressed",
                severity=Severity.MEDIUM, dimension="seeds", points_deducted=5,
                description="PyTorch used without cudnn.deterministic or use_deterministic_algorithms.",
                suggestion="Add: torch.backends.cudnn.deterministic = True",
            )
            dim.findings.append(f)
            dim.score -= 5

        has_dataloader = bool(_grep(snap, r"DataLoader\(", py_exts))
        has_worker_seed = bool(_grep(snap, r"worker_init_fn|generator=", py_exts))
        if has_dataloader and not has_worker_seed:
            f = Finding(
                check_id="SEED-003", title="DataLoader without worker seed control",
                severity=Severity.LOW, dimension="seeds", points_deducted=3,
                description="Multi-worker DataLoader may produce non-deterministic batches.",
                suggestion="Pass worker_init_fn and generator to DataLoader.",
            )
            dim.findings.append(f)
            dim.score -= 3

    dim.score = max(0, min(25, dim.score))
    return dim


# ── Dimension: Data Provenance (0-25) ────────────────────────────────────────

def _score_data(snap: RepoSnapshot, field: SciField) -> DimensionScore:
    dim = DimensionScore(name="data", score=25)

    code_exts = [".py", ".r", ".R", ".jl", ".sh"]
    path_hits = _grep(snap, r'["\'/](home|Users|mnt|data|scratch|tmp)/\w+', code_exts)
    unique_paths = set()
    for fpath, lineno, line in path_hits:
        unique_paths.add(fpath)

    for fpath in list(unique_paths)[:5]:
        hit = next((h for h in path_hits if h[0] == fpath), None)
        f = Finding(
            check_id="DATA-001", title=f"Hardcoded absolute path in {fpath.split('/')[-1]}",
            severity=Severity.HIGH, dimension="data", points_deducted=5,
            description=f"Absolute path breaks portability: {hit[2][:80] if hit else ''}",
            file_path=fpath,
            line_number=hit[1] if hit else None,
            suggestion="Use relative paths or os.path.join(os.path.dirname(__file__), ...)",
        )
        dim.findings.append(f)
        dim.score -= 5

    has_download = (
        _check_file_exists(snap, ["download_data.sh", "download_data.py", "get_data.sh", "get_data.py"])
        or _check_file_exists(snap, ["scripts/download_data.sh", "scripts/download.sh", "scripts/get_data.py"])
        or _check_file_exists(snap, ["Makefile"])
        or bool(_grep(snap, r"(curl|wget|download|fetch).*data", [".sh", ".py", ".md"]))
    )
    if not has_download:
        f = Finding(
            check_id="DATA-002", title="No data download or generation script",
            severity=Severity.HIGH, dimension="data", points_deducted=10,
            description="No script to acquire the input data. Reproducer must guess where to get it.",
            suggestion="Add scripts/download_data.sh with URLs, or document data source in data/README.md.",
        )
        dim.findings.append(f)
        dim.score -= 10

    has_checksums = bool(_grep(snap, r"sha256|md5|checksum|hash.*verify", [".py", ".sh", ".md", ".yml"]))
    if not has_checksums:
        f = Finding(
            check_id="DATA-003", title="No data checksums or integrity verification",
            severity=Severity.MEDIUM, dimension="data", points_deducted=5,
            description="No checksums to verify downloaded data matches the original.",
            suggestion="Add SHA-256 checksums for all data files in data/README.md.",
        )
        dim.findings.append(f)
        dim.score -= 5

    data_extensions = [".csv", ".tsv", ".parquet", ".h5", ".hdf5", ".npy", ".npz", ".pkl"]
    committed_data = [f for f in snap.file_list if any(f.endswith(ext) for ext in data_extensions)]
    if committed_data and not _check_file_exists(snap, ["data/README.md", "DATA.md"]):
        f = Finding(
            check_id="DATA-004", title="Raw data committed without provenance documentation",
            severity=Severity.LOW, dimension="data", points_deducted=3,
            description=f"{len(committed_data)} data files in repo without data/README.md.",
            suggestion="Add data/README.md documenting source, version, and license of each dataset.",
        )
        dim.findings.append(f)
        dim.score -= 3

    dim.score = max(0, min(25, dim.score))
    return dim


# ── Dimension: Documentation (0-25) ─────────────────────────────────────────

def _score_docs(snap: RepoSnapshot, field: SciField) -> DimensionScore:
    dim = DimensionScore(name="docs", score=25)

    readme = _readme_content(snap)

    if not readme:
        f = Finding(
            check_id="DOC-001", title="No README file",
            severity=Severity.CRITICAL, dimension="docs", points_deducted=15,
            description="No README — reproducer has zero guidance on how to run this code.",
            suggestion="Add README.md with installation, data setup, and execution instructions.",
        )
        dim.findings.append(f)
        dim.score -= 15
        dim.score = max(0, dim.score)
        return dim

    readme_lower = readme.lower()

    has_run_instructions = any(kw in readme_lower for kw in [
        "how to run", "usage", "getting started", "quick start",
        "python ", "bash ", "run the", "execute", "reproduc",
        "```bash", "```sh", "```python",
    ])
    if not has_run_instructions:
        f = Finding(
            check_id="DOC-002", title="No step-by-step run instructions in README",
            severity=Severity.HIGH, dimension="docs", points_deducted=8,
            description="README exists but has no clear instructions for running the code.",
            suggestion="Add a 'Getting Started' or 'Usage' section with exact commands.",
        )
        dim.findings.append(f)
        dim.score -= 8

    has_hardware = any(kw in readme_lower for kw in [
        "gpu", "cuda", "ram", "memory", "hardware", "a100", "v100",
        "cpu", "cores", "storage", "disk",
    ])
    if not has_hardware:
        f = Finding(
            check_id="DOC-003", title="Hardware requirements not specified",
            severity=Severity.MEDIUM, dimension="docs", points_deducted=6,
            description="No mention of GPU model, RAM, or storage requirements.",
            suggestion="Add hardware requirements section (e.g., 'Requires 1x A100 GPU, 32GB RAM').",
        )
        dim.findings.append(f)
        dim.score -= 6

    has_runtime = any(kw in readme_lower for kw in [
        "runtime", "takes about", "approximately", "hours", "minutes",
        "wall.?time", "expected.?time", "training.?time",
    ])
    if not has_runtime:
        f = Finding(
            check_id="DOC-004", title="No estimated runtime provided",
            severity=Severity.MEDIUM, dimension="docs", points_deducted=5,
            description="Reproducer can't plan resources without knowing how long this takes.",
            suggestion="Add: 'Expected runtime: ~2 hours on 1x A100 GPU.'",
        )
        dim.findings.append(f)
        dim.score -= 5

    has_outputs = any(kw in readme_lower for kw in [
        "output", "result", "expected", "reproduce", "figure",
        "table", "metric", "accuracy", "score",
    ])
    if not has_outputs:
        f = Finding(
            check_id="DOC-005", title="No description of expected outputs",
            severity=Severity.LOW, dimension="docs", points_deducted=4,
            description="No description of what results a successful run should produce.",
            suggestion="Document expected outputs (e.g., 'Produces results/ with Table 1 metrics.').",
        )
        dim.findings.append(f)
        dim.score -= 4

    if not _check_file_exists(snap, ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"]):
        f = Finding(
            check_id="DOC-006", title="No license file",
            severity=Severity.LOW, dimension="docs", points_deducted=2,
            description="Unclear whether this code can legally be reused.",
            suggestion="Add a LICENSE file (MIT, Apache-2.0, BSD-3, etc.).",
        )
        dim.findings.append(f)
        dim.score -= 2

    dim.score = max(0, min(25, dim.score))
    return dim


# ── Public entry point ───────────────────────────────────────────────────────

def run_audit(repo_path: str | Path, *, memory_hints: list[dict] | None = None) -> AuditReport:
    """Run the full audit pipeline. No API key required."""
    snap = scan_repo(repo_path)
    sci_field, confidence = classify_field(snap)

    env_dim = _score_environment(snap, sci_field)
    seeds_dim = _score_seeds(snap, sci_field)
    data_dim = _score_data(snap, sci_field)
    docs_dim = _score_docs(snap, sci_field)

    all_findings = env_dim.findings + seeds_dim.findings + data_dim.findings + docs_dim.findings

    if memory_hints:
        for hint in memory_hints:
            pattern = hint.get("repo_pattern", "")
            if pattern and any(pattern.lower() in f.lower() for f in snap.file_list):
                all_findings.append(Finding(
                    check_id="MEM-001",
                    title=f"Org memory: {hint.get('repro_failure_type', 'unknown')}",
                    severity=Severity.INFO, dimension="data", points_deducted=0,
                    description=f"Previously seen (confidence {hint.get('confidence', 0):.0%}). "
                                f"Fix: {hint.get('fix_applied', 'N/A')}",
                    suggestion=hint.get("fix_applied"),
                ))

    all_findings.sort(key=lambda f: -f.points_deducted)

    return AuditReport(
        repo_path=str(repo_path),
        field=sci_field,
        field_confidence=confidence,
        dimensions={
            "env": env_dim,
            "seeds": seeds_dim,
            "data": data_dim,
            "docs": docs_dim,
        },
        findings=all_findings,
        files_scanned=len(snap.files),
        languages=snap.languages,
    )
