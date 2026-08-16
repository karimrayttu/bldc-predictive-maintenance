"""Pinout page: editable pin assignment for the NUCLEO-F401RE."""

import os

import yaml
from PyQt5 import QtCore, QtGui, QtWidgets

from bldc_phm import board_f401re as board
from apps.bench.ui import design as D

CAP_LABEL = {
    "adc":  ("ADC", D.T.c_temp),
    "pwm":  ("PWM/TIMER", D.T.c_speed),
    "exti": ("EXTI IN", D.T.c_hall),
    "gpio": ("GPIO", D.T.text_mute),
}


class PinoutPage(QtWidgets.QWidget):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.path = os.path.join(config["app_dir"], "config", "pinout.yaml")
        self.combos = {}
        self.fw_cells = {}
        self._loading = False

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(D.T.gap)

        hdr = D.PageHeader(
            "Pinout",
            "NUCLEO-F401RE (MB1136)  ·  STM32F401RET6  ·  LQFP64.  "
            "Only pins that exist on this board, filtered by what each signal needs")
        self.chip_state = D.Pill(D.NO_VALUE)
        hdr.add(self.chip_state)
        v.addWidget(hdr)

        # the assignment grid
        card = D.Card()
        cv = QtWidgets.QVBoxLayout(card)
        cv.setContentsMargins(18, 16, 18, 16)
        cv.setSpacing(0)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(10)
        for text, w in (("SIGNAL", 190), ("NEEDS", 96), ("ASSIGNED PIN", 0),
                        ("IN FIRMWARE", 150)):
            l = QtWidgets.QLabel(text)
            l.setFont(D.T.font(9, QtGui.QFont.Bold, 1.3))
            l.setStyleSheet(f"color:{D.T.text_mute}; background:transparent;")
            if w:
                l.setFixedWidth(w)
                head.addWidget(l)
            else:
                head.addWidget(l, 1)
        cv.addLayout(head)
        cv.addSpacing(8)

        grid = QtWidgets.QVBoxLayout()
        grid.setSpacing(7)
        for key, label, cap, helptext in board.SIGNALS:
            grid.addLayout(self._row(key, label, cap, helptext))
        cv.addLayout(grid)
        v.addWidget(card)

        # validation output
        self.issues = QtWidgets.QLabel("")
        self.issues.setWordWrap(True)
        self.issues.setFont(D.T.font(11))
        self.issues.setStyleSheet(f"color:{D.T.text_dim}; background:transparent;")
        self.issue_card = D.Card(radius=12, surface=D.T.surface_2, hover=False)
        iv = QtWidgets.QVBoxLayout(self.issue_card)
        iv.setContentsMargins(16, 13, 16, 14)
        iv.addWidget(self.issues)
        v.addWidget(self.issue_card)

        # actions
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.btn_save = QtWidgets.QPushButton("Save pin map")
        self.btn_save.setObjectName("primary")
        self.btn_save.setMinimumHeight(36)
        self.btn_save.clicked.connect(self._save)
        b_rev = QtWidgets.QPushButton("Revert")
        b_rev.setMinimumHeight(36)
        b_rev.clicked.connect(self._load)
        b_fw = QtWidgets.QPushButton("Match firmware")
        b_fw.setMinimumHeight(36)
        b_fw.setToolTip("Reset every dropdown to the pins the current firmware is compiled with.")
        b_fw.clicked.connect(self._match_firmware)
        b_open = QtWidgets.QPushButton("Open pinout.yaml")
        b_open.setMinimumHeight(36)
        b_open.clicked.connect(lambda: os.startfile(self.path))
        for b in (self.btn_save, b_rev, b_fw, b_open):
            b.setCursor(QtCore.Qt.PointingHandCursor)
        row.addWidget(self.btn_save)
        row.addWidget(b_rev)
        row.addWidget(b_fw)
        row.addWidget(b_open)
        row.addStretch(1)
        v.addLayout(row)

        note = QtWidgets.QLabel(
            "Changing a dropdown records the wiring; it does not re-route it. The pins are "
            "compiled into firmware/main.c as direct register writes, so after changing one here "
            "you must change it in the firmware and reflash. Until they agree, the badge above "
            "reads MISMATCH.")
        note.setWordWrap(True)
        note.setFont(D.T.font(10))
        note.setStyleSheet(f"color:{D.T.text_mute}; background:transparent;")
        v.addWidget(note)
        v.addStretch(1)

        self._load()

    # ------------------------------------------------------------------ row
    def _row(self, key, label, cap, helptext):
        h = QtWidgets.QHBoxLayout()
        h.setSpacing(10)

        name = QtWidgets.QLabel(label)
        name.setFont(D.T.font(12, QtGui.QFont.DemiBold))
        name.setStyleSheet(f"color:{D.T.text}; background:transparent;")
        name.setFixedWidth(190)
        name.setToolTip(helptext)
        h.addWidget(name)

        txt, col = CAP_LABEL.get(cap, ("GPIO", D.T.text_mute))
        need = QtWidgets.QLabel(txt)
        need.setFont(D.T.font(9, QtGui.QFont.Bold, 0.8))
        need.setAlignment(QtCore.Qt.AlignCenter)
        c = QtGui.QColor(col)
        need.setStyleSheet(
            f"background: rgba({c.red()},{c.green()},{c.blue()},40); color:{col};"
            f"border:1px solid rgba({c.red()},{c.green()},{c.blue()},95);"
            "border-radius:9px; padding:3px 8px;")
        need.setFixedWidth(96)
        h.addWidget(need)

        combo = QtWidgets.QComboBox()
        combo.setMinimumHeight(32)
        combo.setToolTip(helptext)
        for p in board.pins_for(cap):
            combo.addItem(p.label(), p.port_pin)
            if p.warning():
                combo.setItemData(combo.count() - 1,
                                  f"{p.port_pin}: {p.warning()}", QtCore.Qt.ToolTipRole)
        combo.currentIndexChanged.connect(self._revalidate)
        self.combos[key] = combo
        h.addWidget(combo, 1)

        fw = QtWidgets.QLabel(D.NO_VALUE)
        fw.setFont(D.T.font(11, QtGui.QFont.DemiBold))
        fw.setFixedWidth(150)
        fw.setStyleSheet(f"color:{D.T.text_mute}; background:transparent;")
        self.fw_cells[key] = fw
        h.addWidget(fw)
        return h

    # ----------------------------------------------------------------- data
    def assignment(self):
        return {k: (c.currentData() or "") for k, c in self.combos.items()}

    def _set(self, key, pin_name):
        c = self.combos.get(key)
        if c is None:
            return
        i = c.findData(pin_name)
        if i >= 0:
            c.setCurrentIndex(i)

    def _load(self):
        self._loading = True
        try:
            saved = {}
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    saved = (yaml.safe_load(f) or {}).get("pins", {}) or {}
            for key, _l, _c, _h in board.SIGNALS:
                self._set(key, saved.get(key) or board.FIRMWARE_PINS.get(key, ""))
        finally:
            self._loading = False
        self._revalidate()

    def _match_firmware(self):
        self._loading = True
        try:
            for key, pin_name in board.FIRMWARE_PINS.items():
                self._set(key, pin_name)
        finally:
            self._loading = False
        self._revalidate()

    def _save(self):
        problems = board.validate(self.assignment())
        if any(lv == "error" for lv, _ in problems):
            QtWidgets.QMessageBox.warning(
                self, "Pin map not valid",
                "This map cannot work on the hardware. Fix the errors listed on the page "
                "before saving.")
            return
        a = self.assignment()
        lines = [
            "# ============================================================================",
            "#  PIN ASSIGNMENT: NUCLEO-F401RE (MB1136), STM32F401RET6",
            "#",
            "#  Written by the Pinout page. Every value is validated against the real board:",
            "#  the pin must exist, be capable of the job (ADC channel / timer channel), not",
            "#  be reserved for SWD or the ST-LINK VCP, not be reused by another signal, and",
            "#  the three Hall pins must land on DISTINCT EXTI lines.",
            "#",
            "#  This records the wiring, it does not re-route it. Acquisition pins are compiled",
            "#  into firmware/main.c: change a pin here, then change it there and reflash.",
            "# ============================================================================",
            "",
            "pins:",
        ]
        for key, label, cap, _h in board.SIGNALS:
            p = board.BY_NAME.get(a.get(key, ""))
            if p is None:
                continue
            bits = []
            if p.arduino:
                bits.append(p.arduino)
            if cap == "adc" and p.adc is not None:
                bits.append(f"ADC1_IN{p.adc}")
            if cap == "pwm" and p.timers:
                bits.append(p.timers[0])
            if cap == "exti":
                bits.append(f"EXTI{p.exti_line}")
            comment = f"   # {', '.join([', '.join(bits), label]) if bits else label}"
            lines.append(f"  {key + ':':9} {p.port_pin}{comment}")
        lines.append("")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self._revalidate()
        QtWidgets.QMessageBox.information(
            self, "Pin map saved",
            "Saved to bldc_phm/config/pinout.yaml.\n\n"
            "If any pin now differs from the firmware, update firmware/main.c and reflash; "
            "the badge on this page tracks that.")

    # ----------------------------------------------------------- validation
    def _revalidate(self):
        if self._loading:
            return
        a = self.assignment()
        problems = board.validate(a)
        errors = [m for lv, m in problems if lv == "error"]
        warns = [m for lv, m in problems if lv == "warn"]

        parts = []
        if errors:
            parts.append("<b style='color:%s'>%d problem%s that would not work on the hardware:</b><br>"
                         % (D.T.bad, len(errors), "" if len(errors) == 1 else "s")
                         + "<br>".join("&nbsp;&nbsp;✕ &nbsp;" + e for e in errors))
        if warns:
            parts.append("<b style='color:%s'>Worth knowing:</b><br>" % D.T.warn
                         + "<br>".join("&nbsp;&nbsp;! &nbsp;" + w for w in warns))
        if not parts:
            parts.append("<b style='color:%s'>Valid.</b> Every signal has a capable, "
                         "unshared pin, and the three Hall inputs are on distinct EXTI lines."
                         % D.T.ok)
        self.issues.setText("<br><br>".join(parts))
        self.btn_save.setEnabled(not errors)

        # firmware agreement
        mismatched = 0
        for key, saved, fw, agrees in board.firmware_diff(a):
            cell = self.fw_cells.get(key)
            if cell is None:
                continue
            p = board.BY_NAME.get(fw)
            shown = (f"{p.arduino} · {fw}" if p is not None and p.arduino else (fw or D.NO_VALUE))
            if agrees:
                cell.setText("✓  " + shown)
                cell.setStyleSheet(f"color:{D.T.ok}; background:transparent;")
            else:
                mismatched += 1
                cell.setText("≠  " + shown)
                cell.setStyleSheet(f"color:{D.T.warn}; background:transparent;")

        if errors:
            self.chip_state.setText("INVALID")
            self.chip_state.setColor(D.T.bad)
        elif mismatched:
            self.chip_state.setText(f"MISMATCH · {mismatched}")
            self.chip_state.setColor(D.T.warn)
            self.chip_state.setToolTip(
                "The saved map differs from the pins the firmware is compiled with. "
                "Update firmware/main.c and reflash, or press “Match firmware”.")
        else:
            self.chip_state.setText("MATCHES FIRMWARE")
            self.chip_state.setColor(D.T.ok)
            self.chip_state.setToolTip("Every signal agrees with the compiled firmware.")


def build_pinout_tab(win):
    """Factory used by MainWindow so the page owns its own state."""
    return PinoutPage(win.config)
