"""Atomic YAML persistence with exclusive file locks.

Current-session maps (`state.yaml`) use last-writer-wins *per save*. Each
`update()` holds the store lock for a read-modify-write so two agents merging
different keys do not clobber each other. Concurrent `save()` calls still
replace the whole document; the last `os.replace` wins.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import yaml

T = TypeVar("T")


class ExclusiveFileLock:
    """Inter-process exclusive lock (fcntl) on a sibling `.lock` file."""

    def __init__(self, path: Path) -> None:
        if not path:
            raise ValueError("lock path is required")
        self.path = path
        self._handle: object | None = None

    def __enter__(self) -> ExclusiveFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        self._handle = handle
        return self

    def __exit__(self, *exc: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


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
        """Write the document atomically via a unique temp file in the same dir.

        The temp name includes a random suffix so concurrent writers do not
        share `file.yaml.tmp` (which previously raised FileNotFoundError when
        two processes replaced/unlinked the same path).
        """
        text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold an exclusive lock for a critical section on this store."""
        lock = ExclusiveFileLock(self.path.with_name(self.path.name + ".lock"))
        with lock:
            yield

    def update(self, mutator: Callable[[dict[str, object]], T]) -> T:
        """Load, mutate, and persist under the store lock."""
        with self.lock():
            document = self.load()
            result = mutator(document)
            self.save(document)
            return result
