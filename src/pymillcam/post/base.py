"""Post-processor protocol — translate IR to controller-specific G-code."""
from __future__ import annotations

from typing import Protocol

from pymillcam.engine.ir import Toolpath


class PostProcessor(Protocol):
    """Translate one or more IR Toolpaths into a complete G-code program.

    Concrete implementations (UCCNC, Mach3, GRBL, LinuxCNC) handle dialect
    differences — preamble, tool change syntax, arc I/J conventions, and so on.
    """

    name: str

    def post_program(self, toolpaths: list[Toolpath]) -> str:
        """Return a complete G-code program as a single string.

        The result includes a program-level preamble and footer (units,
        absolute coords, spindle off, program end) so it can be fed
        directly to the controller.
        """
        ...
