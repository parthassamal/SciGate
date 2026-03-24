"""Scan a local or cloned repository and extract structural metadata."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pathspec


@dataclass
class RepoSnapshot:
    root: Path
    files: list[Path] = field(default_factory=list)
    file_contents: dict[str, str] = field(default_factory=dict)
    languages: dict[str, int] = field(default_factory=dict)
    total_lines: int = 0

    @property
    def file_list(self) -> list[str]:
        return [str(f.relative_to(self.root)) for f in self.files]


LANG_EXT_MAP = {
    ".py": "python",
    ".r": "r",
    ".R": "r",
    ".jl": "julia",
    ".m": "matlab",
    ".ipynb": "jupyter",
    ".sh": "bash",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".txt": "text",
    ".md": "markdown",
    ".dockerfile": "docker",
    ".nf": "nextflow",
    ".wdl": "wdl",
    ".snakefile": "snakemake",
    ".smk": "snakemake",
}

DEFAULT_IGNORE = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".eggs", "*.egg-info",
}

MAX_FILE_SIZE = 512_000  # 500 KB — skip very large files
MAX_READ_SIZE = 64_000   # 64 KB — only read the first chunk for analysis


def _load_gitignore(root: Path) -> Optional[pathspec.PathSpec]:
    gi = root / ".gitignore"
    if gi.exists():
        return pathspec.PathSpec.from_lines("gitwildmatch", gi.read_text().splitlines())
    return None


def scan_repo(root: str | Path, read_contents: bool = True) -> RepoSnapshot:
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {root}")

    spec = _load_gitignore(root)
    snap = RepoSnapshot(root=root)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in DEFAULT_IGNORE
            and not (spec and spec.match_file(os.path.relpath(os.path.join(dirpath, d), root) + "/"))
        ]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            relpath = fpath.relative_to(root)

            if spec and spec.match_file(str(relpath)):
                continue
            if fpath.stat().st_size > MAX_FILE_SIZE:
                continue

            snap.files.append(fpath)

            ext = fpath.suffix.lower()
            lang = LANG_EXT_MAP.get(ext, ext.lstrip(".") or "unknown")
            snap.languages[lang] = snap.languages.get(lang, 0) + 1

            if read_contents and fpath.stat().st_size < MAX_READ_SIZE:
                try:
                    content = fpath.read_text(errors="replace")
                    snap.file_contents[str(relpath)] = content
                    snap.total_lines += content.count("\n") + 1
                except Exception:
                    pass

    return snap
