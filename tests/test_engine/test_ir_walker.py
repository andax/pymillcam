"""Tests for the IR-to-XY walker used by the viewport's toolpath overlay."""
from __future__ import annotations

import math

from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.engine.ir import IRInstruction, MoveType
from pymillcam.engine.ir_walker import MoveKind, walk_toolpath


def test_z_only_moves_produce_no_xy_segments() -> None:
    moves = walk_toolpath([
        IRInstruction(type=MoveType.RAPID, z=15.0),
        IRInstruction(type=MoveType.RAPID, z=3.0),
        IRInstruction(type=MoveType.FEED, z=-1.0, f=300),
    ])
    assert moves == []


def test_rapid_then_feed_emit_two_moves_with_correct_kinds() -> None:
    moves = walk_toolpath([
        IRInstruction(type=MoveType.RAPID, x=0, y=0),
        IRInstruction(type=MoveType.RAPID, x=10, y=0),
        IRInstruction(type=MoveType.FEED, x=10, y=10, f=1200),
    ])
    assert [m.kind for m in moves] == [MoveKind.RAPID, MoveKind.FEED]
    assert isinstance(moves[0].segment, LineSegment)
    assert moves[0].segment.start == (0.0, 0.0)
    assert moves[0].segment.end == (10.0, 0.0)
    assert moves[1].segment.start == (10.0, 0.0)
    assert moves[1].segment.end == (10.0, 10.0)


def test_arc_ccw_quarter_circle_walk() -> None:
    # Pre-position to (10, 0). Then arc CCW to (0, 10) around origin (i=-10, j=0).
    moves = walk_toolpath([
        IRInstruction(type=MoveType.RAPID, x=10, y=0),
        IRInstruction(type=MoveType.ARC_CCW, x=0, y=10, i=-10, j=0, f=1200),
    ])
    assert len(moves) == 1
    arc = moves[0].segment
    assert isinstance(arc, ArcSegment)
    assert arc.center == (0.0, 0.0)
    assert arc.radius == 10.0
    assert arc.sweep_deg == 90.0


def test_arc_cw_full_circle_returns_signed_360() -> None:
    moves = walk_toolpath([
        IRInstruction(type=MoveType.RAPID, x=10, y=0),
        IRInstruction(type=MoveType.ARC_CW, x=10, y=0, i=-10, j=0, f=1200),
    ])
    assert len(moves) == 1
    arc = moves[0].segment
    assert isinstance(arc, ArcSegment)
    assert arc.sweep_deg == -360.0
    assert math.isclose(arc.radius, 10.0)


def test_non_motion_instructions_are_skipped() -> None:
    moves = walk_toolpath([
        IRInstruction(type=MoveType.SPINDLE_ON, s=18000),
        IRInstruction(type=MoveType.TOOL_CHANGE, tool_number=1),
        IRInstruction(type=MoveType.COMMENT, comment="hi"),
        IRInstruction(type=MoveType.RAPID, x=5, y=5),
        IRInstruction(type=MoveType.FEED, x=5, y=10, f=1200),
    ])
    assert [m.kind for m in moves] == [MoveKind.FEED]
    assert moves[0].segment.start == (5.0, 5.0)
    assert moves[0].segment.end == (5.0, 10.0)
