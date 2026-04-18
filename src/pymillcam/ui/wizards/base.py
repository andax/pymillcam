"""Wizard scaffolding.

Concrete wizards (SheetCutoutWizard, PocketWizard, DrillPatternWizard, ...)
subclass ``BaseWizard`` and compose ``BaseWizardPage`` instances. The
base handles:

- Button layout (Back / Next / Finish / Cancel), styling, and shortcuts
  — on top of Qt's ``QWizard``, which does the heavy lifting.
- An ``apply(project)`` convention: after the user hits Finish, the
  wizard walks its pages in order and calls ``page.apply(project)``
  on each. Pages that don't mutate project state leave ``apply`` as
  a no-op.
- ``OperationFormPage`` — the glue between the wizard and the
  ``OperationFormBase`` forms from the Properties panel. The same
  form widget is used in both surfaces; we don't maintain two copies.
"""
from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget, QWizard, QWizardPage

from pymillcam.core.operations import Operation
from pymillcam.core.project import Project
from pymillcam.ui.properties_panel import OperationFormBase


class BaseWizardPage(QWizardPage):
    """Base for every page in a PyMillCAM wizard.

    Subclasses build their UI in ``__init__`` and implement
    ``apply(project)`` if they need to mutate project state on Finish.
    Validation beyond "all fields non-empty" goes in ``validatePage``
    (standard QWizardPage hook).
    """

    def apply(self, project: Project) -> None:
        """Persist this page's state onto the project.

        Called by ``BaseWizard`` for each page in page-ID order after
        Finish is accepted. Default: no-op.
        """


class BaseWizard(QWizard):
    """Standard shape for PyMillCAM wizards.

    Pages are added via ``QWizard.addPage``. On Finish, each page's
    ``apply(project)`` runs in page order; on Cancel / Esc, nothing
    is applied. The wizard itself is a modal dialog; callers should
    ``exec()`` it and re-read ``project`` on Accepted.
    """

    def __init__(
        self,
        project: Project,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._project = project
        # ModernStyle renders consistently across Linux / macOS / Windows
        # and matches native dialog chrome better than AeroStyle or
        # MacStyle. ClassicStyle is too dated.
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        # Qt emits `finished(result)` with Accepted on Finish and
        # Rejected on Cancel. Route Accepted through apply.
        self.finished.connect(self._on_finished)
        # Guard against double-apply if apply were somehow called twice
        # (e.g., a buggy subclass calling accept() twice).
        self._applied = False

    @property
    def project(self) -> Project:
        return self._project

    def _on_finished(self, result: int) -> None:
        if result != QWizard.DialogCode.Accepted:
            return
        if self._applied:
            return
        self._applied = True
        for page_id in self.pageIds():
            page = self.page(page_id)
            if isinstance(page, BaseWizardPage):
                page.apply(self._project)


class OperationFormPage(BaseWizardPage):
    """Wizard page wrapping an ``OperationFormBase`` widget.

    The page takes a draft operation (created with the wizard's
    defaults) and the form widget for its type. It:

    - Binds the form to the draft on construction.
    - Routes the form's ``field_changed`` signal to ``write_back`` so
      the draft is kept in sync as the user edits.
    - On ``apply``, appends the draft to ``project.operations``. Subclass
      and override ``apply`` if a wizard attaches the op differently
      (e.g., a multi-region wizard that creates several ops).
    """

    def __init__(
        self,
        form: OperationFormBase,
        operation: Operation,
        title: str,
        subtitle: str | None = None,
    ) -> None:
        super().__init__()
        self.setTitle(title)
        if subtitle is not None:
            self.setSubTitle(subtitle)
        self._form = form
        self._operation = operation
        form.bind(operation, None)
        form.field_changed.connect(self._write_back)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(form)

    @property
    def operation(self) -> Operation:
        return self._operation

    def _write_back(self) -> None:
        self._form.write_back(self._operation, None)

    def apply(self, project: Project) -> None:
        project.operations.append(self._operation)
