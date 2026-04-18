"""PyMillCAM entry point."""
import sys


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
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
