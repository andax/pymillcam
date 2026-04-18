"""Application-wide tool library.

Persists a collection of ``Tool`` definitions (geometry + per-material
cutting data) so the user doesn't have to re-enter a 3 mm endmill's
feeds every time they create a new profile / pocket / drill op.

Relationship to ``ToolController``:

    The library is a catalog of *defaults*. When the user adds an
    operation, the MainWindow creates a new ``ToolController`` **copied
    from** the currently-selected library tool (matched by ``id``).
    Projects stay self-contained — editing a tool in the library later
    does not retroactively change existing ops, which keeps ``.pmc``
    files portable and reproducible. Users who want a project-wide
    tool update can re-assign ops from the library explicitly.

JSON format is a superset of FreeCAD ``.fctb`` / ``.fctl`` — the import
side of that compatibility lives in ``io/tool_import.py`` (not yet
written). The library's own on-disk form is just the result of
``ToolLibrary.model_dump_json`` so new top-level fields (material
database links, supplier URLs, etc.) can land without breaking readers
as long as Pydantic ignores unknown keys.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, model_validator

from pymillcam.core.tools import Tool


class ToolLibraryLoadError(Exception):
    """Raised when a library file exists but can't be parsed."""


class ToolLibrary(BaseModel):
    """An ordered collection of tools plus a default-selection pointer.

    ``default_tool_id`` is the tool used by ``Add Profile / Pocket /
    Drill`` when no explicit selection is in force. Set to ``None`` for
    an empty library; if it points at an id that no longer exists, the
    validator clears it (pragmatic fallback — the UI shows "no default"
    rather than a ``KeyError`` on next launch).
    """

    version: int = 1
    tools: list[Tool] = Field(default_factory=list)
    default_tool_id: str | None = None

    @model_validator(mode="after")
    def _prune_stale_default(self) -> ToolLibrary:
        if self.default_tool_id is None:
            return self
        if not any(t.id == self.default_tool_id for t in self.tools):
            self.default_tool_id = None
        return self

    # ------------------------------------------------------------- lookup

    def find(self, tool_id: str) -> Tool | None:
        """Return the tool with ``tool_id``, or None if absent."""
        return next((t for t in self.tools if t.id == tool_id), None)

    def default_tool(self) -> Tool | None:
        """Convenience: the currently-default tool, or None."""
        if self.default_tool_id is None:
            return None
        return self.find(self.default_tool_id)

    # --------------------------------------------------------- mutations

    def add(self, tool: Tool) -> None:
        """Append ``tool``. Caller sets ``default_tool_id`` separately."""
        self.tools.append(tool)

    def remove(self, tool_id: str) -> None:
        """Remove the tool with ``tool_id``. Silently no-op for unknown ids.

        If the removed tool was the default, ``default_tool_id`` becomes
        ``None`` so the library stays consistent.
        """
        self.tools = [t for t in self.tools if t.id != tool_id]
        if self.default_tool_id == tool_id:
            self.default_tool_id = None


def load_library(path: Path) -> ToolLibrary:
    """Read the library from ``path``. Returns an empty library when
    the file doesn't exist (first-run case — no error).

    Raises ``ToolLibraryLoadError`` on a present-but-unreadable or
    malformed file so the caller can decide whether to back it up and
    continue with a blank library.
    """
    if not path.exists():
        return ToolLibrary()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolLibraryLoadError(f"Cannot read {path}: {exc}") from exc
    try:
        return ToolLibrary.model_validate_json(text)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ToolLibraryLoadError(f"Invalid library in {path}: {exc}") from exc


def save_library(lib: ToolLibrary, path: Path) -> None:
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
