"""Smoke tests for the wizard scaffolding.

These don't exercise any concrete wizard yet — they just verify the
contract that ``BaseWizard`` applies its pages on Accepted and leaves
the project untouched on Rejected, and that ``OperationFormPage``
round-trips form edits to the draft op.
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QWizard
from pytestqt.qtbot import QtBot

from pymillcam.core.operations import ProfileOp
from pymillcam.core.project import Project
from pymillcam.ui.properties_panel import FORM_REGISTRY
from pymillcam.ui.wizards.base import (
    BaseWizard,
    BaseWizardPage,
    OperationFormPage,
)


class _RecordingPage(BaseWizardPage):
    """Test double: records the order in which apply was called."""

    def __init__(self, log: list[str], tag: str) -> None:
        super().__init__()
        self.setTitle(tag)
        self._log = log
        self._tag = tag

    def apply(self, project: Project) -> None:
        self._log.append(self._tag)


def test_apply_runs_on_accept(qtbot: QtBot) -> None:
    """Finishing the wizard runs apply on each page in page-ID order."""
    project = Project()
    wizard = BaseWizard(project)
    qtbot.addWidget(wizard)
    log: list[str] = []
    wizard.addPage(_RecordingPage(log, "first"))
    wizard.addPage(_RecordingPage(log, "second"))

    wizard.done(QWizard.DialogCode.Accepted)

    assert log == ["first", "second"]


def test_apply_is_skipped_on_reject(qtbot: QtBot) -> None:
    """Cancelling the wizard doesn't touch project state."""
    project = Project()
    wizard = BaseWizard(project)
    qtbot.addWidget(wizard)
    log: list[str] = []
    wizard.addPage(_RecordingPage(log, "only"))

    wizard.done(QWizard.DialogCode.Rejected)

    assert log == []


def test_apply_runs_only_once(qtbot: QtBot) -> None:
    """Accidental double-finish doesn't apply the pages twice."""
    project = Project()
    wizard = BaseWizard(project)
    qtbot.addWidget(wizard)
    log: list[str] = []
    wizard.addPage(_RecordingPage(log, "once"))

    wizard.done(QWizard.DialogCode.Accepted)
    # Simulate an unusual re-emit — the base should still only apply once.
    wizard.finished.emit(QWizard.DialogCode.Accepted)

    assert log == ["once"]


def test_operation_form_page_writes_user_edits_to_draft(qtbot: QtBot) -> None:
    """Editing a field in the wizard's form keeps the draft op in sync."""
    project = Project()
    wizard = BaseWizard(project)
    qtbot.addWidget(wizard)
    draft = ProfileOp(name="draft", cut_depth=-1.0)
    form_cls = FORM_REGISTRY[ProfileOp]
    form = form_cls()
    page = OperationFormPage(form, draft, title="Profile")
    wizard.addPage(page)

    # Simulate a user edit — the form's signal fires through the page's
    # write_back, mutating the draft in place.
    form.cut_depth.setValue(-4.2)

    assert draft.cut_depth == pytest.approx(-4.2)


def test_operation_form_page_attaches_op_on_finish(qtbot: QtBot) -> None:
    """A finished OperationFormPage appends its draft to project.operations."""
    project = Project()
    wizard = BaseWizard(project)
    qtbot.addWidget(wizard)
    draft = ProfileOp(name="new op")
    form_cls = FORM_REGISTRY[ProfileOp]
    page = OperationFormPage(form_cls(), draft, title="Profile")
    wizard.addPage(page)

    wizard.done(QWizard.DialogCode.Accepted)

    assert project.operations == [draft]


def test_operation_form_page_does_not_attach_on_cancel(qtbot: QtBot) -> None:
    """Cancelling the wizard leaves project.operations empty."""
    project = Project()
    wizard = BaseWizard(project)
    qtbot.addWidget(wizard)
    draft = ProfileOp(name="discarded")
    form_cls = FORM_REGISTRY[ProfileOp]
    page = OperationFormPage(form_cls(), draft, title="Profile")
    wizard.addPage(page)

    wizard.done(QWizard.DialogCode.Rejected)

    assert project.operations == []
