"""Atomic YAML persistence with a process-local lock."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import yaml

T = TypeVar("T")


class YamlStore:
    """Load and save a YAML document with replace-on-write semantics."""

    def __init__(self, path: Path) -> None:
        if not path:
            raise ValueError("store path is required")
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, object]:
        """Return the document, or an empty mapping when the file is missing."""
        if not self.path.exists():
            return {}
        raw = self.path.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        loaded = yaml.safe_load(raw)
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValueError(f"expected a YAML mapping in {self.path}")
        return loaded

    def save(self, document: dict[str, object]) -> None:
        """Write the document atomically."""
        text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, mutator: Callable[[dict[str, object]], T]) -> T:
        """Load, mutate, persist, and return the mutator's result."""
        document = self.load()
        result = mutator(document)
        self.save(document)
        return result
