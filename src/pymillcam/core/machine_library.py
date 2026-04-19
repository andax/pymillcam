"""Application-wide machine library.

Persists a collection of ``MachineDefinition`` entries so the user
doesn't have to re-paste their shop preamble / parking routine /
tool-change sequence into every new project. Mirrors ``ToolLibrary`` —
same atomic JSON IO, same "soft link" relationship to projects.

Relationship to ``Project.machine``:

    The library is a catalog of machines. When the user opens a new
    project, the MainWindow seeds ``project.machine`` from the library's
    default entry (if any). Edits to the project's machine — rename,
    macro tweaks, controller switch — never retro-propagate to the
    library; that keeps ``.pmc`` files self-contained and reproducible.
    The Machine dialog offers explicit "Save to library…" / "Load from
    library…" actions when the user wants the library to move.

The library's on-disk form is just ``MachineLibrary.model_dump_json``
so new top-level fields land without breaking readers as long as
Pydantic ignores unknown keys.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, model_validator

from pymillcam.core.machine import MachineDefinition


class MachineLibraryLoadError(Exception):
    """Raised when a library file exists but can't be parsed."""


class MachineLibrary(BaseModel):
    """An ordered collection of machines plus a default-selection pointer.

    ``default_machine_id`` is the machine copied into ``project.machine``
    when the user starts a new project. Set to ``None`` for an empty
    library; if it points at an id that no longer exists, the validator
    clears it (pragmatic fallback — the UI shows "no default" rather
    than a ``KeyError`` on next launch).
    """

    version: int = 1
    machines: list[MachineDefinition] = Field(default_factory=list)
    default_machine_id: str | None = None

    @model_validator(mode="after")
    def _prune_stale_default(self) -> MachineLibrary:
        if self.default_machine_id is None:
            return self
        if not any(m.id == self.default_machine_id for m in self.machines):
            self.default_machine_id = None
        return self

    # ------------------------------------------------------------- lookup

    def find(self, machine_id: str) -> MachineDefinition | None:
        """Return the machine with ``machine_id``, or None if absent."""
        return next((m for m in self.machines if m.id == machine_id), None)

    def default_machine(self) -> MachineDefinition | None:
        """Convenience: the currently-default machine, or None."""
        if self.default_machine_id is None:
            return None
        return self.find(self.default_machine_id)

    # --------------------------------------------------------- mutations

    def add(self, machine: MachineDefinition) -> None:
        """Append ``machine``. Caller sets ``default_machine_id`` separately."""
        self.machines.append(machine)

    def remove(self, machine_id: str) -> None:
        """Remove the machine with ``machine_id``. Silently no-op for unknown ids.

        If the removed entry was the default, ``default_machine_id``
        becomes ``None`` so the library stays consistent.
        """
        self.machines = [m for m in self.machines if m.id != machine_id]
        if self.default_machine_id == machine_id:
            self.default_machine_id = None


def load_library(path: Path) -> MachineLibrary:
    """Read the library from ``path``. Returns an empty library when
    the file doesn't exist (first-run case — no error).

    Raises ``MachineLibraryLoadError`` on a present-but-unreadable or
    malformed file so the caller can decide whether to back it up and
    continue with a blank library.
    """
    if not path.exists():
        return MachineLibrary()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MachineLibraryLoadError(f"Cannot read {path}: {exc}") from exc
    try:
        return MachineLibrary.model_validate_json(text)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise MachineLibraryLoadError(
            f"Invalid library in {path}: {exc}"
        ) from exc


def save_library(lib: MachineLibrary, path: Path) -> None:
    """Atomically write the library to ``path``.

    Write-to-tmp + rename pattern: avoids truncating the existing file
    on a crash mid-write, and lets a reader that happens to open the
    path during a save get either the old or the new content — never a
    half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(lib.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
