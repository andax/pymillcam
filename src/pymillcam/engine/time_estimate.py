"""Operation machining-time estimator.

Walks a ``Toolpath``'s IR instruction stream once and sums the time each
instruction contributes. The result is an estimate only — real-machine
time depends on accel/decel, look-ahead, and the controller's handling
of tiny segments — but it's accurate enough for job planning (pick
the right operation order; decide whether a finishing pass is worth
the extra minutes).

What each instruction contributes:

* ``RAPID`` — 3D distance divided by ``rapid_rate_mm_per_min``.
* ``FEED`` — 3D distance divided by the instruction's own feed rate.
  Helical feeds (XY + Z simultaneously) get their full 3D path length.
* ``ARC_CW`` / ``ARC_CCW`` — arc length = radius × |sweep|; for a
  helix (Z also changes over the arc) we use ``hypot(arc, Δz)``.
* ``DWELL`` — the dwell field itself, in seconds.
* ``TOOL_CHANGE`` — fixed ``tool_change_seconds`` per instruction.
* Everything else (``SPINDLE_*``, ``COOLANT_*``, ``COMMENT``,
  ``MACRO``) contributes zero. ``MACRO`` time depends on what the
  macro does; not estimable until the post-processor expands it.

Rapids across a ``z``-only retract still count — a ``G0 Z15`` move
travels 15 mm of Z in air, and at 5000 mm/min that's 0.18 s. Sums up.

Two defaults match ``MachineDefinition`` defaults so a project with
no machine assigned still gets usable numbers:

* ``DEFAULT_RAPID_RATE_MM_PER_MIN = 5000``
* ``DEFAULT_TOOL_CHANGE_SECONDS = 90`` (manual tool change; ATC
  machines should override to their own figure)
"""
from __future__ import annotations

import math

from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath

DEFAULT_RAPID_RATE_MM_PER_MIN = 5000.0
DEFAULT_TOOL_CHANGE_SECONDS = 90.0


def estimate_toolpath_seconds(
    toolpath: Toolpath,
    *,
    rapid_rate_mm_per_min: float = DEFAULT_RAPID_RATE_MM_PER_MIN,
    tool_change_seconds: float = DEFAULT_TOOL_CHANGE_SECONDS,
) -> float:
    """Return the estimated machining time for ``toolpath`` in seconds.

    The estimate covers every instruction that contributes time — feeds
    (including helical), arcs, rapids, dwells, and tool changes. It
    ignores the physics of accel/decel, which means short back-to-back
    segments come out faster here than on a real machine; plan with a
    10–20 % margin when the toolpath is dense.
    """
    if rapid_rate_mm_per_min <= 0:
        raise ValueError(
            f"rapid_rate_mm_per_min must be positive, got {rapid_rate_mm_per_min}"
        )
    if tool_change_seconds < 0:
        raise ValueError(
            f"tool_change_seconds cannot be negative, got {tool_change_seconds}"
        )

    total = 0.0
    # Current machine position. None until the first positioning move —
    # a toolpath that starts with a COMMENT / TOOL_CHANGE / SPINDLE_ON
    # triple has no XY/Z until the first RAPID, and contributes zero
    # distance up to that point.
    cur: tuple[float, float, float] | None = None

    for inst in toolpath.instructions:
        if inst.type is MoveType.TOOL_CHANGE:
            total += tool_change_seconds
            continue
        if inst.type is MoveType.DWELL:
            # The DWELL IR instruction uses ``f`` as dwell seconds
            # (see post/uccnc.py — ``G4 P{f}``). Pragmatic double-use
            # of the feed field until we give DWELL its own slot.
            if inst.f is not None and inst.f > 0:
                total += inst.f
            continue
        if inst.type in (
            MoveType.SPINDLE_ON,
            MoveType.SPINDLE_OFF,
            MoveType.COOLANT_ON,
            MoveType.COOLANT_OFF,
            MoveType.COMMENT,
            MoveType.MACRO,
        ):
            continue

        # From here on, we're dealing with a motion instruction. Resolve
        # the target position by defaulting missing axes to the current.
        if cur is None:
            # First positioning move. Don't charge time for the implicit
            # "jump to start" — the machine is already there.
            cur = (
                inst.x if inst.x is not None else 0.0,
                inst.y if inst.y is not None else 0.0,
                inst.z if inst.z is not None else 0.0,
            )
            continue

        target = (
            inst.x if inst.x is not None else cur[0],
            inst.y if inst.y is not None else cur[1],
            inst.z if inst.z is not None else cur[2],
        )

        if inst.type is MoveType.RAPID:
            total += _dist3d(cur, target) / rapid_rate_mm_per_min * 60.0
            cur = target
        elif inst.type is MoveType.FEED:
            feed = inst.f
            if feed is None or feed <= 0:
                # No feed means the generator made a mistake — skip
                # rather than division-by-zero. Real usage should
                # always set f for FEED.
                cur = target
                continue
            total += _dist3d(cur, target) / feed * 60.0
            cur = target
        elif inst.type in (MoveType.ARC_CW, MoveType.ARC_CCW):
            feed = inst.f
            if feed is None or feed <= 0 or inst.i is None or inst.j is None:
                cur = target
                continue
            length = _arc_length(cur, target, inst)
            total += length / feed * 60.0
            cur = target

    return total


def format_seconds(seconds: float) -> str:
    """Render as ``MM:SS`` for under an hour, ``HH:MM:SS`` otherwise.

    Rounds to the nearest whole second — sub-second precision is below
    the accuracy of the estimate itself, and shorter labels read
    better in the ops tree.
    """
    rounded = max(0, int(round(seconds)))
    hours, rem = divmod(rounded, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ------------------------------------------------------------------ helpers


def _dist3d(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    dz = b[2] - a[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _arc_length(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    inst: IRInstruction,
) -> float:
    """Length travelled over a G2 / G3 arc (helical Z allowed).

    IR convention matches G-code: ``i`` and ``j`` are the **incremental**
    offsets from the arc-start to the centre (i.e. centre = start + (i,j)).
    """
    assert inst.i is not None
    assert inst.j is not None
    cx = start[0] + inst.i
    cy = start[1] + inst.j
    radius = math.hypot(start[0] - cx, start[1] - cy)
    start_ang = math.atan2(start[1] - cy, start[0] - cx)
    end_ang = math.atan2(end[1] - cy, end[0] - cx)

    # Compute sweep in the direction this G2/G3 specifies. Both wraps
    # into [0, 2π); a start == end case means a full circle (our
    # generator splits those into halves, but we handle it here in
    # case an externally-supplied IR uses it).
    if inst.type is MoveType.ARC_CCW:
        sweep = (end_ang - start_ang) % (2.0 * math.pi)
    else:
        sweep = (start_ang - end_ang) % (2.0 * math.pi)
    if sweep < 1e-12:
        sweep = 2.0 * math.pi

    arc_len = radius * sweep
    dz = end[2] - start[2]
    if abs(dz) < 1e-12:
        return arc_len
    # Helix: Pythagorean combine. Treats Z-rise as uniformly distributed
    # along the arc (what a controller does under G2/G3 Z).
    return math.hypot(arc_len, dz)
