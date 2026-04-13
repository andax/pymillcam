"""Application-wide preferences.

Lives at the top of the settings cascade:
`AppPreferences  →  Project (Settings)  →  Operation overrides`.

Stored as a JSON file in the user config directory. Resolving that
directory needs Qt (it knows the per-platform convention), so the path
lookup lives in `ui/` — this module just takes a `Path`.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError


class PreferencesLoadError(Exception):
    """Raised when a preferences file exists but can't be parsed."""


class AppPreferences(BaseModel):
    """Application-wide preferences with safe defaults."""

    version: int = 1
    # Chord-sag tolerance (mm) used when arcs must be collapsed to chords for
    # G-code output. Seeds `ProjectSettings.chord_tolerance` for new projects;
    # individual operations may override it.
    default_chord_tolerance_mm: float = Field(0.02, gt=0)
    # Default tool diameter (mm) used by `Add Profile` when the project has
    # no ToolController yet.
    default_tool_diameter_mm: float = Field(3.0, gt=0)
    # When true, DXF import will try to weld separate LINE entities into one
    # contour where their endpoints meet within `stitch_tolerance_mm`.
    auto_stitch_on_import: bool = False
    stitch_tolerance_mm: float = Field(0.01, gt=0)
    # Idle time (ms) between Properties-panel edits before the coalesced
    # undo entry is pushed.
    edit_coalesce_ms: int = Field(400, ge=0)


def load_preferences(path: Path) -> AppPreferences:
    """Read preferences from `path`. Returns defaults if the file is absent.

    Raises `PreferencesLoadError` for a present-but-unreadable / malformed
    file so the caller can decide whether to back it up and continue.
    """
    if not path.exists():
        return AppPreferences()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreferencesLoadError(f"Cannot read {path}: {exc}") from exc
    try:
        return AppPreferences.model_validate_json(text)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise PreferencesLoadError(f"Invalid preferences in {path}: {exc}") from exc


def save_preferences(prefs: AppPreferences, path: Path) -> None:
    """Atomically write preferences to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(prefs.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
