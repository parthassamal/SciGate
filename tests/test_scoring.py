"""Tests for audit, scoring, memory, and repo scanner — all run offline, no API key."""

import tempfile
from pathlib import Path

from scigate.agents.audit import run_audit, SciField, classify_field
from scigate.agents.memory import OrgMemory
from scigate.scoring.engine import compute_score
from scigate.scoring.badge import badge_url, badge_markdown, score_summary_markdown
from scigate.utils.repo_scanner import scan_repo


def _make_repo(files: dict[str, str]) -> Path:
    """Create a temp directory with the given files. Returns the path."""
    td = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(td) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return Path(td)


# ── Audit + Scoring ──────────────────────────────────────────────────────────

def test_perfect_repo():
    """A repo with everything done right should score high."""
    repo = _make_repo({
        "requirements.txt": "numpy==1.26.4\ntorch==2.1.0\n",
        "Dockerfile": "FROM python@sha256:abc123\nCOPY . .\n",
        "README.md": "# Project\n## Usage\n```bash\npython train.py\n```\n"
                     "## Hardware\nRequires 1x A100 GPU, 32GB RAM\n"
                     "## Runtime\nTakes about 2 hours\n"
                     "## Results\nProduces accuracy metrics in results/\n",
        "LICENSE": "MIT License\n",
        "train.py": "import numpy as np\nimport torch\nimport random\n"
                    "random.seed(42)\nnp.random.seed(42)\ntorch.manual_seed(42)\n"
                    "torch.backends.cudnn.deterministic = True\n"
                    "print('training')\n",
        "scripts/download_data.sh": "#!/bin/bash\ncurl -L http://example.com/data.tar.gz\n"
                                    "echo 'abc123  data.tar.gz' | sha256sum -c\n",
    })
    report = run_audit(repo)
    assert report.total_score >= 75, f"Expected >=75, got {report.total_score}"
    assert report.field != SciField.GENERAL_SCIENCE


def test_bare_repo():
    """A repo with nothing should score very low."""
    repo = _make_repo({
        "main.py": "import random\nx = random.random()\nprint(x)\n",
    })
    report = run_audit(repo)
    assert report.total_score < 50, f"Expected <50, got {report.total_score}"


def test_unpinned_deps_deduction():
    repo = _make_repo({
        "requirements.txt": "numpy\ntorch\nscipy\n",
        "main.py": "import numpy\n",
        "README.md": "# Test\n## Usage\npython main.py\n",
    })
    report = run_audit(repo)
    env_dim = report.dimensions["env"]
    assert env_dim.score < 25
    check_ids = [f.check_id for f in env_dim.findings]
    assert "ENV-002" in check_ids


def test_hardcoded_paths_deduction():
    repo = _make_repo({
        "load_data.py": 'data = open("/home/researcher/data/train.csv")\n',
        "requirements.txt": "pandas==2.0.0\n",
        "README.md": "# Test\n",
    })
    report = run_audit(repo)
    data_dim = report.dimensions["data"]
    assert data_dim.score < 25
    check_ids = [f.check_id for f in data_dim.findings]
    assert "DATA-001" in check_ids


def test_ml_field_classification():
    repo = _make_repo({
        "train.py": "import torch\nfrom torch import nn\nmodel = nn.Linear(10, 2)\n"
                    "optimizer = torch.optim.Adam(model.parameters(), lr=0.001)\n"
                    "for epoch in range(10):\n    loss = model(torch.randn(5,10)).sum()\n",
        "requirements.txt": "torch==2.1.0\n",
    })
    snap = scan_repo(repo)
    field, confidence = classify_field(snap)
    assert field == SciField.ML_TRAINING
    assert confidence > 0.5


def test_bio_field_classification():
    repo = _make_repo({
        "pipeline.sh": "samtools sort -o aligned.bam input.bam\nbwa mem ref.fa reads.fastq\n",
        "environment.yml": "name: bio\ndependencies:\n  - samtools=1.17\n",
    })
    snap = scan_repo(repo)
    field, confidence = classify_field(snap)
    assert field in (SciField.BIOINFORMATICS, SciField.GENOMICS)


def test_scoring_engine():
    repo = _make_repo({
        "main.py": "print('hello')\n",
        "README.md": "# Hello\n## Usage\npython main.py\n",
        "requirements.txt": "click==8.0.0\n",
    })
    report = run_audit(repo)
    sc = compute_score(report)
    assert 0 <= sc.total_score <= 100
    assert sc.grade in ("EXCELLENT", "GOOD", "FAIR", "POOR", "CRITICAL")
    assert sc.env >= 0
    assert sc.seeds >= 0
    assert sc.data >= 0
    assert sc.docs >= 0


def test_four_dimensions_sum_to_total():
    repo = _make_repo({
        "main.py": "import random\nx = random.random()\n",
    })
    report = run_audit(repo)
    summed = report.env_score + report.seeds_score + report.data_score + report.docs_score
    assert abs(summed - report.total_score) < 0.1


def test_to_dict_contract():
    """The JSON output matches the contract the dashboard expects."""
    repo = _make_repo({
        "main.py": "import numpy as np\nnp.random.seed(42)\n",
        "requirements.txt": "numpy==1.26.4\n",
        "README.md": "# Proj\n## Usage\npython main.py\n## Results\nOutputs table.\n",
    })
    report = run_audit(repo)
    d = report.to_dict()
    assert "scores" in d
    assert "env" in d["scores"]
    assert "seeds" in d["scores"]
    assert "data" in d["scores"]
    assert "docs" in d["scores"]
    assert "total" in d["scores"]
    assert "domain" in d
    assert "grade" in d
    assert "fixes" in d
    assert "gate_blocked" in d


# ── Badge ────────────────────────────────────────────────────────────────────

def test_badge_url():
    repo = _make_repo({"main.py": "print(1)\n"})
    report = run_audit(repo)
    sc = compute_score(report)
    url = badge_url(sc)
    assert "img.shields.io" in url


def test_score_summary_markdown():
    repo = _make_repo({"main.py": "print(1)\n"})
    report = run_audit(repo)
    sc = compute_score(report)
    md = score_summary_markdown(sc)
    assert "SciGate" in md
    assert "Environment" in md


# ── Memory ───────────────────────────────────────────────────────────────────

def test_memory_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "mem.json"
        mem = OrgMemory.load(path)
        assert len(mem.entries) == 0

        mem.record("setup.py", "unpinned-deps", "pin versions", 15.0, "ml-training")
        assert len(mem.entries) == 1
        assert mem.entries[0].confidence > 0.5

        mem2 = OrgMemory.load(path)
        assert len(mem2.entries) == 1

        mem2.record("setup.py", "unpinned-deps", "pin v2", 10.0, "ml-training")
        assert len(mem2.entries) == 1
        assert mem2.entries[0].occurrences == 2


# ── Repo Scanner ─────────────────────────────────────────────────────────────

def test_repo_scanner():
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "main.py").write_text("import torch\nprint('hello')\n")
        (Path(td) / "requirements.txt").write_text("torch==2.1.0\n")

        snap = scan_repo(td)
        assert snap.total_lines > 0
        assert "python" in snap.languages
        assert len(snap.files) >= 2
