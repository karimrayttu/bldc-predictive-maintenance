"""
BLDC PHM Data Acquisition: application entry point.

Run via run_bench.bat, or:  python -m apps.bench.main   (from the BLDC_PHM folder)
"""

import os
import sys
import yaml
from PyQt5 import QtWidgets, QtGui

# Make the repo root importable when launched directly, so 'bldc_phm' and 'apps'
# both resolve. A packaged executable keeps its editable config, firmware, docs
# and data folders beside BLDCMotorBench.exe.
if getattr(sys, "frozen", False):
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    # .../<repo>/apps/bench/main.py -> .../<repo>
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from apps.bench.ui.main_window import MainWindow


def load_config():
    cfg_path = os.path.join(ROOT, "bldc_phm", "config", "app_config.yaml")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
    # resolve data dir relative to the workspace root unless absolute
    data_dir = cfg.get("data_dir", "data")
    if not os.path.isabs(data_dir):
        data_dir = os.path.join(ROOT, data_dir)
    cfg["data_dir"] = data_dir
    # app_dir is the backend package: it owns config/*.yaml.
    # ui_dir is this front end: it owns the window assets.
    cfg["app_dir"] = os.path.join(ROOT, "bldc_phm")
    cfg["ui_dir"] = os.path.join(ROOT, "apps", "bench", "ui")
    cfg["root_dir"] = ROOT
    # Resolve firmware paths relative to the app root so the folder is portable.
    for key, default in (("firmware_dir", "firmware"), ("firmware_elf", "firmware/motor.elf")):
        p = cfg.get(key, default)
        cfg[key] = p if os.path.isabs(p) else os.path.join(ROOT, p)
    os.makedirs(data_dir, exist_ok=True)
    return cfg


