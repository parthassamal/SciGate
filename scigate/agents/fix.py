"""Agent 2 — Fix Agent (local version).

Takes an AuditReport and uses Claude with scientific-code reasoning skills
to propose concrete, targeted fixes for each finding.  Returns a FixPlan
with file contents and explanations.

Requires ANTHROPIC_API_KEY to be set.  If missing, generate_fix_plan()
raises an error with a clear message.  The audit agent and scoring engine
do NOT need this — only fix generation requires Claude.
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from scigate.agents.audit import AuditReport, Finding
from scigate.utils.claude_client import ask_claude
from scigate.utils.repo_scanner import scan_repo

PROTECTED_PATTERNS = [
    "train", "model", "loss", "network", "arch",
    "backbone", "head", "encoder", "decoder",
]

SEED_TEMPLATES = {
    "ml-training": textwrap.dedent("""\
        import random, numpy as np, torch
        SEED = 42
        random.seed(SEED)
        np.random.seed(SEED)
        torch.manual_seed(SEED)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(SEED)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
    """),
    "bioinformatics": "set.seed(42)  # R\n",
    "general-science": textwrap.dedent("""\
        import random, numpy as np
        SEED = 42
        random.seed(SEED)
        np.random.seed(SEED)
    """),
}


@dataclass
class FixAction:
    finding_id: str
    file_path: str
    action_type: str
    description: str
    diff: Optional[str] = None
    new_content: Optional[str] = None
    priority: int = 0


@dataclass
class FixPlan:
    repo_path: str
    field: str
    actions: list[FixAction] = field(default_factory=list)
    summary: str = ""
    estimated_score_impact: float = 0.0

    def to_dict(self) -> dict:
        return {
            "repo_path": self.repo_path,
            "field": self.field,
            "summary": self.summary,
            "estimated_score_impact": self.estimated_score_impact,
            "actions": [
                {
                    "finding_id": a.finding_id,
                    "file_path": a.file_path,
                    "action_type": a.action_type,
                    "description": a.description,
                    "diff": a.diff,
                    "new_content": a.new_content,
                    "priority": a.priority,
                }
                for a in self.actions
            ],
        }


SYSTEM_PROMPT = """\
You are an expert scientific-software engineer specializing in computational
reproducibility.  You understand:

- Why `requirements.txt` without pinned versions makes builds non-deterministic.
- Why hardcoded `/home/researcher/data` paths break portability.
- Why `torch.manual_seed` alone is insufficient without CUDA determinism flags.
- Why bioinformatics pipelines must specify reference genome builds and tool versions.
- Why statistical analyses without seed setting produce different p-values per run.

Your job: given reproducibility findings and file context, produce a structured
JSON fix plan.
"""

FIX_PROMPT = """\
Repository domain: {domain}
Repository path: {repo_path}

## Findings (sorted by points recoverable):

{findings_json}

## Relevant file contents:

{file_samples}

## Seed template for this domain:

```
{seed_template}
```

## Instructions:

Return a JSON object with this schema:

{{
  "summary": "1-2 sentence overview of all fixes",
  "estimated_score_impact": <float, total points recovered>,
  "actions": [
    {{
      "finding_id": "<check_id>",
      "file_path": "<relative path>",
      "action_type": "create | modify | add_config | document",
      "description": "<what this fix does>",
      "diff": "<unified diff for modify, or null>",
      "new_content": "<full file content for create, or null>",
      "priority": <1-5, 1=highest>
    }}
  ]
}}

Rules:
- Never touch files matching: {protected}
- For path fixes: use os.path.join(os.path.dirname(__file__), ...)
- For seed injection: add at TOP of file, after imports.
- Return ONLY valid JSON.
"""


def generate_fix_plan(report: AuditReport) -> FixPlan:
    """Use Claude to generate a fix plan from an audit report.

    Raises EnvironmentError if ANTHROPIC_API_KEY is not set.
    """
    snap = scan_repo(report.repo_path, read_contents=True)

    findings_data = [
        {
            "check_id": f.check_id,
            "title": f.title,
            "severity": f.severity.value,
            "dimension": f.dimension,
            "points_deducted": f.points_deducted,
            "description": f.description,
            "file_path": f.file_path,
            "suggestion": f.suggestion,
        }
        for f in sorted(report.findings, key=lambda x: -x.points_deducted)
    ]

    relevant_files: dict[str, str] = {}
    for finding in report.findings:
        if finding.file_path and finding.file_path in snap.file_contents:
            relevant_files[finding.file_path] = snap.file_contents[finding.file_path][:4000]

    for key in ["README.md", "requirements.txt", "setup.py", "pyproject.toml", "environment.yml"]:
        if key in snap.file_contents:
            relevant_files[key] = snap.file_contents[key][:3000]

    file_samples = "\n".join(
        f"--- {path} ---\n{content}\n"
        for path, content in list(relevant_files.items())[:10]
    )

    domain = report.field.value
    seed_template = SEED_TEMPLATES.get(domain, SEED_TEMPLATES["general-science"])

    user_msg = FIX_PROMPT.format(
        domain=domain,
        repo_path=report.repo_path,
        findings_json=json.dumps(findings_data, indent=2),
        file_samples=file_samples,
        seed_template=seed_template,
        protected=PROTECTED_PATTERNS,
    )

    raw = ask_claude(SYSTEM_PROMPT, user_msg, max_tokens=8192)

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return FixPlan(
            repo_path=report.repo_path,
            field=domain,
            summary="Claude did not return structured output.",
        )

    data = json.loads(match.group())

    actions = []
    for a in data.get("actions", []):
        path = a.get("file_path", "")
        if _is_protected(path):
            continue
        actions.append(FixAction(
            finding_id=a.get("finding_id", ""),
            file_path=path,
            action_type=a.get("action_type", "document"),
            description=a.get("description", ""),
            diff=a.get("diff"),
            new_content=a.get("new_content"),
            priority=a.get("priority", 3),
        ))

    actions.sort(key=lambda x: x.priority)

    return FixPlan(
        repo_path=report.repo_path,
        field=domain,
        actions=actions,
        summary=data.get("summary", ""),
        estimated_score_impact=float(data.get("estimated_score_impact", 0)),
    )


def _is_protected(path: str) -> bool:
    lower = path.lower()
    return any(p in lower for p in PROTECTED_PATTERNS)


def apply_fixes(plan: FixPlan, *, dry_run: bool = True) -> list[str]:
    """Apply fix actions to the filesystem."""
    modified: list[str] = []
    root = Path(plan.repo_path)

    for action in plan.actions:
        target = root / action.file_path
        if dry_run:
            modified.append(f"[DRY RUN] Would {action.action_type}: {action.file_path}")
            continue

        if action.action_type == "create" and action.new_content:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.new_content)
            modified.append(f"Created: {action.file_path}")

        elif action.action_type == "modify" and action.diff:
            modified.append(f"Diff ready for: {action.file_path}")

        elif action.action_type in ("add_config", "document") and action.new_content:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                existing = target.read_text()
                target.write_text(existing + "\n" + action.new_content)
            else:
                target.write_text(action.new_content)
            modified.append(f"Updated: {action.file_path}")

    return modified
