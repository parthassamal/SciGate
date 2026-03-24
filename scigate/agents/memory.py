"""Agent 3 — Org Memory Agent.

Confidence-scored pattern store that learns from every audit.  Stores
{repo_pattern, repro_failure_type, fix_applied, score_delta} tuples and
feeds high-confidence hints back to Agent 1 on subsequent audits.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class MemoryEntry:
    repo_pattern: str
    repro_failure_type: str
    fix_applied: str
    score_delta: float
    confidence: float
    field: str
    timestamp: float = 0.0
    occurrences: int = 1

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


@dataclass
class OrgMemory:
    entries: list[MemoryEntry] = field(default_factory=list)
    store_path: Optional[Path] = None

    # ── Persistence ──────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str | Path) -> OrgMemory:
        path = Path(path)
        mem = cls(store_path=path)
        if path.exists():
            data = json.loads(path.read_text())
            mem.entries = [MemoryEntry(**e) for e in data.get("entries", [])]
        return mem

    def save(self) -> None:
        if self.store_path is None:
            raise ValueError("No store_path configured — call OrgMemory.load() or set store_path.")
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"entries": [asdict(e) for e in self.entries]}
        self.store_path.write_text(json.dumps(data, indent=2))

    # ── Recording ────────────────────────────────────────────────────────

    def record(
        self,
        repo_pattern: str,
        repro_failure_type: str,
        fix_applied: str,
        score_delta: float,
        sci_field: str,
    ) -> MemoryEntry:
        """Record or update a pattern.  Confidence increases with repeated observation."""
        existing = self._find(repo_pattern, repro_failure_type)
        if existing:
            existing.occurrences += 1
            existing.score_delta = (
                existing.score_delta * 0.7 + score_delta * 0.3
            )
            existing.confidence = min(1.0, existing.confidence + 0.05)
            existing.fix_applied = fix_applied
            existing.timestamp = time.time()
            self.save()
            return existing

        entry = MemoryEntry(
            repo_pattern=repo_pattern,
            repro_failure_type=repro_failure_type,
            fix_applied=fix_applied,
            score_delta=score_delta,
            confidence=_initial_confidence(score_delta),
            field=sci_field,
        )
        self.entries.append(entry)
        self.save()
        return entry

    # ── Querying ─────────────────────────────────────────────────────────

    def get_hints(
        self,
        sci_field: str,
        *,
        min_confidence: float = 0.4,
        limit: int = 20,
    ) -> list[dict]:
        """Return high-confidence patterns relevant to a field, for Agent 1."""
        relevant = [
            e for e in self.entries
            if e.field == sci_field and e.confidence >= min_confidence
        ]
        relevant.sort(key=lambda e: (-e.confidence, -e.occurrences))
        return [asdict(e) for e in relevant[:limit]]

    def get_stats(self) -> dict:
        if not self.entries:
            return {"total_patterns": 0, "avg_confidence": 0.0, "fields": {}}

        fields: dict[str, int] = {}
        for e in self.entries:
            fields[e.field] = fields.get(e.field, 0) + 1

        return {
            "total_patterns": len(self.entries),
            "avg_confidence": sum(e.confidence for e in self.entries) / len(self.entries),
            "top_patterns": [
                {"pattern": e.repo_pattern, "failure": e.repro_failure_type, "confidence": e.confidence}
                for e in sorted(self.entries, key=lambda e: -e.confidence)[:5]
            ],
            "fields": fields,
        }

    # ── Decay ────────────────────────────────────────────────────────────

    def decay(self, factor: float = 0.98) -> None:
        """Apply time-based confidence decay and prune very low entries."""
        for entry in self.entries:
            entry.confidence *= factor
        self.entries = [e for e in self.entries if e.confidence >= 0.1]
        self.save()

    # ── Internal ─────────────────────────────────────────────────────────

    def _find(self, repo_pattern: str, failure_type: str) -> Optional[MemoryEntry]:
        for e in self.entries:
            if e.repo_pattern == repo_pattern and e.repro_failure_type == failure_type:
                return e
        return None


def _initial_confidence(score_delta: float) -> float:
    """Higher score impact → higher initial confidence."""
    if score_delta >= 15:
        return 0.7
    if score_delta >= 8:
        return 0.55
    return 0.4
