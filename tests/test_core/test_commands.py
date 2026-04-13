"""Tests for the snapshot-based undo / redo command stack."""
from __future__ import annotations

import pytest

from pymillcam.core.commands import CommandStack


def test_empty_stack_cannot_undo_or_redo() -> None:
    s = CommandStack()
    assert not s.can_undo
    assert not s.can_redo
    assert s.undo() is None
    assert s.redo() is None


def test_push_then_undo_returns_entry_and_moves_state() -> None:
    s = CommandStack()
    s.push("Add", before={"x": 1}, after={"x": 2})
    assert s.can_undo
    assert not s.can_redo
    entry = s.undo()
    assert entry is not None
    assert entry.before == {"x": 1}
    assert entry.after == {"x": 2}
    assert entry.description == "Add"
    assert not s.can_undo
    assert s.can_redo


def test_redo_after_undo_restores_can_undo() -> None:
    s = CommandStack()
    s.push("a", before={"v": 0}, after={"v": 1})
    s.undo()
    entry = s.redo()
    assert entry is not None
    assert entry.after == {"v": 1}
    assert s.can_undo
    assert not s.can_redo


def test_new_push_clears_redo_history() -> None:
    s = CommandStack()
    s.push("a", before={"v": 0}, after={"v": 1})
    s.push("b", before={"v": 1}, after={"v": 2})
    s.push("c", before={"v": 2}, after={"v": 3})
    s.undo()  # back to v=2
    s.undo()  # back to v=1
    assert s.can_redo
    s.push("d", before={"v": 1}, after={"v": 99})
    assert not s.can_redo


@pytest.mark.parametrize("done,undo,expected_done", [
    (5, 3, 2),
    (5, 5, 0),
    (5, 0, 5),
])
def test_multi_step_done_undone_balance(done: int, undo: int, expected_done: int) -> None:
    s = CommandStack()
    for i in range(done):
        s.push(f"cmd{i}", before={"i": i}, after={"i": i + 1})
    for _ in range(undo):
        s.undo()
    # Push something new — should clear redo and leave us with expected_done + 1.
    s.push("new", before={"i": 99}, after={"i": 100})
    # Count the done stack via repeated undo.
    count = 0
    while s.undo() is not None:
        count += 1
    assert count == expected_done + 1


def test_clear_drops_both_stacks() -> None:
    s = CommandStack()
    s.push("a", before={}, after={"x": 1})
    s.undo()
    s.clear()
    assert not s.can_undo
    assert not s.can_redo


def test_no_op_push_is_dropped() -> None:
    s = CommandStack()
    s.push("noop", before={"x": 1}, after={"x": 1})
    assert not s.can_undo


def test_undo_redo_descriptions_track_top_of_stack() -> None:
    s = CommandStack()
    s.push("first", before={"v": 0}, after={"v": 1})
    s.push("second", before={"v": 1}, after={"v": 2})
    assert s.undo_description == "second"
    s.undo()
    assert s.undo_description == "first"
    assert s.redo_description == "second"
