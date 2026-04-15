"""Tab placement and per-pass Z modulation for profile operations.

Tabs are bridges of stock left in place so the cut part doesn't drift on
the final pass. Auto-placement spaces `count` tabs evenly along the
contour's arc-length. Each tab footprint is

    [entry ramp] [plateau] [exit ramp]
        ↑           ↑          ↑
    ramp_length  tab_width  ramp_length

so the full footprint per tab is `tab_width + 2 * ramp_length`. Over the
plateau the tool sits at `tab_top_z = target_z + tab_height`, leaving
`tab_height` of stock as the bridge.

Z modulation runs per pass. For a pass at `pass_z`:
  - If `pass_z >= tab_top_z` (pass is shallower than the tab top), the
    tab isn't breached and the pass cuts as normal.
  - Otherwise, the pass walks the contour, ramping up to `tab_top_z`
    over each tab's entry ramp, holding through the plateau, and ramping
    back down on the exit. This naturally handles the "multiple final
    passes straddle the tab" case — every breaching pass modulates.
"""
from __future__ import annotations

from pymillcam.core.segments import (
    ArcSegment,
    LineSegment,
    Segment,
    split_full_circle,
    split_segment_at_length,
)
from pymillcam.engine.ir import IRInstruction, MoveType

_LENGTH_EPSILON = 1e-9


class TabPlacementError(Exception):
    """Raised when tabs cannot be placed on a contour."""


def compute_tab_intervals(
    contour_length: float,
    count: int,
    tab_width: float,
    ramp_length: float,
) -> list[tuple[float, float]]:
    """Return arc-length intervals (s_start, s_end) for each auto-placed tab.

    Centers sit at (i + 0.5) * (contour_length / count) so the tabs are
    interior to the contour and don't collide with the seam at s=0.
    Raises if any tab footprint would overlap a neighbour or the seam.
    """
    if count <= 0:
        return []
    half_footprint = tab_width / 2.0 + ramp_length
    spacing = contour_length / count
    if 2.0 * half_footprint > spacing + _LENGTH_EPSILON:
        raise TabPlacementError(
            f"{count} tabs need spacing >= {2.0 * half_footprint:.3f} mm; "
            f"contour gives {spacing:.3f} mm. Reduce count, width, or ramp_length."
        )
    return [
        ((i + 0.5) * spacing - half_footprint, (i + 0.5) * spacing + half_footprint)
        for i in range(count)
    ]


def split_chain_at_lengths(
    segments: list[Segment], cuts: list[float]
) -> list[Segment]:
    """Split a chain at multiple arc-length cut points, returning a flat
    chain where each cut is a segment boundary. Cuts outside (0, total)
    are ignored.
    """
    total = sum(s.length for s in segments)
    valid = sorted(
        c for c in cuts if _LENGTH_EPSILON < c < total - _LENGTH_EPSILON
    )
    if not valid:
        return list(segments)
    out: list[Segment] = []
    queue = list(segments)
    seg_start = 0.0
    cut_idx = 0
    while queue and cut_idx < len(valid):
        seg = queue.pop(0)
        seg_end = seg_start + seg.length
        # Apply every cut falling inside this segment, in order.
        while cut_idx < len(valid) and valid[cut_idx] < seg_end - _LENGTH_EPSILON:
            local = valid[cut_idx] - seg_start
            cut_idx += 1
            if local <= _LENGTH_EPSILON:
                continue
            seg_a, seg_b = split_segment_at_length(seg, local)
            out.append(seg_a)
            seg_start += seg_a.length
            seg = seg_b
            seg_end = seg_start + seg.length
        out.append(seg)
        seg_start = seg_end
    out.extend(queue)
    return out


def effective_z_at(
    s: float,
    *,
    pass_z: float,
    tab_top_z: float,
    intervals: list[tuple[float, float]],
    ramp_length: float,
) -> float:
    """Z height the tool should be at, at arc-length `s` along the contour.

    `pass_z` outside any tab interval; linearly interpolated up to
    `tab_top_z` on the entry ramp, held there on the plateau, and
    interpolated back down on the exit ramp.
    """
    for s_start, s_end in intervals:
        if s < s_start - _LENGTH_EPSILON or s > s_end + _LENGTH_EPSILON:
            continue
        if ramp_length <= _LENGTH_EPSILON:
            return tab_top_z
        if s <= s_start + ramp_length:
            t = (s - s_start) / ramp_length
            return pass_z + t * (tab_top_z - pass_z)
        if s >= s_end - ramp_length:
            t = (s_end - s) / ramp_length
            return pass_z + t * (tab_top_z - pass_z)
        return tab_top_z
    return pass_z


def emit_pass_with_tabs(
    instructions: list[IRInstruction],
    segments: list[Segment],
    *,
    pass_z: float,
    tab_top_z: float,
    intervals: list[tuple[float, float]],
    ramp_length: float,
    feed_xy: float,
    feed_z: float,
) -> None:
    """Walk a contour at `pass_z`, lifting Z to `tab_top_z` across each tab.

    Pre-splits the chain at every tab boundary (entry-start, plateau-start,
    plateau-end, exit-end) so each emitted segment carries a single linear
    Z change. The tool descends to `pass_z` first, then segments encode
    their own Z transitions.
    """
    if not segments:
        return
    cuts: list[float] = []
    for s_start, s_end in intervals:
        cuts.extend([s_start, s_start + ramp_length, s_end - ramp_length, s_end])
    pieces = split_chain_at_lengths(segments, cuts)

    instructions.append(
        IRInstruction(type=MoveType.FEED, z=pass_z, f=feed_z)
    )
    accum = 0.0
    for piece in pieces:
        # A full-circle arc piece (no tab inside this region) would emit
        # one ambiguous G2/G3 with start XY == end XY. Split into halves.
        sub_pieces: list[Segment]
        if isinstance(piece, ArcSegment) and piece.is_full_circle:
            a, b = split_full_circle(piece)
            sub_pieces = [a, b]
        else:
            sub_pieces = [piece]
        for sub in sub_pieces:
            s_b = accum + sub.length
            accum = s_b
            z_end = effective_z_at(
                s_b,
                pass_z=pass_z,
                tab_top_z=tab_top_z,
                intervals=intervals,
                ramp_length=ramp_length,
            )
            if isinstance(sub, LineSegment):
                ex, ey = sub.end
                instructions.append(
                    IRInstruction(type=MoveType.FEED, x=ex, y=ey, z=z_end, f=feed_xy)
                )
            elif isinstance(sub, ArcSegment):
                sx, sy = sub.start
                ex, ey = sub.end
                cx, cy = sub.center
                move_type = MoveType.ARC_CCW if sub.ccw else MoveType.ARC_CW
                instructions.append(
                    IRInstruction(
                        type=move_type,
                        x=ex,
                        y=ey,
                        z=z_end,
                        i=cx - sx,
                        j=cy - sy,
                        f=feed_xy,
                    )
                )
