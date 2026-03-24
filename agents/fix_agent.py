"""
SciGate — Agent 2: Fix Generator
---------------------------------
Reads the audit score object from Agent 1, calls the Anthropic API (Claude)
to generate targeted reproducibility fixes, writes the patched files, and
opens a draft MR via the GitLab API.

Usage (called by GitLab Duo Flow, or directly):
    python fix_agent.py --score-json score.json --project-id 1234

Environment variables required:
    ANTHROPIC_API_KEY   - Anthropic API key
    GITLAB_TOKEN        - GitLab personal/project access token
    GITLAB_URL          - e.g. https://gitlab.com
"""

import os
import json
import argparse
import textwrap
import time
from pathlib import Path
from typing import Any

import anthropic
import httpx

# ── CONFIG ────────────────────────────────────────────────────────────────────

ANTHROPIC_MODEL  = "claude-sonnet-4-20250514"
MAX_TOKENS       = 4096
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
            "Add it as a masked GitLab CI variable."
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


# ── CLAUDE FIX CALL ───────────────────────────────────────────────────────────

def generate_fix(
    client: anthropic.Anthropic,
    domain: str,
    fix: dict[str, Any],
    file_contents: dict[str, str],
) -> dict[str, Any]:
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

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS,
        system=build_skill_context(domain, fix),
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Claude returned non-JSON for fix '{fix['title']}': {exc}\n"
            f"Raw (first 400 chars): {raw[:400]}"
        ) from exc


# ── SAFETY CHECK ──────────────────────────────────────────────────────────────

def is_protected(path: str) -> bool:
    lower = path.lower()
    return any(p in lower for p in PROTECTED_PATTERNS)


# ── GITLAB API ────────────────────────────────────────────────────────────────

class GitLabClient:
    def __init__(self, project_id: str):
        self.base = os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/")
        self.token = os.environ.get("GITLAB_TOKEN", "")
        self.project_id = project_id
        self.headers = {
            "PRIVATE-TOKEN": self.token,
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(headers=self.headers, timeout=30)

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v4/projects/{self.project_id}/{path}"

    def get_file(self, path: str, ref: str = "main") -> str | None:
        import base64
        r = self._client.get(
            self._url(f"repository/files/{path.replace('/', '%2F')}"),
            params={"ref": ref},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return base64.b64decode(r.json()["content"]).decode()

    def create_branch(self, branch: str, ref: str = "main") -> None:
        self._client.post(
            self._url("repository/branches"),
            json={"branch": branch, "ref": ref},
        ).raise_for_status()

    def commit_files(
        self, branch: str, message: str,
        actions: list[dict[str, str]],
    ) -> None:
        self._client.post(
            self._url("repository/commits"),
            json={
                "branch": branch,
                "commit_message": message,
                "actions": actions,
            },
        ).raise_for_status()

    def create_mr(
        self, source_branch: str, title: str, description: str,
    ) -> dict[str, Any]:
        r = self._client.post(
            self._url("merge_requests"),
            json={
                "source_branch": source_branch,
                "target_branch": "main",
                "title": title,
                "description": description,
                "labels": "scigate,reproducibility",
                "draft": True,
                "remove_source_branch": True,
            },
        )
        r.raise_for_status()
        return r.json()


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────────

def run(score_json: dict[str, Any], project_id: str) -> dict[str, Any]:
    client = get_anthropic_client()
    gitlab = GitLabClient(project_id)
    domain = score_json["domain"]
    fixes = score_json["fixes"]
    sha = score_json["commit_sha"]
    score_before = score_json["scores"]["total"]

    print(f"[SciGate Agent 2] Domain: {domain} | Score: {score_before}/100")
    print(f"[SciGate Agent 2] Processing {len(fixes)} fixes...")

    all_file_actions: list[dict[str, str]] = []
    mr_notes: list[str] = []
    total_points_projected = score_before

    for fix in fixes:
        print(f"  -> Fix {fix['rank']}: {fix['title']} (+{fix['points_recoverable']} pts)")

        file_contents: dict[str, str] = {}
        for path in fix["files"]:
            content = gitlab.get_file(path, ref=sha)
            if content:
                file_contents[path] = content

        try:
            fix_result = generate_fix(client, domain, fix, file_contents)
        except Exception as exc:
            print(f"    ! Claude fix generation failed: {exc}")
            continue

        for file_change in fix_result.get("files", []):
            path = file_change["path"]
            if is_protected(path):
                print(f"    X Skipped protected file: {path}")
                continue

            action = "update" if path in file_contents else "create"
            all_file_actions.append({
                "action": action,
                "file_path": path,
                "content": file_change["content"],
            })
            print(f"    + Staged {action}: {path}")

        mr_notes.append(f"- {fix_result.get('mr_note', fix['title'])}")
        total_points_projected += fix_result.get("points_recovered", 0)
        time.sleep(0.5)

    if not all_file_actions:
        print("[SciGate Agent 2] No safe file changes generated.")
        return {"status": "no_changes", "score_before": score_before}

    branch = f"scigate/fix-{sha[:8]}"
    score_projected = min(total_points_projected, 100)

    print(f"\n[SciGate Agent 2] Creating branch: {branch}")
    gitlab.create_branch(branch, ref=sha)

    commit_message = (
        f"SciGate: +{score_projected - score_before} pts reproducibility fixes\n\n"
        + "\n".join(mr_notes)
    )
    gitlab.commit_files(branch, commit_message, all_file_actions)

    gate_cleared = score_projected >= GATE_THRESHOLD
    mr_description = textwrap.dedent(f"""
        ## SciGate automated reproducibility fixes

        | | Before | Projected |
        |---|---|---|
        | Score | {score_before}/100 | **{score_projected}/100** |
        | Gate status | {'🟢 Clear' if score_before >= GATE_THRESHOLD else '🔴 Blocked'} | {'✅ Projected score clears threshold.' if gate_cleared else f'⚠️ Still below threshold ({score_projected} < {GATE_THRESHOLD}).'} |

        ### Changes applied
        {chr(10).join(mr_notes)}

        ### What was NOT changed
        All experiment logic, model architecture, training code, and loss functions
        are completely untouched. This MR only adds reproducibility infrastructure.

        ---
        _Generated by [SciGate](https://scigate.io) · Powered by Claude {ANTHROPIC_MODEL}_
        _Commit: `{sha}` · Domain: `{domain}`_
    """).strip()

    mr = gitlab.create_mr(
        source_branch=branch,
        title=f"SciGate: +{score_projected - score_before} pts — "
              f"{len(all_file_actions)} files updated",
        description=mr_description,
    )

    print(f"\n[SciGate Agent 2] MR created: {mr.get('web_url', 'unknown')}")
    print(f"[SciGate Agent 2] Score: {score_before} -> {score_projected} (projected)")

    return {
        "status": "mr_created",
        "mr_url": mr.get("web_url"),
        "mr_iid": mr.get("iid"),
        "branch": branch,
        "score_before": score_before,
        "score_projected": score_projected,
        "files_changed": len(all_file_actions),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SciGate Agent 2 — Fix generator")
    parser.add_argument("--score-json", required=True, help="Path to Agent 1 score output JSON")
    parser.add_argument("--project-id", required=True, help="GitLab project ID")
    args = parser.parse_args()

    with open(args.score_json) as f:
        score = json.load(f)

    result = run(score, args.project_id)
    print("\n-- Result --")
    print(json.dumps(result, indent=2))
