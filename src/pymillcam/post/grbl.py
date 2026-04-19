"""GRBL post-processor.

Targets GRBL 1.1+, the firmware running on most hobby-grade CNC
controllers (Arduino-based routers, 3018-class machines, grblHAL-based
boards). The G-code dialect is a near-subset of UCCNC's:

- G21 G90           — mm + absolute (GRBL's defaults, stated explicitly
                       for safety). G94 / G17 are also defaults but are
                       silently ignored / rejected on some older GRBL
                       builds, so we leave them out of the preamble.
- G0 / G1           — rapid / feed
- G2 / G3           — arcs, incremental I/J (same as UCCNC)
- M3 Snnnn / M5     — spindle on / off
- G4 P<seconds>     — dwell
- M0                — program pause (useful for manual tool change)
- M30               — program end

Tool change on stock GRBL is a no-op — there's no ``M6`` handler, so the
default ``tool_change`` macro pauses with ``M0`` and surfaces the target
tool number in a comment. ATC / tool-length-probe setups override this
via ``MachineDefinition.macros``.
"""
from __future__ import annotations

from pymillcam.post._basic import BasicGcodePost


class GrblPostProcessor(BasicGcodePost):
    name = "GRBL"
    default_macros = {
        "program_start": "G21 G90",
        "program_end": "M5\nM30",
        # Stock GRBL ignores ``Tn M6`` — pause and let the operator
        # swap tools. ``{tool_number}`` is substituted so the comment
        # reminds the operator which tool to load.
        "tool_change": "M5\nM0 (Change to T{tool_number})",
    }
