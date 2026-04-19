"""PyMillCAM entry point."""
import sys

# Input-widget border styling — QLineEdit / QPlainTextEdit / QComboBox /
# QAbstractSpinBox. Qt's platform-default borders blend into the window
# background on several Linux themes, so form fields look like plain
# text blocks and dialogs feel chaotic. A thin ``palette(mid)`` border
# plus a slightly thicker ``palette(highlight)`` focus ring makes each
# input obviously interactive without fighting the host theme. The
# 1 px padding on focus keeps the widget the same total size so the
# form doesn't twitch when focus moves.
_INPUT_WIDGET_STYLESHEET = """
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QAbstractSpinBox {
    border: 1px solid palette(mid);
    border-radius: 3px;
    padding: 2px 4px;
    background: palette(base);
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QComboBox:focus, QAbstractSpinBox:focus {
    border: 2px solid palette(highlight);
    padding: 1px 3px;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QComboBox:disabled, QAbstractSpinBox:disabled {
    border: 1px solid palette(midlight);
    color: palette(mid);
}
"""


def main() -> None:
    """Launch PyMillCAM application."""
    from PySide6.QtWidgets import QApplication

    from pymillcam.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    # Only applicationName is set — Qt's QStandardPaths.AppConfigLocation
    # resolves to `<GenericConfigLocation>/<OrganizationName>/<ApplicationName>/`
    # when both are set, so identical strings produce the ugly double-nested
    # `~/.config/PyMillCAM/PyMillCAM/`. With just the app name Qt gives
    # `~/.config/PyMillCAM/` on Linux, `%APPDATA%\PyMillCAM\` on Windows,
    # and `~/Library/Application Support/PyMillCAM/` on macOS.
    app.setApplicationName("PyMillCAM")
    app.setStyleSheet(_INPUT_WIDGET_STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
