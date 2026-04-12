"""Save / load a Project as JSON.

Every model under pymillcam.core is Pydantic-based and already round-trips
through JSON, so this module is a thin file-I/O wrapper that:

- writes pretty-printed JSON by default for human-diffable project files
- wraps Pydantic / filesystem errors in a single ProjectLoadError so
  callers don't need to catch a grab bag of exception types
- leaves file-extension policy to the caller; `.pmc` is suggested but
  not enforced
"""
from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from pymillcam.core.project import Project


class ProjectLoadError(Exception):
    """Raised when a project file cannot be opened, parsed, or validated."""


def save_project(project: Project, path: str | Path, *, indent: int = 2) -> None:
    """Serialize `project` to JSON at `path`.

    Set `indent=None` for a compact single-line representation.
    """
    Path(path).write_text(project.model_dump_json(indent=indent), encoding="utf-8")


def load_project(path: str | Path) -> Project:
    """Load a Project from a JSON file.

    Raises ProjectLoadError on any failure — missing file, malformed JSON,
    or schema mismatch.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ProjectLoadError(f"Cannot open project {p}: {e}") from e
    try:
        return Project.model_validate_json(text)
    except ValidationError as e:
        raise ProjectLoadError(f"Invalid project in {p}:\n{e}") from e
