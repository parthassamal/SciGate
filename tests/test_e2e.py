"""
SciGate — End-to-end integration tests
────────────────────────────────────────
Simulates the full pipeline: audit → memory → regression chained together,
plus FastAPI endpoint tests using TestClient. No API keys needed.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.audit_agent import RepoReader, audit
from agents.memory_agent import run as memory_run, repo_slug, MEMORY_DIR
from agents.regression_agent import check_regression


class TestFullPipeline:
    """Simulate the full audit → memory → regression pipeline on a temp repo."""

    @pytest.fixture
    def repo_with_issues(self, tmp_path):
        """Minimal repo that should score significantly lower than well_formed_repo."""
        (tmp_path / "train.py").write_text(
            "import torch\nimport numpy as np\n"
            "x = np.random.randn(10)\nmodel = torch.nn.Linear(10, 1)\n"
            'data = np.load("/home/user/data/train.npy")\n'
        )
        return tmp_path

    @pytest.fixture
    def well_formed_repo(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("numpy==1.26.4\ntorch==2.1.0\nscipy==1.11.4\n")
        (tmp_path / "environment.yml").write_text("name: myenv\ndependencies:\n  - numpy=1.26.4\n  - torch=2.1.0\n")
        (tmp_path / "README.md").write_text(
            "# My Project\n\n## How to run\n```bash\npython train.py\n```\n"
            "## Hardware\nGPU: NVIDIA A100 80GB\n## Expected runtime\n2 hours on A100\n"
            "## Expected outputs\nModel checkpoint saved to `outputs/model.pt`\n"
            "## Citation\nDoe et al., 2024. My Paper. arXiv:2401.00001\n"
        )
        (tmp_path / "train.py").write_text(
            "import os\nimport torch\nimport numpy as np\nimport random\n\n"
            "SEED = 42\nrandom.seed(SEED)\nnp.random.seed(SEED)\n"
            "torch.manual_seed(SEED)\n"
            "torch.cuda.manual_seed_all(SEED)\n"
            "os.environ['PYTHONHASHSEED'] = str(SEED)\n\n"
            "model = torch.nn.Linear(10, 1)\n"
        )
        (tmp_path / "LICENSE").write_text("MIT License\nCopyright (c) 2024\n")
        (tmp_path / "Dockerfile").write_text("FROM python:3.11-slim\nCOPY . /app\n")
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "download_data.sh").write_text("#!/bin/bash\ncurl -O https://example.com/data.tar.gz\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_basic.py").write_text("def test_pass(): assert True\n")
        (tests / "test_model.py").write_text("def test_model_shape():\n    assert 10 > 0\n")
        return tmp_path

    @pytest.fixture
    def memory_dir(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "scans").mkdir()
        (mem / "leaderboard.json").write_text("[]")
        (mem / "patterns.json").write_text("{}")
        return mem

    def test_pipeline_audit_memory_regression(self, repo_with_issues, memory_dir, monkeypatch):
        """Full pipeline: audit a repo, persist to memory, check for regression."""
        monkeypatch.setenv("SCIGATE_MEMORY_DIR", str(memory_dir))
        import agents.memory_agent as mm
        monkeypatch.setattr(mm, "MEMORY_DIR", memory_dir)

        reader = RepoReader(mode="local", path=str(repo_with_issues))
        score = audit(reader, commit_sha="abc123", trigger="api")

        assert "scores" in score
        assert "domain" in score
        assert score["scores"]["total"] <= 100
        assert score["grade"] in ("EXCELLENT", "GOOD", "FAIR", "POOR", "CRITICAL")

        repo_name = "test-org/test-repo"
        mem_result = memory_run(score, repo_name)
        assert mem_result is not None

        scan_file = memory_dir / "scans" / f"{repo_slug(repo_name)}.jsonl"
        assert scan_file.exists()
        lines = [l for l in scan_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["repo"] == repo_name
        assert record["total"] == score["scores"]["total"]

        reg = check_regression(score, repo_name, history_dir=str(memory_dir / "scans"))
        assert reg.regression_detected is False
        assert "No previous scans" not in reg.message

    def test_regression_detected_on_score_drop(self, well_formed_repo, memory_dir, monkeypatch, tmp_path):
        """Detect regression when score drops between two scans."""
        monkeypatch.setenv("SCIGATE_MEMORY_DIR", str(memory_dir))
        import agents.memory_agent as mm
        monkeypatch.setattr(mm, "MEMORY_DIR", memory_dir)

        repo_name = "test-org/regression-test"

        reader1 = RepoReader(mode="local", path=str(well_formed_repo))
        score1 = audit(reader1, commit_sha="good123", trigger="api")
        memory_run(score1, repo_name)

        bad_repo = tmp_path / "bad_repo"
        bad_repo.mkdir()
        (bad_repo / "main.py").write_text("import numpy\nx = numpy.random.randn(5)\n")

        reader2 = RepoReader(mode="local", path=str(bad_repo))
        score2 = audit(reader2, commit_sha="bad456", trigger="api")

        assert score2["scores"]["total"] < score1["scores"]["total"], \
            f"Bad repo ({score2['scores']['total']}) should score lower than good repo ({score1['scores']['total']})"

        reg = check_regression(score2, repo_name, history_dir=str(memory_dir / "scans"))
        assert reg.regression_detected is True
        assert reg.score_delta < 0
        assert len(reg.regressions) > 0

    def test_pipeline_idempotent_memory(self, repo_with_issues, memory_dir, monkeypatch):
        """Running memory twice creates two history records, leaderboard has one entry."""
        monkeypatch.setenv("SCIGATE_MEMORY_DIR", str(memory_dir))
        import agents.memory_agent as mm
        monkeypatch.setattr(mm, "MEMORY_DIR", memory_dir)

        reader = RepoReader(mode="local", path=str(repo_with_issues))
        score = audit(reader, commit_sha="run1", trigger="api")
        repo_name = "test-org/idempotent"

        memory_run(score, repo_name)
        memory_run(score, repo_name)

        scan_file = memory_dir / "scans" / f"{repo_slug(repo_name)}.jsonl"
        lines = [l for l in scan_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 2

        lb = json.loads((memory_dir / "leaderboard.json").read_text())
        repo_entries = [e for e in lb if e.get("repo") == repo_name]
        assert len(repo_entries) == 1

    def test_credential_scan_in_pipeline(self, repo_with_issues, monkeypatch):
        """Credential scan integrates with the audit pipeline."""
        from agents.tracker import credential_scan

        (repo_with_issues / ".env").write_text(
            "AWS_KEY=AKIAIOSFODNN7EXAMPLE\n"
            "GITHUB=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234\n"
        )

        reader = RepoReader(mode="local", path=str(repo_with_issues))
        result = credential_scan(reader, repo_path=str(repo_with_issues))

        assert result["severity"] in ("critical", "high", "medium", "warning", "clean")
        assert result["total_findings"] > 0

    def test_repo_map_in_pipeline(self, well_formed_repo):
        """Repo map generation works as part of the pipeline."""
        from agents.tracker import generate_repo_map

        reader = RepoReader(mode="local", path=str(well_formed_repo))
        result = generate_repo_map(reader)

        assert result["total_files"] > 0
        assert len(result["languages"]) > 0
        assert len(result["key_files"]) > 0
        assert any(kf["file"] == "LICENSE" for kf in result["key_files"])


class TestFixAgentValidation:
    """Test the fix agent's validation logic without calling Claude."""

    def test_validate_valid_python(self):
        from agents.fix_agent import validate_python_patch
        code = "import os\nx = 1\nprint(x)\n"
        assert validate_python_patch(code, "test.py") is None

    def test_validate_invalid_python(self):
        from agents.fix_agent import validate_python_patch
        code = "def foo(\n  x = 1\n"
        err = validate_python_patch(code, "test.py")
        assert err is not None
        assert "SyntaxError" in err

    def test_validate_non_python_skipped(self):
        from agents.fix_agent import validate_python_patch
        assert validate_python_patch("not valid python {{{", "readme.md") is None

    def test_validate_fix_result_clean(self):
        from agents.fix_agent import validate_fix_result
        result = {
            "files": [
                {"path": "utils.py", "content": "x = 1\n"},
                {"path": "readme.md", "content": "# Hello\n"},
            ]
        }
        assert validate_fix_result(result) == []

    def test_validate_fix_result_with_errors(self):
        from agents.fix_agent import validate_fix_result
        result = {
            "files": [
                {"path": "good.py", "content": "x = 1\n"},
                {"path": "bad.py", "content": "def foo(\n"},
            ]
        }
        errors = validate_fix_result(result)
        assert len(errors) == 1
        assert "bad.py" in errors[0]

    def test_is_protected(self):
        from agents.fix_agent import is_protected
        assert is_protected("src/train.py") is True
        assert is_protected("model_config.py") is True
        assert is_protected("utils/helpers.py") is False
        assert is_protected("requirements.txt") is False


