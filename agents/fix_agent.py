"""
SciGate — Agent 2: Fix Generator
---------------------------------
Reads the audit score object from Agent 1, calls the Anthropic API (Claude)
to generate targeted reproducibility fixes, writes the patched files, and
opens a draft PR via the GitHub API.

Usage:
    python fix_agent.py --score-json score.json --repo owner/repo

Environment variables required:
    ANTHROPIC_API_KEY   - Anthropic API key
    GITHUB_TOKEN        - GitHub personal access token
"""

import os
import sys
import ast
import json
import argparse
import logging
import textwrap
import time
from typing import Any

import anthropic
import httpx

logger = logging.getLogger("scigate.fix")

# ── CONFIG ────────────────────────────────────────────────────────────────────

ANTHROPIC_MODEL  = os.getenv("SCIGATE_FIX_MODEL", "claude-sonnet-4-20250514")
MAX_TOKENS       = 4096
MAX_FIX_TURNS    = int(os.getenv("SCIGATE_FIX_MAX_TURNS", "3"))
GATE_THRESHOLD   = int(os.getenv("SCIGATE_THRESHOLD", "75"))

PROTECTED_PATTERNS = [
    "train", "model", "loss", "network", "arch",
    "backbone", "head", "encoder", "decoder",
]

# ── FIX TEMPLATES ─────────────────────────────────────────────────────────────

FIX_TEMPLATES = {
    "seed_injection": {
        "ml-training": textwrap.dedent("""\
            import random
            import numpy as np
            import torch

            SEED = 42
            random.seed(SEED)
            np.random.seed(SEED)
            torch.manual_seed(SEED)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(SEED)
                torch.backends.cudnn.deterministic = True
                torch.backends.cudnn.benchmark = False
        """),
        "general-science": textwrap.dedent("""\
            import random
            import numpy as np

            SEED = 42
            random.seed(SEED)
            np.random.seed(SEED)
        """),
        "bioinformatics": "set.seed(42)  # R",
    },
    "dockerfile_sha_note": (
        "# Pin the base image SHA for full reproducibility:\n"
        "# Run: docker pull python:3.11-slim && "
        "docker inspect --format='{{.Id}}' python:3.11-slim\n"
        "# Then replace 'python:3.11-slim' with 'python@sha256:<digest>'\n"
    ),
    "data_readme": textwrap.dedent("""\
        # Dataset

        ## Source
        <!-- Where does this data come from? URL, paper, version. -->

        ## Download
        ```bash
        bash scripts/download_data.sh
        ```

        ## Checksums
        | File | SHA-256 |
        |---|---|
        | data/train.csv | `<run: sha256sum data/train.csv>` |

        ## License
        <!-- Dataset license here -->
    """),
    "download_script": textwrap.dedent("""\
        #!/usr/bin/env bash
        set -euo pipefail
        DATA_DIR="${1:-data}"
        mkdir -p "$DATA_DIR"

        echo "Downloading dataset..."
        # curl -L "https://your-data-source.example.com/dataset.tar.gz" -o "$DATA_DIR/dataset.tar.gz"

        echo "Verifying checksum..."
        # echo "<expected-sha256>  $DATA_DIR/dataset.tar.gz" | sha256sum -c

        echo "Done. Data is in $DATA_DIR/"
    """),
}


# ── ANTHROPIC CLIENT ──────────────────────────────────────────────────────────

def get_anthropic_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set. "
            "Add it as a repository secret in GitHub Actions."
        )
    return anthropic.Anthropic(api_key=api_key)


# ── SKILL CONTEXT ─────────────────────────────────────────────────────────────

