"""Locate and return SKILL.md for `devicefleet skill`."""

from __future__ import annotations

from pathlib import Path


def skill_path() -> Path | None:
    """Prefer the repo-root SKILL.md, then the packaged copy."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "SKILL.md",  # repo root when installed editable from src/
        here.parents[1] / "SKILL.md",
        here.parent / "data" / "SKILL.md",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_skill_markdown() -> str:
    path = skill_path()
    if path is None:
        return "# Devicefleet skill\n\nSKILL.md was not packaged with this install.\n"
    return path.read_text(encoding="utf-8")
