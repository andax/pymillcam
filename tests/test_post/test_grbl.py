"""Tests for the GRBL post-processor.

GRBL shares almost everything with UCCNC — same IR, same motion codes,
same arc format. We only assert the dialect-specific defaults here;
the cross-controller formatting (G0/G1/G2/G3, feed modality,
``{tool_number}`` substitution, comment passthrough) is already
covered by ``test_uccnc.py`` through the shared ``BasicGcodePost``.
"""
from __future__ import annotations

from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath
from pymillcam.post.grbl import GrblPostProcessor


def test_default_preamble_omits_uccnc_only_codes() -> None:
    """``G94`` / ``G17`` are UCCNC defaults; they're also GRBL defaults but
    some older GRBL builds reject the explicit statement. The GRBL post
    emits just ``G21 G90``."""
    out = GrblPostProcessor().post_program([])
    lines = out.splitlines()
    assert lines[1] == "G21 G90"


def test_default_tool_change_pauses_instead_of_m6() -> None:
    """Stock GRBL has no ``M6`` handler, so the default ``tool_change``
    macro pauses with ``M0`` and leaves a comment naming the target tool.
    """
    tp = Toolpath(
        operation_name="op",
        tool_number=2,
        instructions=[IRInstruction(type=MoveType.TOOL_CHANGE, tool_number=2)],
    )
    out = GrblPostProcessor().post_program([tp])
    assert "M0 (Change to T2)" in out
    # No inline T<n> M6 — GRBL would either ignore it or warn.
    assert "T2 M6" not in out


def test_custom_macros_override_grbl_defaults() -> None:
    """Per-project macros take priority over the GRBL dialect defaults —
    the customisation layer is controller-agnostic."""
    out = GrblPostProcessor().post_program(
        [], macros={"program_start": "(CUSTOM)"}
    )
    assert "(CUSTOM)" in out
    assert "G21 G90" not in out


def test_motion_codes_match_uccnc() -> None:
    """Sanity: GRBL and UCCNC emit identical G0/G1/G2/G3 lines for the
    same IR. Catches accidental divergence in the shared formatter."""
    from pymillcam.post.uccnc import UccncPostProcessor
    tp = Toolpath(
        operation_name="op",
        tool_number=1,
        instructions=[
            IRInstruction(type=MoveType.RAPID, x=1.0, y=2.0, z=3.0),
            IRInstruction(type=MoveType.FEED, x=4.0, y=5.0, f=1200.0),
        ],
    )
    grbl_out = GrblPostProcessor().post_program([tp])
    uccnc_out = UccncPostProcessor().post_program([tp])
    # Strip the preamble + footer (which differ) and compare the body.
    grbl_body = [
        line for line in grbl_out.splitlines()
        if line.startswith(("G0 ", "G1 ", "G2 ", "G3 "))
    ]
    uccnc_body = [
        line for line in uccnc_out.splitlines()
        if line.startswith(("G0 ", "G1 ", "G2 ", "G3 "))
    ]
    assert grbl_body == uccnc_body
