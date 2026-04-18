"""Behaviour tests for the ToolLibrary model + JSON round-trip."""
from __future__ import annotations

from pathlib import Path

import pytest

from pymillcam.core.tool_library import (
    ToolLibrary,
    ToolLibraryLoadError,
    load_library,
    save_library,
)
from pymillcam.core.tools import Tool, ToolShape


def _tool(name: str = "3mm endmill", diameter: float = 3.0) -> Tool:
    t = Tool(name=name, shape=ToolShape.ENDMILL)
    t.geometry["diameter"] = diameter
    return t


# -------------------------------------------------------------- model basics


def test_empty_library_has_no_default() -> None:
    lib = ToolLibrary()
    assert lib.tools == []
    assert lib.default_tool_id is None
    assert lib.default_tool() is None


def test_add_tool_appends_to_list() -> None:
    lib = ToolLibrary()
    t = _tool()
    lib.add(t)
    assert lib.tools == [t]


def test_find_returns_tool_by_id() -> None:
    lib = ToolLibrary()
    t = _tool()
    lib.add(t)
    assert lib.find(t.id) is t
    assert lib.find("nonexistent") is None


def test_default_tool_resolves_reference() -> None:
    lib = ToolLibrary()
    t = _tool()
    lib.add(t)
    lib.default_tool_id = t.id
    assert lib.default_tool() is t


def test_remove_drops_tool_and_clears_default_when_matched() -> None:
    lib = ToolLibrary()
    t1 = _tool("t1", 3.0)
    t2 = _tool("t2", 6.0)
    lib.add(t1)
    lib.add(t2)
    lib.default_tool_id = t1.id

    lib.remove(t1.id)

    assert lib.find(t1.id) is None
    assert lib.find(t2.id) is t2
    # Removing the defaulted tool clears the default — the library
    # stays consistent, callers check for None.
    assert lib.default_tool_id is None


def test_remove_preserves_default_when_different_tool() -> None:
    lib = ToolLibrary()
    t1 = _tool("t1", 3.0)
    t2 = _tool("t2", 6.0)
    lib.add(t1)
    lib.add(t2)
    lib.default_tool_id = t1.id

    lib.remove(t2.id)

    assert lib.default_tool_id == t1.id


def test_remove_unknown_id_is_silent_noop() -> None:
    lib = ToolLibrary()
    lib.add(_tool())
    lib.remove("not-present")  # no exception, no state change
    assert len(lib.tools) == 1


# ------------------------------------------------------- validator robustness


def test_validator_clears_stale_default_on_construction() -> None:
    """If a JSON file has a default_tool_id that doesn't match any
    present tool (manual edit, botched merge, tool deleted in a later
    session), drop the id rather than letting downstream code KeyError."""
    lib = ToolLibrary(
        tools=[_tool("t1", 3.0)],
        default_tool_id="totally-fake-id",
    )
    assert lib.default_tool_id is None


def test_validator_accepts_valid_default_reference() -> None:
    t = _tool()
    lib = ToolLibrary(tools=[t], default_tool_id=t.id)
    assert lib.default_tool_id == t.id


# ------------------------------------------------------------- save / load


def test_save_load_round_trip_preserves_tool_ids(tmp_path: Path) -> None:
    """A stable ``id`` means a saved-then-reloaded library maps to the
    same tool object references in users' project files. Without this
    property, editing the library would silently break references."""
    path = tmp_path / "library.json"
    lib = ToolLibrary()
    t = _tool()
    original_id = t.id
    lib.add(t)
    lib.default_tool_id = t.id

    save_library(lib, path)
    loaded = load_library(path)

    assert len(loaded.tools) == 1
    assert loaded.tools[0].id == original_id
    assert loaded.default_tool_id == original_id


def test_save_load_round_trip_preserves_cutting_data(tmp_path: Path) -> None:
    from pymillcam.core.tools import CuttingData

    path = tmp_path / "library.json"
    t = _tool()
    t.cutting_data["aluminum"] = CuttingData(
        spindle_rpm=18000, feed_xy=1200, feed_z=300,
    )
    t.cutting_data["wood"] = CuttingData(
        spindle_rpm=22000, feed_xy=2000, feed_z=500,
    )
    save_library(ToolLibrary(tools=[t]), path)

    loaded = load_library(path)
    reloaded_tool = loaded.tools[0]
    assert set(reloaded_tool.cutting_data.keys()) == {"aluminum", "wood"}
    assert reloaded_tool.cutting_data["aluminum"].feed_xy == 1200


def test_load_missing_file_returns_empty_library(tmp_path: Path) -> None:
    """First-run behaviour: no library file exists yet. Returning a
    blank library (not raising) lets the app start without a special
    bootstrap path."""
    path = tmp_path / "never-written.json"
    lib = load_library(path)
    assert lib.tools == []
    assert lib.default_tool_id is None


def test_load_malformed_json_raises_load_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ToolLibraryLoadError, match="Invalid library"):
        load_library(path)


def test_save_is_atomic_no_tmp_file_remains(tmp_path: Path) -> None:
    path = tmp_path / "library.json"
    lib = ToolLibrary(tools=[_tool()])
    save_library(lib, path)
    # The .tmp sibling used for atomic write should be gone after rename.
    assert not (path.with_suffix(path.suffix + ".tmp")).exists()
    assert path.exists()


def test_save_creates_parent_directory(tmp_path: Path) -> None:
    """App config dirs may not exist on first run — save must mkdir -p."""
    path = tmp_path / "nested" / "sub" / "library.json"
    save_library(ToolLibrary(), path)
    assert path.exists()


# --------------------------------------------------- Tool.id backward compat


def test_tool_without_id_in_json_gets_id_assigned() -> None:
    """Older .pmc files have Tool entries without an ``id`` field. The
    default_factory should fill one in, so they load instead of failing
    validation."""
    import json

    from pymillcam.core.tools import Tool

    payload = {"name": "legacy 3mm"}
    t = Tool.model_validate(json.loads(json.dumps(payload)))
    assert t.id  # a uuid hex was assigned
    assert len(t.id) == 32