# Theme for the STOCK Qt controls (buttons, inputs, tables, scrollbars).
# The instrument cards, gauge, sparklines and sidebar are painted in
# apps/bench/ui/design.py: these hex values mirror the tokens in design.T.
QSS = """
/* ============================================================
   BLDC Motor Bench
   Deep neutral canvas, one accent, elevation by surface not by
   border. Numbers are the hero; chrome stays quiet.
   ============================================================ */
* { font-family: 'Segoe UI Variable Text', 'Segoe UI', Inter, Arial; font-size: 12px; }
QWidget { background: #0a0e14; color: #e8eef7; }
QMainWindow, QDialog { background: #0a0e14; }
QLabel { background: transparent; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: 0; }

/* group boxes */
QGroupBox {
    background: #121822; border: 1px solid #222d3c; border-radius: 14px;
    margin-top: 15px; padding: 15px 13px 13px 13px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 15px; padding: 2px 7px;
    color: #63748c; font-size: 10px; font-weight: 700; letter-spacing: 1.3px;
}

/* buttons: quiet by default, loud only where it matters */
QPushButton {
    background: #18212d; border: 1px solid #2a3648; border-radius: 10px;
    padding: 8px 14px; color: #cddaea; font-weight: 600;
}
QPushButton:hover    { background: #1e2937; border-color: #3a4c66; color: #ffffff; }
QPushButton:pressed  { background: #141c26; }
QPushButton:disabled { background: #10161f; border-color: #1a2330; color: #44536a; }
QPushButton:checked  { background: #4c8dff; border-color: #6ba0ff; color: #ffffff; font-weight: 700; }
QPushButton:focus    { outline: none; }

QPushButton#primary {
    background: #16a34a; border: 1px solid #22c55e; color: #ffffff;
    font-size: 14px; font-weight: 700; border-radius: 12px;
}
QPushButton#primary:hover   { background: #1cb956; }
QPushButton#primary:pressed { background: #128040; }

QPushButton#danger {
    background: rgba(239,68,68,28); border: 1px solid rgba(239,68,68,120);
    color: #ef4444; font-weight: 700; border-radius: 12px;
}
QPushButton#danger:hover   { background: #ef4444; color: #ffffff; border-color: #ef4444; }
QPushButton#danger:pressed { background: #c82f2f; }

QPushButton#accent {
    background: #2f6ede; border: 1px solid #4c8dff; color: #ffffff;
    font-weight: 700; border-radius: 12px;
}
QPushButton#accent:hover { background: #3d7dfa; }

QPushButton#violet {
    background: rgba(167,139,250,30); border: 1px solid rgba(167,139,250,120);
    color: #a78bfa; font-weight: 700; border-radius: 12px;
}
QPushButton#violet:hover { background: #a78bfa; color: #10131a; }

QPushButton#ghostRed { color: #ef4444; border-color: rgba(239,68,68,90); }
QPushButton#ghostRed:hover { background: rgba(239,68,68,35); color: #ef4444; }

/* segmented control (fixture) */
QPushButton#segL { border-top-right-radius: 0; border-bottom-right-radius: 0; border-right: 0; }
QPushButton#segR { border-top-left-radius: 0;  border-bottom-left-radius: 0; }

/* inputs */
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {
    background: #0d131c; border: 1px solid #24303f; border-radius: 9px;
    padding: 6px 10px; color: #e8eef7; selection-background-color: #4c8dff;
}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover { border-color: #31435a; }
QComboBox:focus, QLineEdit:focus, QSpinBox:focus { border-color: #4c8dff; }
QComboBox::drop-down { border: 0; width: 22px; }
QComboBox QAbstractItemView {
    background: #18212d; border: 1px solid #2a3648; border-radius: 10px;
    selection-background-color: #23406e; outline: 0; padding: 4px;
}
QSpinBox::up-button, QSpinBox::down-button { width: 16px; border: 0; background: transparent; }
QCheckBox { spacing: 8px; color: #cddaea; }
QCheckBox::indicator {
    width: 15px; height: 15px; border-radius: 5px;
    border: 1px solid #2a3648; background: #0d131c;
}
QCheckBox::indicator:checked { background: #4c8dff; border-color: #4c8dff; }

/* tables */
QTableWidget {
    background: #0d131c; gridline-color: #1a2431;
    border: 1px solid #222d3c; border-radius: 12px;
    alternate-background-color: #101823;
}
QTableWidget::item { padding: 5px; }
QTableWidget::item:selected { background: #23406e; }
QHeaderView::section {
    background: #121822; color: #63748c; border: 0;
    border-bottom: 1px solid #222d3c; padding: 8px;
    font-size: 10px; font-weight: 700; letter-spacing: 0.8px;
}
QTableCornerButton::section { background: #121822; border: 0; }

/* scrollbars: thin, unobtrusive */
QScrollBar:vertical   { background: transparent; width: 9px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 9px; margin: 2px; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #2a3648; border-radius: 4px; min-height: 34px; min-width: 34px;
}
QScrollBar::handle:hover { background: #3a4c66; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* progress */
QProgressBar { border: 0; border-radius: 3px; background: #0d131c; text-align: center; }
QProgressBar::chunk { background: #4c8dff; border-radius: 3px; }

/* misc */
QStatusBar { background: #080b11; color: #63748c; border-top: 1px solid #161f2b; }
QStatusBar::item { border: 0; }
QToolTip {
    background: #18212d; color: #e8eef7; border: 1px solid #2d3c50;
    border-radius: 8px; padding: 8px;
}
QSplitter::handle { background: #1a2431; }
QMessageBox { background: #121822; }
"""


def _logo_path(cfg):
    base = os.path.join(cfg["ui_dir"], "assets")
    for name in ("app_logo.ico", "app_logo.png"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    return None


def main():
    cfg = load_config()
    # Windows: give the process its own taskbar identity so it uses OUR icon, not python's.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BLDC.Motor.Bench")
        except Exception:
            pass
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    logo = _logo_path(cfg)
    if logo:
        app.setWindowIcon(QtGui.QIcon(logo))
    win = MainWindow(cfg)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