def build_skill_context(domain: str, fix: dict[str, Any]) -> str:
    seed_template = FIX_TEMPLATES["seed_injection"].get(
        domain, FIX_TEMPLATES["seed_injection"]["general-science"]
    )
    return textwrap.dedent(f"""
        You are SciGate's fix generation engine for a **{domain}** repository.

        ## Your role
        Generate a minimal, targeted code fix for one specific reproducibility issue.
        You write only what is necessary. You never rewrite working logic.

        ## Domain: {domain}
        {'ML training repos need torch.manual_seed, numpy.random.seed, and CUDA determinism flags.' if domain == 'ml-training' else ''}
        {'Bioinformatics repos need R set.seed(), conda envs with exact tool versions, and reference genome version docs.' if domain == 'bioinformatics' else ''}
        {'Climate model repos need compiler flag documentation, MPI version pins, and input data checksums.' if domain == 'climate-model' else ''}

        ## Fix being generated
        Dimension : {fix['dimension']}
        Title     : {fix['title']}
        Files     : {', '.join(fix['files'])}
        Points    : +{fix['points_recoverable']} if fixed
        Hint      : {fix['claude_fix_hint']}

        ## Seed template for this domain
        ```python
        {seed_template}
        ```

        ## Output format - return exactly this JSON:
        {{
          "fix_type": "seed_injection|path_fix|env_update|docs_update|new_file",
          "files": [
            {{
              "path": "relative/path/to/file.py",
              "action": "modify|create",
              "content": "FULL file content after fix (not a diff)",
              "explanation": "One sentence: what changed and why"
            }}
          ],
          "mr_note": "One sentence suitable for an MR description bullet point",
          "points_recovered": {fix['points_recoverable']}
        }}

        ## Hard rules
        - "content" must be the COMPLETE file, not a diff or snippet.
        - Never touch files matching: {PROTECTED_PATTERNS}
        - For path fixes: replace absolute paths with os.path.join(os.path.dirname(__file__), '..', 'data', 'filename')
        - For seed injection: add seeds at the TOP of the file, after imports, before any logic.
        - Return ONLY valid JSON.
    """).strip()


# ── PATCH VALIDATION ──────────────────────────────────────────────────────────

def validate_python_patch(content: str, path: str) -> str | None:
    """Validate a Python file patch. Returns None if valid, error string if not."""
    if not path.endswith(".py"):
        return None
    try:
        ast.parse(content, filename=path)
        return None
    except SyntaxError as exc:
        return f"SyntaxError at line {exc.lineno}: {exc.msg}"


def validate_fix_result(fix_result: dict) -> list[str]:
    """Validate all files in a fix result. Returns list of error descriptions."""
    errors = []
    for file_change in fix_result.get("files", []):
        path = file_change.get("path", "")
        content = file_change.get("content", "")
        err = validate_python_patch(content, path)
        if err:
            errors.append(f"{path}: {err}")
    return errors


# ── CLAUDE FIX CALL ───────────────────────────────────────────────────────────

