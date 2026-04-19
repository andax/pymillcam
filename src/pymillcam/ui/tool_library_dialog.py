"""Tool Library dialog.

Browse / edit the application-wide ``ToolLibrary``. The dialog edits a
deep copy of the library so Cancel truly discards changes; the caller
reads ``result_library()`` on Accepted and persists via
``core.tool_library.save_library``.

Layout:

    +----------------+-------------------------------+
    |  Tool list     |  Tool edit form               |
    |  [default*]    |                               |
    |                |  Name, shape, diameter,       |
    |                |  flutes, stickout, feeds,     |
    |                |  RPM, notes                   |
    |                |                               |
    |                |  [Set as default]             |
    |                |                               |
    |  [+] [−]       |                               |
    +----------------+-------------------------------+
    |              [Cancel]  [OK]                    |
    +------------------------------------------------+

The default tool is marked in the list with a leading asterisk. Editing
a field immediately updates the in-memory copy; the list refreshes
when the name or diameter change so the user sees their edits reflect.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pymillcam.core.tool_library import ToolLibrary
from pymillcam.core.tools import Tool, ToolShape

LIBRARY_FILENAME = "tool_library.json"


def default_library_path() -> Path:
    """Per-platform path for the tool library file.

    Sits in the same folder as ``preferences.json`` so a user wiping
    their PyMillCAM config (or backing it up) gets everything in one
    place.
    """
    base = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppConfigLocation
        )
    )
    return base / LIBRARY_FILENAME


class ToolLibraryDialog(QDialog):
    """Modal dialog for editing a ``ToolLibrary``."""

    def __init__(
        self, library: ToolLibrary, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tool Library")
        self.resize(640, 480)
        self._library = library.model_copy(deep=True)
        self._selected_index: int | None = None
        # Guard against field-change signals firing while we programmatically
        # populate the form for a newly-selected tool — otherwise populate
        # would turn into write_back and corrupt the model.
        self._suspend_writeback = False

        self._build_ui()
        self._rebuild_list(select=0 if self._library.tools else None)

    # ------------------------------------------------------------- layout

    def _build_ui(self) -> None:
        # Left column: tool list + add/remove buttons.
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_selection_changed)

        self._btn_new = QPushButton("New")
        self._btn_new.clicked.connect(self._on_new)
        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_default = QPushButton("Set as default")
        self._btn_default.clicked.connect(self._on_set_default)

        list_buttons = QHBoxLayout()
        list_buttons.addWidget(self._btn_new)
        list_buttons.addWidget(self._btn_delete)

        left = QVBoxLayout()
        left.addWidget(self._list, stretch=1)
        left.addLayout(list_buttons)
        left.addWidget(self._btn_default)

        left_container = QWidget()
        left_container.setLayout(left)
        left_container.setFixedWidth(240)

        # Right column: edit form for the currently-selected tool.
        self._form = self._build_form()

        middle = QHBoxLayout()
        middle.addWidget(left_container)
        middle.addWidget(self._form, stretch=1)

        # Bottom: OK / Cancel.
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addLayout(middle, stretch=1)
        outer.addWidget(self._buttons)

    def _build_form(self) -> QWidget:
        self._name = QLineEdit()
        self._shape = QComboBox()
        self._shape.addItems([s.value for s in ToolShape])

        self._diameter = _mm_spin(0.05, 100.0, step=0.5, decimals=3)
        self._flute_length = _mm_spin(1.0, 500.0, step=1.0, decimals=2)
        self._total_length = _mm_spin(1.0, 500.0, step=1.0, decimals=2)
        self._shank_diameter = _mm_spin(0.05, 100.0, step=0.5, decimals=3)
        self._flute_count = QSpinBox()
        self._flute_count.setRange(1, 16)

        # ToolController runtime defaults — stored on the Tool's
        # cutting_data under a "default" key so they round-trip through
        # save/load. Later, the wizard's tool-step will expose
        # per-material cutting_data instead; this is the one-material
        # path for the MVP.
        self._spindle_rpm = QSpinBox()
        self._spindle_rpm.setRange(0, 100_000)
        self._spindle_rpm.setSingleStep(500)
        self._spindle_rpm.setSuffix(" rpm")

        self._feed_xy = QDoubleSpinBox()
        self._feed_xy.setRange(0.0, 20_000.0)
        self._feed_xy.setDecimals(1)
        self._feed_xy.setSingleStep(100.0)
        self._feed_xy.setSuffix(" mm/min")

        self._feed_z = QDoubleSpinBox()
        self._feed_z.setRange(0.0, 20_000.0)
        self._feed_z.setDecimals(1)
        self._feed_z.setSingleStep(50.0)
        self._feed_z.setSuffix(" mm/min")

        self._stepdown = _mm_spin(0.01, 100.0, step=0.1, decimals=3)

        self._supplier = QLineEdit()
        self._part_number = QLineEdit()
        self._notes = QLineEdit()

        form = QFormLayout()
        form.addRow("Name", self._name)
        form.addRow("Shape", self._shape)
        form.addRow("Diameter", self._diameter)
        form.addRow("Flute length", self._flute_length)
        form.addRow("Total length", self._total_length)
        form.addRow("Shank diameter", self._shank_diameter)
        form.addRow("Flute count", self._flute_count)
        form.addRow(QLabel("<b>Cutting data</b>"))
        self._btn_calc_feeds_speeds = QPushButton("Calculate from material…")
        self._btn_calc_feeds_speeds.clicked.connect(
            self._on_calc_feeds_speeds
        )
        form.addRow("", self._btn_calc_feeds_speeds)
        form.addRow("Spindle RPM", self._spindle_rpm)
        form.addRow("Feed XY", self._feed_xy)
        form.addRow("Feed Z", self._feed_z)
        form.addRow("Stepdown", self._stepdown)
        form.addRow(QLabel("<b>Bookkeeping</b>"))
        form.addRow("Supplier", self._supplier)
        form.addRow("Part number", self._part_number)
        form.addRow("Notes", self._notes)

        widget = QWidget()
        widget.setLayout(form)
        widget.setEnabled(False)  # disabled until a tool is selected

        # Hook everything up.
        self._name.textEdited.connect(self._write_back_name)
        self._shape.currentTextChanged.connect(self._write_back_plain)
        for w in (
            self._diameter,
            self._flute_length,
            self._total_length,
            self._shank_diameter,
            self._spindle_rpm,
            self._feed_xy,
            self._feed_z,
            self._stepdown,
            self._flute_count,
        ):
            w.valueChanged.connect(self._write_back_plain)
        for w in (self._supplier, self._part_number, self._notes):
            w.textEdited.connect(self._write_back_plain)

        return widget

    # --------------------------------------------------- list / selection

    def _rebuild_list(self, *, select: int | None = None) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for tool in self._library.tools:
            marker = "★ " if tool.id == self._library.default_tool_id else ""
            diameter = float(tool.geometry.get("diameter", 0.0))
            label = f"{marker}{tool.name}  ({diameter:g} mm)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, tool.id)
            self._list.addItem(item)
        self._list.blockSignals(False)
        if select is not None and 0 <= select < len(self._library.tools):
            self._list.setCurrentRow(select)
        elif not self._library.tools:
            self._selected_index = None
            self._form.setEnabled(False)
            self._btn_delete.setEnabled(False)
            self._btn_default.setEnabled(False)

    def _on_selection_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._library.tools):
            self._selected_index = None
            self._form.setEnabled(False)
            self._btn_delete.setEnabled(False)
            self._btn_default.setEnabled(False)
            return
        self._selected_index = row
        self._form.setEnabled(True)
        self._btn_delete.setEnabled(True)
        self._btn_default.setEnabled(True)
        self._populate_form(self._library.tools[row])

    def _populate_form(self, tool: Tool) -> None:
        self._suspend_writeback = True
        try:
            self._name.setText(tool.name)
            self._shape.setCurrentText(tool.shape.value)
            self._diameter.setValue(float(tool.geometry.get("diameter", 3.0)))
            self._flute_length.setValue(
                float(tool.geometry.get("flute_length", 15.0))
            )
            self._total_length.setValue(
                float(tool.geometry.get("total_length", 50.0))
            )
            self._shank_diameter.setValue(
                float(tool.geometry.get("shank_diameter", 3.0))
            )
            self._flute_count.setValue(
                int(tool.geometry.get("flute_count", 2))
            )
            cd = tool.cutting_data.get("default")
            self._spindle_rpm.setValue(cd.spindle_rpm if cd else 18000)
            self._feed_xy.setValue(cd.feed_xy if cd else 1200.0)
            self._feed_z.setValue(cd.feed_z if cd else 300.0)
            self._stepdown.setValue(cd.stepdown if cd else 1.0)
            self._supplier.setText(tool.supplier)
            self._part_number.setText(tool.part_number)
            self._notes.setText(tool.notes)
        finally:
            self._suspend_writeback = False

    # ----------------------------------------------------------- write-back

    def _current_tool(self) -> Tool | None:
        if self._selected_index is None:
            return None
        return self._library.tools[self._selected_index]

    def _write_back_plain(self, *_: object) -> None:
        """Field-change handler for any input that doesn't influence the
        list label — just sync the model, no list rebuild."""
        if self._suspend_writeback:
            return
        self._apply_form_to_tool()

    def _write_back_name(self, *_: object) -> None:
        """Name changes also change the label in the left-hand list."""
        if self._suspend_writeback:
            return
        self._apply_form_to_tool()
        # Surgical list refresh: just update the current item's text.
        item = self._list.currentItem()
        tool = self._current_tool()
        if item is not None and tool is not None:
            marker = "★ " if tool.id == self._library.default_tool_id else ""
            diameter = float(tool.geometry.get("diameter", 0.0))
            item.setText(f"{marker}{tool.name}  ({diameter:g} mm)")

    def _apply_form_to_tool(self) -> None:
        tool = self._current_tool()
        if tool is None:
            return
        from pymillcam.core.tools import CuttingData

        tool.name = self._name.text() or "Unnamed tool"
        tool.shape = ToolShape(self._shape.currentText())
        tool.geometry["diameter"] = self._diameter.value()
        tool.geometry["flute_length"] = self._flute_length.value()
        tool.geometry["total_length"] = self._total_length.value()
        tool.geometry["shank_diameter"] = self._shank_diameter.value()
        tool.geometry["flute_count"] = self._flute_count.value()
        tool.cutting_data["default"] = CuttingData(
            spindle_rpm=self._spindle_rpm.value(),
            feed_xy=self._feed_xy.value(),
            feed_z=self._feed_z.value(),
            stepdown=self._stepdown.value(),
        )
        tool.supplier = self._supplier.text()
        tool.part_number = self._part_number.text()
        tool.notes = self._notes.text()

    # ---------------------------------------------------------- actions

    def _on_new(self) -> None:
        """Create a blank 3 mm endmill entry and select it."""
        from pymillcam.core.tools import CuttingData

        new_tool = Tool(
            name=f"New tool {len(self._library.tools) + 1}",
            shape=ToolShape.ENDMILL,
        )
        new_tool.cutting_data["default"] = CuttingData()
        self._library.add(new_tool)
        if self._library.default_tool_id is None:
            # First tool — promote to default so the library has a
            # usable default right away.
            self._library.default_tool_id = new_tool.id
        self._rebuild_list(select=len(self._library.tools) - 1)

    def _on_delete(self) -> None:
        tool = self._current_tool()
        if tool is None:
            return
        idx = self._selected_index or 0
        self._library.remove(tool.id)
        # Select the neighbouring tool after deletion so the user keeps
        # editing context.
        if self._library.tools:
            self._rebuild_list(select=min(idx, len(self._library.tools) - 1))
        else:
            self._selected_index = None
            self._rebuild_list()

    def _on_set_default(self) -> None:
        tool = self._current_tool()
        if tool is None:
            return
        self._library.default_tool_id = tool.id
        self._rebuild_list(select=self._selected_index)

    def _on_calc_feeds_speeds(self) -> None:
        """Open the feeds/speeds calculator; on Apply, write the
        suggested RPM and feed back into the form. The same widgets'
        ``valueChanged`` signals then propagate the numbers through
        ``_write_back_plain`` into the selected tool, so no extra
        bookkeeping is needed here."""
        from pymillcam.ui.feeds_speeds_dialog import FeedsSpeedsDialog

        dialog = FeedsSpeedsDialog(
            tool_diameter_mm=self._diameter.value(),
            flute_count=self._flute_count.value(),
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        rpm, feed = dialog.result()
        self._spindle_rpm.setValue(rpm)
        self._feed_xy.setValue(feed)
        # Feed Z defaults to ~1/3 of feed_xy for a plunge move — safer
        # than matching the horizontal feed on flat end mills, which
        # routinely break when plunging. Users can tweak afterwards.
        self._feed_z.setValue(feed / 3.0)

    # ---------------------------------------------------------- result

    def result_library(self) -> ToolLibrary:
        """Return the edited library. Callers persist it."""
        return self._library.model_copy(deep=True)


def _mm_spin(
    low: float, high: float, *, step: float = 0.5, decimals: int = 3
) -> QDoubleSpinBox:
    w = QDoubleSpinBox()
    w.setRange(low, high)
    w.setDecimals(decimals)
    w.setSingleStep(step)
    w.setSuffix(" mm")
    return w
