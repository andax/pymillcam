"""PyMillCAM entry point."""
import sys

def main() -> None:
    """Launch PyMillCAM application."""
    from PySide6.QtWidgets import QApplication
    from pymillcam.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("PyMillCAM")
    app.setOrganizationName("PyMillCAM")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