class TestAPIEndpoints:
    """Test FastAPI endpoints via TestClient."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.server import app
        return TestClient(app)

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "agents" in data
        assert data["agents"]["audit"] == "ready"

    def test_help(self, client):
        r = client.get("/v1/help")
        assert r.status_code == 200
        data = r.json()
        assert data["app"] == "SciGate"
        assert len(data["agents"]) >= 5
        assert len(data["endpoints"]) >= 5

    def test_leaderboard(self, client):
        r = client.get("/v1/leaderboard")
        assert r.status_code == 200
        data = r.json()
        assert "leaderboard" in data
        assert "top_patterns" in data

    def test_scan_missing_input(self, client):
        r = client.post("/v1/scan", json={})
        assert r.status_code == 422 or r.status_code == 400

    def test_scan_local_repo(self, client, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')\n")
        (tmp_path / "README.md").write_text("# Test\n")
        r = client.post("/v1/scan", json={
            "local_path": str(tmp_path),
            "ref": "main",
        })
        assert r.status_code == 200
        data = r.json()
        assert "scores" in data
        assert "grade" in data
        assert data["scores"]["total"] >= 0
        assert data["scores"]["total"] <= 100

    def test_scan_nonexistent_path(self, client):
        r = client.post("/v1/scan", json={
            "local_path": "/tmp/nonexistent_repo_scigate_test_xyz",
        })
        assert r.status_code in (404, 500)

    def test_policy_default(self, client):
        r = client.get("/v1/policy/test-tenant")
        assert r.status_code == 200
        data = r.json()
        assert "gate_threshold" in data
        assert data["tenant_id"] == "test-tenant"
