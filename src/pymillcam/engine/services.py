"""ToolpathService — the facade the UI talks to instead of the engine.

Without this, every UI surface (MainWindow, wizards, future dock widgets)
has to know which engine function to call for which op type. That turns
into a combinatorial mess as Phase 4 adds drill / surface / engrave /
v-carve / contour operations — MainWindow would carry 5+ ``isinstance``
chains and 5+ engine imports.

The service keeps two registries — one for previews, one for toolpath
generation — keyed by op type. New op types register themselves (ideally
inside the engine module that implements them), so the UI code never
needs to know about them. ``generate_program`` assembles the whole
project through a post-processor.

Errors propagate as ``EngineError`` (or subclasses). The UI catches once,
at a high level — not per op type.
"""
from __future__ import annotations

from collections.abc import Callable

from pymillcam.core.operations import DrillOp, Operation, PocketOp, ProfileOp
from pymillcam.core.project import Project
from pymillcam.core.segments import Segment
from pymillcam.engine.drill import compute_drill_preview, generate_drill_toolpath
from pymillcam.engine.ir import Toolpath
from pymillcam.engine.pocket import compute_pocket_preview, generate_pocket_toolpath
from pymillcam.engine.profile import (
    compute_profile_preview,
    generate_profile_toolpath,
)
from pymillcam.post.base import PostProcessor

PreviewFn = Callable[[Operation, Project], list[Segment]]
ToolpathFn = Callable[[Operation, Project], Toolpath]


class ToolpathService:
    """Dispatch project operations to their preview / toolpath generators.

    Thread a single instance through the UI — the registries are mutable
    only via ``register_*``, so Phase 4 op types plug in at init time and
    the service otherwise looks like a pure function namespace.
    """

    def __init__(self) -> None:
        self._preview_fns: dict[type[Operation], PreviewFn] = {}
        self._toolpath_fns: dict[type[Operation], ToolpathFn] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Default registrations for the op types shipped in core today.

        Phase 4 op types should call ``register_preview`` /
        ``register_toolpath`` from their own module so they don't need
        to be listed here.
        """
        self.register_preview(ProfileOp, compute_profile_preview)
        self.register_preview(PocketOp, compute_pocket_preview)
        self.register_preview(DrillOp, compute_drill_preview)
        self.register_toolpath(ProfileOp, generate_profile_toolpath)
        self.register_toolpath(PocketOp, generate_pocket_toolpath)
        self.register_toolpath(DrillOp, generate_drill_toolpath)

    # ------------------------------------------------------------- registry

    def register_preview(
        self, op_type: type[Operation], fn: PreviewFn
    ) -> None:
        self._preview_fns[op_type] = fn

    def register_toolpath(
        self, op_type: type[Operation], fn: ToolpathFn
    ) -> None:
        self._toolpath_fns[op_type] = fn

    def supports(self, op: Operation) -> bool:
        """True if this op type has both a preview and a toolpath generator."""
        op_type = type(op)
        return op_type in self._preview_fns and op_type in self._toolpath_fns

    # ------------------------------------------------------------- dispatch

    def compute_preview(
        self, op: Operation, project: Project
    ) -> list[Segment]:
        """Return the plan-view path the cutter centre will follow.

        Returns ``[]`` when the op's type has no registered preview —
        the UI treats that as "no preview available" rather than an
        error, since users can edit an op of any type and we don't
        want live editing to blow up on unsupported types.
        """
        fn = self._preview_fns.get(type(op))
        if fn is None:
            return []
        return fn(op, project)

    def generate_toolpath(
        self, op: Operation, project: Project
    ) -> Toolpath | None:
        """Return IR for one op. ``None`` if disabled or unsupported.

        Errors (EngineError subclasses) propagate to the caller so a
        generation failure on one op aborts the program — partial
        G-code with missing ops would be dangerous.
        """
        if not op.enabled:
            return None
        fn = self._toolpath_fns.get(type(op))
        if fn is None:
            return None
        return fn(op, project)

    def generate_program(
        self, project: Project, post: PostProcessor
    ) -> tuple[str, list[Toolpath]]:
        """Generate the complete G-code program for ``project``.

        Returns the G-code string plus the IR toolpaths that fed into
        it (so the UI can overlay the actual moves). Raises on the first
        ``EngineError`` — callers should catch and surface the error
        without shipping partial G-code.
        """
        toolpaths: list[Toolpath] = []
        for op in project.operations:
            tp = self.generate_toolpath(op, project)
            if tp is not None:
                toolpaths.append(tp)
        gcode = post.post_program(toolpaths)
        return gcode, toolpaths
