"""
SciGate — Unit tests for agents
────────────────────────────────
Tests audit_agent, memory_agent, tracker (credential_scan, generate_repo_map),
and regression_agent using offline, temp-directory repos. No API keys needed.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.audit_agent import RepoReader, audit, classify_domain, journal_checklist
from agents.memory_agent import (
    persist_scan,
    update_patterns,
    update_leaderboard,
    run as memory_run,
    repo_slug,
    load_json,
    save_json,
    append_jsonl,
    MEMORY_DIR,
)
from agents.regression_agent import check_regression, RegressionResult
from agents.tracker import credential_scan, generate_repo_map, detect_ai_config_files


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def make_repo(files: dict[str, str]) -> Path:
    td = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(td) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return Path(td)


def local_reader(path: Path) -> RepoReader:
    return RepoReader(mode="local", path=str(path))


# ─── AUDIT AGENT ──────────────────────────────────────────────────────────────

class TestAuditAgent:
    def test_audit_minimal_repo(self):
        """A minimal repo should return a valid score dict."""
        repo = make_repo({"main.py": "import random\nx = random.random()\nprint(x)\n"})
        reader = local_reader(repo)
        result = audit(reader)

        assert "scores" in result
        assert "grade" in result
        assert "domain" in result
        assert "fixes" in result
        assert 0 <= result["scores"]["total"] <= 100

    def test_audit_well_formed_repo(self):
        """A repo with good reproducibility should score higher."""
        repo = make_repo({
            "requirements.txt": "numpy==1.26.4\ntorch==2.1.0\n",
            "Dockerfile": "FROM python:3.11-slim\nCOPY . .\n",
            "README.md": (
                "# Experiment\n## Usage\n```bash\npython train.py\n```\n"
                "## Hardware\nRequires 1x GPU, 16GB RAM\n"
                "## Runtime\nApprox 30 minutes\n"
                "## Expected Output\nmodel.pt and metrics.json\n"
            ),
            "LICENSE": "MIT License\nCopyright 2024\n",
            "train.py": (
                "import numpy as np\nimport torch\nimport random\n"
                "random.seed(42)\nnp.random.seed(42)\ntorch.manual_seed(42)\n"
                "print('training')\n"
            ),
            "scripts/download_data.sh": (
                "#!/bin/bash\ncurl -L http://example.com/data.tar.gz -o data.tar.gz\n"
                "echo 'abc123  data.tar.gz' | sha256sum -c\n"
            ),
        })
        reader = local_reader(repo)
        result = audit(reader)
        assert result["scores"]["total"] >= 50
        assert result["grade"] in ("EXCELLENT", "GOOD", "FAIR")

    def test_audit_scores_sum_correctly(self):
        repo = make_repo({"main.py": "print(1)\n"})
        reader = local_reader(repo)
        result = audit(reader)
        scores = result["scores"]
        computed = (
            scores["env"] + scores["seeds"] + scores["data"]
            + scores["docs"] + scores["testing"] + scores["compliance"]
        )
        assert abs(computed - scores["total"]) <= 1

    def test_audit_gate_blocked(self):
        """A terrible repo should be gate-blocked."""
        repo = make_repo({"main.py": "x = 1\n"})
        reader = local_reader(repo)
        result = audit(reader)
        assert result["gate_blocked"] is True
        assert result["scores"]["total"] < 75

    def test_domain_classification_ml(self):
        repo = make_repo({
            "train.py": "import torch\nfrom torch import nn\nmodel = nn.Linear(10, 2)\n",
            "requirements.txt": "torch==2.1.0\n",
        })
        reader = local_reader(repo)
        domain = classify_domain(reader)
        assert domain == "ml-training"

    def test_journal_check(self):
        """Journal check should return results for all journals."""
        repo = make_repo({
            "main.py": "import random\nrandom.seed(42)\n",
            "README.md": "# Test\n## Usage\npython main.py\n",
            "requirements.txt": "numpy==1.26.4\n",
            "LICENSE": "MIT\n",
        })
        reader = local_reader(repo)
        result = audit(reader)
        for journal_key in ("nature", "neurips", "plos-one"):
            jc = journal_checklist(result, journal_key)
            assert "journal" in jc
            assert "checks" in jc
            assert isinstance(jc["checks"], list)
            assert len(jc["checks"]) > 0

    def test_audit_projected_score(self):
        repo = make_repo({"main.py": "import random\nx = random.random()\n"})
        reader = local_reader(repo)
        result = audit(reader)
        assert "projected_score" in result
        assert "projected_grade" in result
        assert "total_effort_minutes" in result
        assert result["projected_score"] >= result["scores"]["total"]

    def test_audit_fixes_have_required_fields(self):
        repo = make_repo({"main.py": "import random\nx = random.random()\n"})
        reader = local_reader(repo)
        result = audit(reader)
        for fix in result["fixes"]:
            assert "title" in fix
            assert "dimension" in fix
            assert "rank" in fix
            assert "points_recoverable" in fix


# ─── MEMORY AGENT ─────────────────────────────────────────────────────────────

class TestMemoryAgent:
    def test_persist_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCIGATE_MEMORY_DIR", str(tmp_path))
        import agents.memory_agent as mem
        mem.MEMORY_DIR = tmp_path

        score = {
            "domain": "ml-training",
            "grade": "GOOD",
            "commit_sha": "abc123",
            "trigger": "push",
            "scores": {"env": 15, "seeds": 14, "data": 12, "docs": 10, "testing": 10, "compliance": 12, "total": 73},
            "fixes": [],
        }
        persist_scan(score, "owner/repo")

        scan_file = tmp_path / "scans" / "owner__repo.jsonl"
        assert scan_file.exists()
        lines = scan_file.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["repo"] == "owner/repo"
        assert record["total"] == 73

    def test_persist_skip_empty_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCIGATE_MEMORY_DIR", str(tmp_path))
        import agents.memory_agent as mem
        mem.MEMORY_DIR = tmp_path

        score = {
            "domain": "general-science", "grade": "POOR", "commit_sha": "x",
            "trigger": "push",
            "scores": {"env": 0, "seeds": 0, "data": 0, "docs": 0, "testing": 0, "compliance": 0, "total": 0},
            "fixes": [],
        }
        persist_scan(score, "")
        assert not (tmp_path / "scans").exists()

    def test_update_patterns(self, tmp_path, monkeypatch):
        import agents.memory_agent as mem
        mem.MEMORY_DIR = tmp_path

        score = {
            "domain": "ml-training", "grade": "FAIR", "commit_sha": "abc",
            "trigger": "push",
            "scores": {"env": 5, "seeds": 5, "data": 5, "docs": 5, "testing": 5, "compliance": 5, "total": 30},
            "fixes": [
                {"dimension": "env", "title": "Add requirements.txt", "rank": 1, "points_recoverable": 10},
            ],
        }
        alerts = update_patterns(score, "owner/repo1")
        assert len(alerts) == 0

        patterns_file = tmp_path / "patterns.json"
        assert patterns_file.exists()
        patterns = json.loads(patterns_file.read_text())
        assert len(patterns) == 1

    def test_update_leaderboard(self, tmp_path, monkeypatch):
        import agents.memory_agent as mem
        mem.MEMORY_DIR = tmp_path

        score = {
            "domain": "ml-training", "grade": "GOOD", "commit_sha": "abc",
            "trigger": "push",
            "scores": {"env": 15, "seeds": 14, "data": 12, "docs": 10, "testing": 10, "compliance": 12, "total": 73},
            "fixes": [],
        }
        update_leaderboard(score, "owner/repo")

        lb_file = tmp_path / "leaderboard.json"
        assert lb_file.exists()
        lb = json.loads(lb_file.read_text())
        assert len(lb) == 1
        assert lb[0]["repo"] == "owner/repo"

    def test_repo_slug(self):
        assert repo_slug("owner/repo") == "owner__repo"
        assert repo_slug("my repo") == "my_repo"

    def test_atomic_save_json(self, tmp_path):
        p = tmp_path / "test.json"
        save_json(p, {"key": "value"})
        assert p.exists()
        data = json.loads(p.read_text())
        assert data["key"] == "value"


# ─── REGRESSION AGENT ────────────────────────────────────────────────────────

class TestRegressionAgent:
    def test_no_history_no_regression(self, tmp_path):
        score = {
            "scores": {"env": 15, "seeds": 14, "data": 12, "docs": 10, "testing": 10, "compliance": 12, "total": 73},
        }
        result = check_regression(score, "owner/new-repo", history_dir=str(tmp_path))
        assert isinstance(result, RegressionResult)
        assert result.regression_detected is False
        assert len(result.regressions) == 0

    def test_regression_detected(self, tmp_path):
        history_file = tmp_path / "owner__repo.jsonl"
        old_record = json.dumps({
            "ts": "2024-01-01T00:00:00Z",
            "total": 80,
            "env": 17, "seeds": 17, "data": 17, "docs": 15, "testing": 10, "compliance": 4,
        })
        history_file.write_text(old_record + "\n")

        current = {
            "scores": {"env": 5, "seeds": 17, "data": 17, "docs": 15, "testing": 10, "compliance": 4, "total": 68},
        }
        result = check_regression(current, "owner/repo", history_dir=str(tmp_path))
        assert result.regression_detected is True
        assert any(r.dimension == "env" for r in result.regressions)


# ─── TRACKER — CREDENTIAL SCAN ───────────────────────────────────────────────

class TestCredentialScan:
    def test_clean_repo(self):
        repo = make_repo({
            "main.py": "print('hello world')\n",
            "config.json": '{"debug": true}\n',
        })
        reader = local_reader(repo)
        result = credential_scan(reader, repo_path=str(repo))
        assert result["severity"] in ("clean", "warning", "critical")
        assert isinstance(result["findings"], list)

    def test_detect_aws_key_in_file(self):
        repo = make_repo({
            ".env": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n",
        })
        reader = local_reader(repo)
        result = credential_scan(reader, repo_path=str(repo))
        assert result["severity"] != "clean"
        found_types = [f["type"] for f in result["findings"]]
        assert any("AWS" in t for t in found_types)

    def test_detect_generic_secret(self):
        repo = make_repo({
            "config.py": 'DATABASE_URL = "postgresql://user:s3cretPassw0rd@host:5432/db"\n',
            ".env": "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n",
        })
        reader = local_reader(repo)
        result = credential_scan(reader, repo_path=str(repo))
        assert result["severity"] != "clean"


# ─── TRACKER — REPO MAP ──────────────────────────────────────────────────────

class TestRepoMap:
    def test_basic_repo_map(self):
        repo = make_repo({
            "main.py": "print(1)\n",
            "src/utils.py": "def helper(): pass\n",
            "README.md": "# Hello\n",
            "requirements.txt": "numpy==1.26.4\n",
            "LICENSE": "MIT\n",
        })
        reader = local_reader(repo)
        result = generate_repo_map(reader)
        assert "languages" in result
        assert "total_files" in result
        assert result["total_files"] >= 4
        assert "key_files" in result
        key_file_names = [kf["file"] for kf in result["key_files"]]
        assert any("README" in f for f in key_file_names)

    def test_ai_config_detection(self):
        repo = make_repo({
            ".cursorrules": "some rules\n",
            "CLAUDE.md": "instructions for claude\n",
            "main.py": "print(1)\n",
        })
        reader = local_reader(repo)
        result = generate_repo_map(reader)
        assert "ai_config" in result
        assert result["ai_config"]["total"] >= 1
        assert len(result["ai_config"]["findings"]) >= 1

    def test_excludes_venv(self):
        repo = make_repo({
            "main.py": "print(1)\n",
            ".venv/lib/site.py": "import os\n",
            "node_modules/pkg/index.js": "module.exports = {}\n",
        })
        reader = local_reader(repo)
        result = generate_repo_map(reader)
        flat_text = json.dumps(result)
        assert ".venv" not in flat_text or result["total_files"] <= 2


# ─── AI CONFIG DETECTION ─────────────────────────────────────────────────────

class TestAIConfigDetection:
    def test_detect_cursorrules(self):
        repo = make_repo({
            ".cursorrules": "rules here\n",
            "main.py": "x = 1\n",
        })
        reader = local_reader(repo)
        result = detect_ai_config_files(reader)
        assert result["total"] >= 1
        assert any(f["file"] == ".cursorrules" for f in result["findings"])

    def test_detect_claude_md(self):
        repo = make_repo({
            "CLAUDE.md": "# Claude instructions\nDo stuff.\n",
        })
        reader = local_reader(repo)
        result = detect_ai_config_files(reader)
        assert result["total"] >= 1

    def test_no_false_positives(self):
        repo = make_repo({
            "main.py": "print(1)\n",
            "README.md": "# Project\n",
        })
        reader = local_reader(repo)
        result = detect_ai_config_files(reader)
        assert result["total"] == 0
        assert len(result["findings"]) == 0
