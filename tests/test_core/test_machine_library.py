"""Behaviour tests for the MachineLibrary model + JSON round-trip.

Mirrors ``test_tool_library.py`` — same contract (empty default handling,
find/add/remove, atomic save/load, default-pruning on validation).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pymillcam.core.machine import MachineDefinition
from pymillcam.core.machine_library import (
    MachineLibrary,
    MachineLibraryLoadError,
    load_library,
    save_library,
)


def _machine(name: str = "CNC 6040", controller: str = "uccnc") -> MachineDefinition:
    return MachineDefinition(name=name, controller=controller)


# -------------------------------------------------------------- model basics


def test_empty_library_has_no_default() -> None:
    lib = MachineLibrary()
    assert lib.machines == []
    assert lib.default_machine_id is None
    assert lib.default_machine() is None


def test_add_appends_to_list() -> None:
    lib = MachineLibrary()
    m = _machine()
    lib.add(m)
    assert lib.machines == [m]


def test_find_returns_machine_by_id() -> None:
    lib = MachineLibrary()
    m = _machine()
    lib.add(m)
    assert lib.find(m.id) is m
    assert lib.find("nonexistent") is None


def test_default_machine_resolves_reference() -> None:
    lib = MachineLibrary()
    m = _machine()
    lib.add(m)
    lib.default_machine_id = m.id
    assert lib.default_machine() is m


def test_remove_drops_machine_and_clears_default_when_matched() -> None:
    lib = MachineLibrary()
    m1 = _machine("First")
    m2 = _machine("Second")
    lib.add(m1)
    lib.add(m2)
    lib.default_machine_id = m1.id

    lib.remove(m1.id)

    assert [m.name for m in lib.machines] == ["Second"]
    assert lib.default_machine_id is None


def test_remove_leaves_default_alone_when_unrelated() -> None:
    lib = MachineLibrary()
    m1 = _machine("First")
    m2 = _machine("Second")
    lib.add(m1)
    lib.add(m2)
    lib.default_machine_id = m2.id

    lib.remove(m1.id)

    assert lib.default_machine_id == m2.id


def test_stale_default_is_pruned_on_validation() -> None:
    """A saved library that references a since-deleted machine id should
    load with ``default_machine_id`` reset to None rather than raising."""
    lib = MachineLibrary.model_validate(
        {
            "machines": [],
            "default_machine_id": "missing-id",
        }
    )
    assert lib.default_machine_id is None


# --------------------------------------------------------------- JSON round-trip


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    lib = MachineLibrary()
    m = _machine("CNC 6040")
    m.macros["program_start"] = "(SHOP)\nG21 G90"
    lib.add(m)
    lib.default_machine_id = m.id

    path = tmp_path / "machine_library.json"
    save_library(lib, path)
    restored = load_library(path)

    assert [m.name for m in restored.machines] == ["CNC 6040"]
    assert restored.default_machine_id == m.id
    assert restored.machines[0].macros["program_start"] == "(SHOP)\nG21 G90"


def test_load_missing_file_returns_empty_library(tmp_path: Path) -> None:
    path = tmp_path / "absent.json"
    lib = load_library(path)
    assert lib.machines == []


def test_load_malformed_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not valid json", encoding="utf-8")
    with pytest.raises(MachineLibraryLoadError):
        load_library(path)


def test_save_is_atomic_via_tmp_file(tmp_path: Path) -> None:
    """Confirm the tmp-file-then-rename pattern leaves no stray ``.tmp``
    after a successful save."""
    lib = MachineLibrary()
    path = tmp_path / "machine_library.json"
    save_library(lib, path)
    assert path.exists()
    assert not (tmp_path / "machine_library.json.tmp").exists()
