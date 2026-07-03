from __future__ import annotations

from style import APP_STYLESHEET
from ui import MainWindow, build_application


def main() -> int:
    app = build_application()
    app.setStyleSheet(APP_STYLESHEET)
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