def _parse_claude_json(raw: str) -> dict:
    """Extract JSON from Claude's response, handling markdown fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def generate_fix(
    client: anthropic.Anthropic,
    domain: str,
    fix: dict[str, Any],
    file_contents: dict[str, str],
) -> dict[str, Any]:
    """Generate a fix with multi-turn validation (up to MAX_FIX_TURNS attempts)."""
    file_context_parts = []
    for path in fix["files"]:
        if path in file_contents:
            file_context_parts.append(
                f'<file path="{path}">\n{file_contents[path]}\n</file>'
            )
        else:
            file_context_parts.append(
                f'<file path="{path}" status="not_found">'
                f'File does not exist yet - create it.</file>'
            )

    user_message = (
        "Here are the current file contents:\n\n"
        + "\n\n".join(file_context_parts)
        + "\n\nGenerate the fix. Return only JSON."
    )

    messages = [{"role": "user", "content": user_message}]
    system_prompt = build_skill_context(domain, fix)

    for turn in range(MAX_FIX_TURNS):
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=messages,
        )

        raw = response.content[0].text.strip()
        try:
            fix_result = _parse_claude_json(raw)
        except json.JSONDecodeError as exc:
            if turn < MAX_FIX_TURNS - 1:
                logger.info("Turn %d: Claude returned invalid JSON, requesting correction", turn + 1)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                    f"Your response was not valid JSON. Error: {exc}\n"
                    "Please return ONLY valid JSON matching the schema."
                })
                continue
            raise ValueError(
                f"Claude returned non-JSON after {MAX_FIX_TURNS} attempts for '{fix['title']}': {exc}"
            ) from exc

        validation_errors = validate_fix_result(fix_result)
        if not validation_errors:
            if turn > 0:
                logger.info("Turn %d: fix validated successfully after refinement", turn + 1)
            return fix_result

        if turn < MAX_FIX_TURNS - 1:
            logger.info("Turn %d: validation errors found, requesting correction: %s", turn + 1, "; ".join(validation_errors))
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                "The generated code has syntax errors that must be fixed:\n\n"
                + "\n".join(f"- {e}" for e in validation_errors) +
                "\n\nPlease regenerate the fix with corrected Python syntax. "
                "Return only valid JSON."
            })
        else:
            logger.warning("Fix '%s' has validation errors after %d turns: %s",
                           fix["title"], MAX_FIX_TURNS, "; ".join(validation_errors))
            return fix_result

    return fix_result


# ── SAFETY CHECK ──────────────────────────────────────────────────────────────

def is_protected(path: str) -> bool:
    lower = path.lower()
    return any(p in lower for p in PROTECTED_PATTERNS)


# ── GITHUB API ────────────────────────────────────────────────────────────────

class GitHubClient:
    def __init__(self, repo: str):
        self.base = os.environ.get(
            "GITHUB_API_URL", "https://api.github.com"
        ).rstrip("/")
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.repo = repo
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.Client(headers=self.headers, timeout=30)

    def _url(self, path: str) -> str:
        return f"{self.base}/repos/{self.repo}/{path}"

    def get_file(self, path: str, ref: str = "main") -> str | None:
        import base64
        r = self._client.get(
            self._url(f"contents/{path}"),
            params={"ref": ref},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode()
        return data.get("content", "")

    def _get_ref_sha(self, ref: str = "main") -> str:
        r = self._client.get(self._url(f"git/ref/heads/{ref}"))
        r.raise_for_status()
        return r.json()["object"]["sha"]

    def create_branch(self, branch: str, ref: str = "main") -> None:
        sha = self._get_ref_sha(ref)
        self._client.post(
            self._url("git/refs"),
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        ).raise_for_status()

    def commit_files(
        self, branch: str, message: str,
        actions: list[dict[str, str]],
    ) -> None:
        import base64 as b64
        branch_sha = self._get_ref_sha(branch)

        r = self._client.get(self._url(f"git/commits/{branch_sha}"))
        r.raise_for_status()
        base_tree_sha = r.json()["tree"]["sha"]

        tree_items = []
        for action in actions:
            tree_items.append({
                "path": action["file_path"],
                "mode": "100644",
                "type": "blob",
                "content": action["content"],
            })

        r = self._client.post(
            self._url("git/trees"),
            json={"base_tree": base_tree_sha, "tree": tree_items},
        )
        r.raise_for_status()
        new_tree_sha = r.json()["sha"]

        r = self._client.post(
            self._url("git/commits"),
            json={
                "message": message,
                "tree": new_tree_sha,
                "parents": [branch_sha],
            },
        )
        r.raise_for_status()
        new_commit_sha = r.json()["sha"]

        self._client.patch(
            self._url(f"git/refs/heads/{branch}"),
            json={"sha": new_commit_sha},
        ).raise_for_status()

    def create_pr(
        self, head_branch: str, title: str, description: str,
        base_branch: str = "main",
    ) -> dict[str, Any]:
        r = self._client.post(
            self._url("pulls"),
            json={
                "head": head_branch,
                "base": base_branch,
                "title": title,
                "body": description,
                "draft": True,
            },
        )
        r.raise_for_status()
        return r.json()


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────────

def run(score_json: dict[str, Any], repo: str) -> dict[str, Any]:
    client = get_anthropic_client()
    github = GitHubClient(repo)
    domain = score_json["domain"]
    fixes = score_json["fixes"]
    sha = score_json["commit_sha"]
    score_before = score_json["scores"]["total"]

    logger.info("Domain: %s | Score: %d/100", domain, score_before)
    logger.info("Processing %d fixes...", len(fixes))

    all_file_actions: list[dict[str, str]] = []
    pr_notes: list[str] = []
    total_points_projected = score_before

    for fix in fixes:
        logger.info("Fix %d: %s (+%d pts)", fix["rank"], fix["title"], fix["points_recoverable"])

        file_contents: dict[str, str] = {}
        fetch_ref = sha if sha and sha != "unknown" else "main"
        for path in fix["files"]:
            content = github.get_file(path, ref=fetch_ref)
            if content:
                file_contents[path] = content

        try:
            fix_result = generate_fix(client, domain, fix, file_contents)
        except Exception as exc:
            logger.warning("Claude fix generation failed: %s", exc)
            continue

        for file_change in fix_result.get("files", []):
            path = file_change["path"]
            if is_protected(path):
                logger.info("Skipped protected file: %s", path)
                continue

            action = "update" if path in file_contents else "create"
            all_file_actions.append({
                "action": action,
                "file_path": path,
                "content": file_change["content"],
            })
            logger.info("Staged %s: %s", action, path)

        pr_notes.append(f"- {fix_result.get('mr_note', fix['title'])}")
        total_points_projected += fix_result.get("points_recovered", 0)
        time.sleep(0.5)

    if not all_file_actions:
        logger.info("No safe file changes generated.")
        return {"status": "no_changes", "score_before": score_before}

    branch = f"scigate/fix-{sha[:8]}"
    score_projected = min(total_points_projected, 100)

    logger.info("Creating branch: %s", branch)
    github.create_branch(branch, ref="main")

    commit_message = (
        f"SciGate: +{score_projected - score_before} pts reproducibility fixes\n\n"
        + "\n".join(pr_notes)
    )
    github.commit_files(branch, commit_message, all_file_actions)

    gate_cleared = score_projected >= GATE_THRESHOLD
    pr_description = textwrap.dedent(f"""
        ## SciGate automated reproducibility fixes

        | | Before | Projected |
        |---|---|---|
        | Score | {score_before}/100 | **{score_projected}/100** |
        | Gate status | {'🟢 Clear' if score_before >= GATE_THRESHOLD else '🔴 Blocked'} | {'✅ Projected score clears threshold.' if gate_cleared else f'⚠️ Still below threshold ({score_projected} < {GATE_THRESHOLD}).'} |

        ### Changes applied
        {chr(10).join(pr_notes)}

        ### What was NOT changed
        All experiment logic, model architecture, training code, and loss functions
        are completely untouched. This PR only adds reproducibility infrastructure.

        ---
        _Generated by [SciGate](https://scigate.io) · Powered by Claude {ANTHROPIC_MODEL}_
        _Commit: `{sha}` · Domain: `{domain}`_
    """).strip()

    pr = github.create_pr(
        head_branch=branch,
        title=f"SciGate: +{score_projected - score_before} pts — "
              f"{len(all_file_actions)} files updated",
        description=pr_description,
    )

    logger.info("PR created: %s", pr.get("html_url", "unknown"))
    logger.info("Score: %d -> %d (projected)", score_before, score_projected)

    return {
        "status": "pr_created",
        "pr_url": pr.get("html_url"),
        "pr_number": pr.get("number"),
        "branch": branch,
        "score_before": score_before,
        "score_projected": score_projected,
        "files_changed": len(all_file_actions),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SciGate Agent 2 — Fix generator")
    parser.add_argument("--score-json", required=True, help="Path to Agent 1 score output JSON")
    parser.add_argument("--repo", required=True, help="GitHub repo (owner/repo)")
    args = parser.parse_args()

    try:
        with open(args.score_json) as f:
            score = json.load(f)
    except FileNotFoundError:
        print(f"Error: file not found: {args.score_json}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {args.score_json}: {e}")
        sys.exit(1)

    result = run(score, args.repo)
    print("\n-- Result --")
    print(json.dumps(result, indent=2))
