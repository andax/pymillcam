"""Undo / redo command stack.

Snapshot-based: each entry holds a `description` plus the `Project.model_dump()`
of the state before and after the change. Undo replays the `before`; redo
replays the `after`. The actual mutation is performed by the caller — the
stack only records.

Why snapshot, not Command-pattern subclasses? For Phase 1 the project is
small (kilobytes), and snapshot/restore is bulletproof: any mutation
(geometry, operations, settings, tools) round-trips automatically without a
new Command subclass. When the project grows enough that JSON dumps become
expensive, switch to per-action Command classes — the public API of
`CommandStack` won't need to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StackEntry:
    description: str
    before: dict[str, Any]
    after: dict[str, Any]


class CommandStack:
    """A two-list undo/redo stack of project snapshots."""

    def __init__(self) -> None:
        self._done: list[StackEntry] = []
        self._undone: list[StackEntry] = []

    def push(
        self, description: str, before: dict[str, Any], after: dict[str, Any]
    ) -> None:
        """Record a state transition; no-op if before == after."""
        if before == after:
            return
        self._done.append(StackEntry(description, before, after))
        self._undone.clear()

    def undo(self) -> StackEntry | None:
        if not self._done:
            return None
        entry = self._done.pop()
        self._undone.append(entry)
        return entry

    def redo(self) -> StackEntry | None:
        if not self._undone:
            return None
        entry = self._undone.pop()
        self._done.append(entry)
        return entry

    def clear(self) -> None:
        self._done.clear()
        self._undone.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._done)

    @property
    def can_redo(self) -> bool:
        return bool(self._undone)

    @property
    def undo_description(self) -> str | None:
        return self._done[-1].description if self._done else None

    @property
    def redo_description(self) -> str | None:
        return self._undone[-1].description if self._undone else None
