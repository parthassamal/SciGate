"""
SciGate — Policy-as-Code loader.

Reads .scigate/policy.yml from a repo root or from the policy/ directory.
Provides default policy when no config is found.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


DEFAULT_POLICY = {
    "gate_threshold": 75,
    "regression_gate": False,
    "regression_threshold": -5,
    "protected_branches": ["main", "master"],
    "notify_channels": [],
    "cost_limit_usd": 50.0,
    "dimensions": {
        "env": {"max": 17, "weight": 1.0},
        "seeds": {"max": 17, "weight": 1.0},
        "data": {"max": 17, "weight": 1.0},
        "docs": {"max": 17, "weight": 1.0},
        "testing": {"max": 17, "weight": 1.0},
        "compliance": {"max": 15, "weight": 1.0},
    },
}


def load_policy(tenant_id: str = "", repo_path: str = "") -> dict[str, Any]:
    """Load policy from file system or return defaults."""
    policy = dict(DEFAULT_POLICY)
    policy["tenant_id"] = tenant_id

    search_paths = []
    if repo_path:
        search_paths.append(Path(repo_path) / ".scigate" / "policy.yml")
        search_paths.append(Path(repo_path) / ".scigate" / "policy.yaml")

    policy_dir = Path(os.environ.get("SCIGATE_POLICY_DIR", "policy"))
    if tenant_id:
        search_paths.append(policy_dir / f"{tenant_id}.yml")
        search_paths.append(policy_dir / f"{tenant_id}.yaml")

    if not HAS_YAML:
        return policy

    for path in search_paths:
        if path.exists():
            try:
                with open(path) as f:
                    custom = yaml.safe_load(f)
                if isinstance(custom, dict):
                    policy.update(custom)
                    policy["_source"] = str(path)
                break
            except Exception:
                continue

    return policy
