"""UCCNC post-processor.

Emits G-code for the UCCNC controller. Conventions:

- G21 G90 G94 G17  — mm, absolute, feed per minute, XY plane
- G0 / G1          — rapid / linear feed
- G2 / G3          — CW / CCW arc, centre specified by incremental I/J
- M3 Snnnn / M5    — spindle on (CW) at given RPM / off
- Tnn M6           — tool change (overridable via the ``tool_change`` macro)
- ( ... )          — inline comment
- M30              — program end

Every motion line re-emits its G-word (no implicit modality) for clarity;
controllers accept the redundancy. Feed-rate modality is handled: F is
emitted only when it changes, since UCCNC's look-ahead treats a new F
as a real command.

Machine macros (``program_start`` / ``program_end`` / ``tool_change``)
are substituted at well-defined points so a single project can target
different machines (manual-change vs ATC, probed vs un-probed, parked-
to-home vs parked-to-origin) by swapping the ``MachineDefinition``.
"""
from __future__ import annotations

from pymillcam.post._basic import BasicGcodePost


class UccncPostProcessor(BasicGcodePost):
    name = "UCCNC"
    # Dialect-neutral defaults — reproduce the original hardcoded
    # behaviour. Users override per-machine (park moves, probing,
    # manual-change pauses) via ``MachineDefinition.macros``.
    default_macros = {
        "program_start": "G21 G90 G94 G17",
        "program_end": "M5\nM30",
        "tool_change": "T{tool_number} M6",
    }
