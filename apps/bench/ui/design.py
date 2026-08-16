"""BLDC Motor Bench: design system."""

import collections
import math

from PyQt5 import QtCore, QtGui, QtWidgets


# ============================================================== TOKENS
# Shown wherever there is no measurement yet. One name, so the UI and the
# checks in tools/e2e_check.py can never disagree about what "blank" is.
NO_VALUE = "n/a"


class T:
    """Design tokens. Change here, changes everywhere."""

    # surfaces: elevation comes from lightness steps, not from borders
    bg        = "#0a0e14"
    surface   = "#121822"
    surface_2 = "#18212d"
    surface_3 = "#1e2937"
    border    = "#222d3c"
    border_hi = "#2d3c50"

    # text
    text      = "#e8eef7"
    text_dim  = "#93a4bb"
    text_mute = "#63748c"

    # one accent, used sparingly
    accent    = "#4c8dff"
    accent_dk = "#2f6ede"

    # semantic
    ok        = "#22c55e"
    warn      = "#f59e0b"
    bad       = "#ef4444"
    info      = "#38bdf8"

    # channel identity colours (each sensor keeps its hue everywhere)
    c_speed   = "#4c8dff"
    c_current = "#f472b6"
    c_temp    = "#22d3ee"
    c_vib     = "#fbbf24"
    c_ripple  = "#a78bfa"
    c_hall    = "#c084fc"
    c_health  = "#4ade80"

    # geometry
    r_card    = 16
    r_ctl     = 10
    r_pill    = 999
    pad       = 16
    gap       = 12

    @staticmethod
    def font(size=13, weight=QtGui.QFont.Normal, spacing=0.0):
        f = QtGui.QFont("Segoe UI Variable Display", size)
        if not QtGui.QFontInfo(f).exactMatch():
            f = QtGui.QFont("Segoe UI", size)
        f.setWeight(weight)
        if spacing:
            f.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, spacing)
        return f

    @staticmethod
    def num_font(size=44, weight=QtGui.QFont.DemiBold):
        """Numerals: tabular figures so live values don't jitter their width."""
        f = QtGui.QFont("Segoe UI Variable Display", size)
        if not QtGui.QFontInfo(f).exactMatch():
            f = QtGui.QFont("Segoe UI", size)
        f.setWeight(weight)
        f.setStyleStrategy(QtGui.QFont.PreferAntialias)
        try:                       # Qt >= 5.11; keeps digits monospaced
            f.setStyleName("")
            f.setHintingPreference(QtGui.QFont.PreferNoHinting)
        except Exception:
            pass
        return f


def _c(hexstr, alpha=None):
    col = QtGui.QColor(hexstr)
    if alpha is not None:
        col.setAlpha(alpha)
    return col


def shadow(widget, blur=28, dy=6, alpha=120):
    """Soft drop shadow. Qt applies this per-widget, so use it only on cards."""
    eff = QtWidgets.QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(_c("#000000", alpha))
    widget.setGraphicsEffect(eff)
    return eff


# ============================================================== CARD
class Card(QtWidgets.QFrame):
    """Base surface. Painted (not QSS) so the radius stays crisp on hover."""

    def __init__(self, parent=None, radius=None, surface=None, hover=True):
        super().__init__(parent)
        self._radius = T.r_card if radius is None else radius
        self._surface = surface or T.surface
        self._hover_on = hover
        self._hovered = False
        self.setAttribute(QtCore.Qt.WA_Hover, True)
        self.setAutoFillBackground(False)

    def enterEvent(self, e):
        if self._hover_on:
            self._hovered = True
            self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        # subtle top-down sheen so large cards don't read flat
        g = QtGui.QLinearGradient(r.topLeft(), r.bottomLeft())
        base = _c(self._surface)
        g.setColorAt(0.0, base.lighter(108))
        g.setColorAt(1.0, base)
        p.setBrush(QtGui.QBrush(g))
        p.setPen(QtGui.QPen(_c(T.border_hi if self._hovered else T.border), 1))
        p.drawRoundedRect(r, self._radius, self._radius)


# ============================================================== SPARKLINE
class Sparkline(QtWidgets.QWidget):
    """Trailing trend line with a soft gradient fill under it."""

    def __init__(self, color=T.accent, points=90, parent=None):
        super().__init__(parent)
        self._buf = collections.deque(maxlen=points)
        self._color = color
        self.setMinimumHeight(34)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Expanding)

    def push(self, v):
        try:
            self._buf.append(float(v))
        except (TypeError, ValueError):
            return
        self.update()

    def clear(self):
        self._buf.clear()
        self.update()

    def paintEvent(self, _e):
        if len(self._buf) < 2:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        vals = list(self._buf)
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:                     # flat line -> centre it
            lo, hi = lo - 1.0, hi + 1.0
        pad = 3.0
        span = hi - lo
        n = len(vals)

        def pt(i, v):
            x = w * i / (n - 1)
            y = h - pad - (h - 2 * pad) * (v - lo) / span
            return QtCore.QPointF(x, y)

        path = QtGui.QPainterPath(pt(0, vals[0]))
        for i in range(1, n):                  # cubic smoothing between samples
            a, b = pt(i - 1, vals[i - 1]), pt(i, vals[i])
            cx = (a.x() + b.x()) / 2.0
            path.cubicTo(QtCore.QPointF(cx, a.y()), QtCore.QPointF(cx, b.y()), b)

        fill = QtGui.QPainterPath(path)
        fill.lineTo(w, h)
        fill.lineTo(0, h)
        fill.closeSubpath()
        g = QtGui.QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, _c(self._color, 70))
        g.setColorAt(1.0, _c(self._color, 0))
        p.fillPath(fill, QtGui.QBrush(g))

        p.setPen(QtGui.QPen(_c(self._color), 1.8, QtCore.Qt.SolidLine,
                            QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
        p.drawPath(path)

        # live head dot
        head = pt(n - 1, vals[-1])
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(_c(self._color, 60))
        p.drawEllipse(head, 4.5, 4.5)
        p.setBrush(_c(self._color))
        p.drawEllipse(head, 2.2, 2.2)


# ============================================================== GAUGE
class Gauge(QtWidgets.QWidget):
    """Radial arc for the headline channel. 240-degree sweep, gradient track."""

    def __init__(self, maximum=4000, color=T.c_speed, parent=None):
        super().__init__(parent)
        self._max = float(maximum) or 1.0
        self._val = 0.0
        self._shown = 0.0                 # eased value, so the needle glides
        self._color = color
        self._label = ""
        self._sub = ""
        self.setMinimumSize(210, 175)
        # The easing timer runs ONLY while the arc is catching up. An always-on
        # 60 Hz repaint of this widget would burn CPU for nothing while the motor
        # holds a steady speed, and this app has to stay responsive during a
        # 10-minute recording.
        self._anim = QtCore.QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._ease)

    def setValue(self, v):
        try:
            self._val = max(0.0, min(float(v), self._max))
        except (TypeError, ValueError):
            self._val = 0.0
        if self._val != self._shown and not self._anim.isActive():
            self._anim.start()

    def setMaximum(self, m):
        self._max = float(m) or 1.0

    def setLabel(self, text, sub=""):
        self._label, self._sub = text, sub
        self.update()

    def _ease(self):
        d = self._val - self._shown
        if abs(d) < 1.5:                  # 1.5 rpm = 0.09 deg of arc, invisible
            self._anim.stop()             # settled -> stop repainting entirely
            if self._shown != self._val:
                self._shown = self._val
                self.update()
            return
        self._shown += d * 0.55           # big steps sweep in ~130 ms; jitter tracks in one tick
        self.update()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        thick = 13.0
        side = min(w, h * 1.28) - thick - 8
        rect = QtCore.QRectF((w - side) / 2.0, (h * 0.92 - side) / 2.0, side, side)

        start, sweep = 210.0, -240.0     # Qt angles: degrees, CCW positive

        # track
        p.setPen(QtGui.QPen(_c(T.surface_3), thick, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
        p.drawArc(rect, int(start * 16), int(sweep * 16))

        frac = (self._shown / self._max) if self._max else 0.0
        if frac > 0.001:
            g = QtGui.QConicalGradient(rect.center(), start)
            g.setColorAt(0.00, _c(self._color))
            g.setColorAt(0.34, _c(self._color).lighter(125))
            g.setColorAt(0.67, _c(self._color))
            pen = QtGui.QPen(QtGui.QBrush(g), thick, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, int(start * 16), int(sweep * frac * 16))

            # glowing tip
            ang = math.radians(start + sweep * frac)
            cx = rect.center().x() + rect.width() / 2.0 * math.cos(ang)
            cy = rect.center().y() - rect.height() / 2.0 * math.sin(ang)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(_c(self._color, 55))
            p.drawEllipse(QtCore.QPointF(cx, cy), thick * 0.86, thick * 0.86)
            p.setBrush(_c("#ffffff", 210))
            p.drawEllipse(QtCore.QPointF(cx, cy), thick * 0.20, thick * 0.20)

        # centre readout
        p.setPen(_c(T.text))
        f = T.num_font(min(46, int(side * 0.30)), QtGui.QFont.DemiBold)
        f.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, -1.5)
        p.setFont(f)
        vr = QtCore.QRectF(0, rect.center().y() - side * 0.30, w, side * 0.42)
        p.drawText(vr, QtCore.Qt.AlignCenter, self._label or NO_VALUE)

        p.setPen(_c(T.text_mute))
        p.setFont(T.font(10, QtGui.QFont.Bold, 1.4))
        sr = QtCore.QRectF(0, rect.center().y() + side * 0.10, w, 20)
        p.drawText(sr, QtCore.Qt.AlignCenter, self._sub)

        # end labels
        p.setPen(_c(T.text_mute))
        p.setFont(T.font(9, QtGui.QFont.DemiBold))
        p.drawText(QtCore.QRectF(rect.left() - 6, rect.bottom() - 22, 44, 16),
                   QtCore.Qt.AlignCenter, "0")
        p.drawText(QtCore.QRectF(rect.right() - 38, rect.bottom() - 22, 44, 16),
                   QtCore.Qt.AlignCenter, f"{int(self._max):,}")


# ============================================================== PILL
class Pill(QtWidgets.QLabel):
    """Status chip. setText() still works, so existing bindings are unaffected."""

    def __init__(self, text=NO_VALUE, color=None, parent=None):
        super().__init__(text, parent)
        self._color = color or T.surface_3
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setFont(T.font(11, QtGui.QFont.Bold, 0.6))
        self.setMinimumHeight(26)
        self._restyle()

    def setColor(self, color):
        if color != self._color:
            self._color = color
            self._restyle()

    def _restyle(self):
        col = QtGui.QColor(self._color)
        # tinted background + solid text keeps chips quiet next to big numbers
        self.setStyleSheet(
            f"background: rgba({col.red()},{col.green()},{col.blue()},52);"
            f"color: {self._color}; border: 1px solid rgba({col.red()},{col.green()},{col.blue()},110);"
            f"border-radius: 13px; padding: 4px 14px;")


class Dot(QtWidgets.QWidget):
    """Small state indicator with a soft halo."""

    def __init__(self, color=T.text_mute, size=9, parent=None):
        super().__init__(parent)
        self._color = color
        self._size = size
        self.setFixedSize(size * 2, size * 2)

    def setColor(self, color):
        if color != self._color:
            self._color = color
            self.update()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        c = self.rect().center()
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(_c(self._color, 60))
        p.drawEllipse(QtCore.QPointF(c), self._size * 0.85, self._size * 0.85)
        p.setBrush(_c(self._color))
        p.drawEllipse(QtCore.QPointF(c), self._size * 0.40, self._size * 0.40)


# ============================================================== METRIC CARD
class Metric(Card):
    """    Label + big value (+ optional unit, sparkline, trailing note)."""

    def __init__(self, title, color=T.text, big=False, spark=False,
                 unit="", parent=None):
        super().__init__(parent)
        self._color = color
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(2)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(8)
        self.dot = Dot(color, 7)
        self.title = QtWidgets.QLabel(title.upper())
        self.title.setFont(T.font(10, QtGui.QFont.Bold, 1.3))
        self.title.setStyleSheet(f"color:{T.text_mute}; background:transparent;")
        top.addWidget(self.dot)
        top.addWidget(self.title, 1)
        self.badge = QtWidgets.QLabel("")
        self.badge.setFont(T.font(10, QtGui.QFont.DemiBold))
        self.badge.setStyleSheet(f"color:{T.text_mute}; background:transparent;")
        top.addWidget(self.badge, 0, QtCore.Qt.AlignRight)
        v.addLayout(top)
        v.addSpacing(6 if big else 2)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.value = QtWidgets.QLabel(NO_VALUE)
        self.value.setFont(T.num_font(52 if big else 28,
                                      QtGui.QFont.Normal if big else QtGui.QFont.DemiBold))
        self.value.setStyleSheet(f"color:{color}; background:transparent;")
        self.value.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        row.addWidget(self.value, 0, QtCore.Qt.AlignBottom)
        if unit:
            u = QtWidgets.QLabel(unit)
            u.setFont(T.font(12, QtGui.QFont.DemiBold))
            u.setStyleSheet(f"color:{T.text_mute}; background:transparent; padding-bottom:6px;")
            row.addWidget(u, 0, QtCore.Qt.AlignBottom)
        row.addStretch(1)
        v.addLayout(row)

        self.spark = None
        if spark:
            self.spark = Sparkline(color)
            v.addSpacing(6)
            v.addWidget(self.spark, 1)     # trend fills the card, anchored to the bottom
        else:
            v.addStretch(1)
        self.setMinimumHeight(150 if spark else 92)

    def setBadge(self, text, color=None):
        self.badge.setText(text)
        self.badge.setStyleSheet(f"color:{color or T.text_mute}; background:transparent;")


class ChipMetric(Card):
    """Card whose readout is a status pill (health, integrity)."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(8)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(8)
        self.dot = Dot(T.text_mute, 7)
        t = QtWidgets.QLabel(title.upper())
        t.setFont(T.font(10, QtGui.QFont.Bold, 1.3))
        t.setStyleSheet(f"color:{T.text_mute}; background:transparent;")
        top.addWidget(self.dot)
        top.addWidget(t, 1)
        v.addLayout(top)
        row = QtWidgets.QHBoxLayout()
        self.chip = Pill(NO_VALUE)
        row.addWidget(self.chip)
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        self.setMinimumHeight(92)


# ============================================================== SENSOR STRIP
class SensorStrip(Card):
    """One row of per-channel verdicts: the whole rig at a glance."""

    def __init__(self, channels, parent=None):
        super().__init__(parent)
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(18, 12, 18, 12)
        h.setSpacing(0)
        self.items = {}
        for i, (key, label) in enumerate(channels):
            if i:
                sep = QtWidgets.QFrame()
                sep.setFixedWidth(1)
                sep.setStyleSheet(f"background:{T.border};")
                h.addWidget(sep)
            cell = QtWidgets.QWidget()
            cv = QtWidgets.QVBoxLayout(cell)
            cv.setContentsMargins(14, 0, 14, 0)
            cv.setSpacing(5)
            top = QtWidgets.QHBoxLayout()
            top.setSpacing(7)
            dot = Dot(T.text_mute, 7)
            name = QtWidgets.QLabel(label)
            name.setFont(T.font(10, QtGui.QFont.Bold, 1.1))
            name.setStyleSheet(f"color:{T.text_mute}; background:transparent;")
            top.addWidget(dot)
            top.addWidget(name)
            top.addStretch(1)
            state = QtWidgets.QLabel(NO_VALUE)
            state.setFont(T.font(13, QtGui.QFont.DemiBold))
            state.setStyleSheet(f"color:{T.text_dim}; background:transparent;")
            cv.addLayout(top)
            cv.addWidget(state)
            h.addWidget(cell, 1)
            self.items[key] = (dot, state)

    def set(self, key, text, color):
        it = self.items.get(key)
        if not it:
            return
        dot, state = it
        dot.setColor(color)
        if state.text() != text:
            state.setText(text)
        state.setStyleSheet(f"color:{color}; background:transparent;")


# ============================================================== NAV + SHELL
class NavButton(QtWidgets.QAbstractButton):
    """Sidebar item: active state marked by a left bar + tinted surface."""

    def __init__(self, glyph, text, parent=None):
        super().__init__(parent)
        self._glyph = glyph          # icon name in _ICON_PATHS
        self._text = text
        self.setCheckable(True)
        self.setFixedHeight(44)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setAttribute(QtCore.Qt.WA_Hover, True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def sizeHint(self):
        return QtCore.QSize(190, 44)

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        r = QtCore.QRectF(self.rect()).adjusted(8, 2, -8, -2)
        on = self.isChecked()
        hov = self.underMouse()

        if on:
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(_c(T.accent, 34))
            p.drawRoundedRect(r, T.r_ctl, T.r_ctl)
            bar = QtCore.QRectF(r.left() + 1, r.center().y() - 9, 3, 18)
            p.setBrush(_c(T.accent))
            p.drawRoundedRect(bar, 1.5, 1.5)
        elif hov:
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(_c("#ffffff", 12))
            p.drawRoundedRect(r, T.r_ctl, T.r_ctl)

        col = T.text if on else T.text_dim
        pm = icon_pixmap(self._glyph, col, 17)
        p.drawPixmap(QtCore.QPointF(r.left() + 15, r.center().y() - 8.5), pm)
        p.setPen(_c(col))
        p.setFont(T.font(12, QtGui.QFont.DemiBold if on else QtGui.QFont.Normal))
        p.drawText(QtCore.QRectF(r.left() + 44, r.top(), r.width() - 50, r.height()),
                   QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, self._text)


class Shell(QtWidgets.QWidget):
    """    Sidebar + stacked pages, exposing the slice of the QTabWidget API the app"""

    currentChanged = QtCore.pyqtSignal(int)

    def __init__(self, logo_path=None, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background:{T.bg};")
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        rail = QtWidgets.QWidget()
        rail.setFixedWidth(208)
        rail.setStyleSheet(f"background:{T.surface}; border-right:1px solid {T.border};")
        rv = QtWidgets.QVBoxLayout(rail)
        rv.setContentsMargins(0, 20, 0, 14)
        rv.setSpacing(4)

        # brand
        brand = QtWidgets.QHBoxLayout()
        brand.setContentsMargins(20, 0, 16, 0)
        brand.setSpacing(11)
        if logo_path:
            lg = QtWidgets.QLabel()
            lg.setPixmap(QtGui.QPixmap(logo_path).scaledToHeight(
                32, QtCore.Qt.SmoothTransformation))
            lg.setStyleSheet("background:transparent;")
            brand.addWidget(lg)
        tt = QtWidgets.QVBoxLayout()
        tt.setSpacing(0)
        n1 = QtWidgets.QLabel("BLDC Bench")
        n1.setFont(T.font(15, QtGui.QFont.Bold, -0.2))
        n1.setStyleSheet(f"color:{T.text}; background:transparent;")
        n2 = QtWidgets.QLabel("Predictive maintenance")
        n2.setFont(T.font(9))
        n2.setStyleSheet(f"color:{T.text_mute}; background:transparent;")
        tt.addWidget(n1)
        tt.addWidget(n2)
        brand.addLayout(tt, 1)
        rv.addLayout(brand)
        rv.addSpacing(22)

        self._btns = []
        self._navbox = rv
        self.stack = QtWidgets.QStackedWidget()
        self.stack.setStyleSheet(f"background:{T.bg};")

        rv.addStretch(1)

        # live footer: link state + throughput
        foot = QtWidgets.QWidget()
        foot.setStyleSheet("background:transparent;")
        fv = QtWidgets.QVBoxLayout(foot)
        fv.setContentsMargins(20, 10, 16, 0)
        fv.setSpacing(6)
        lrow = QtWidgets.QHBoxLayout()
        lrow.setSpacing(8)
        self.link_dot = Dot(T.bad, 7)
        self.link_txt = QtWidgets.QLabel("Disconnected")
        self.link_txt.setFont(T.font(11, QtGui.QFont.DemiBold))
        self.link_txt.setStyleSheet(f"color:{T.text_dim}; background:transparent;")
        lrow.addWidget(self.link_dot)
        lrow.addWidget(self.link_txt, 1)
        fv.addLayout(lrow)
        self.link_sub = QtWidgets.QLabel(NO_VALUE)
        self.link_sub.setFont(T.font(10))
        self.link_sub.setStyleSheet(f"color:{T.text_mute}; background:transparent;")
        fv.addWidget(self.link_sub)
        rv.addWidget(foot)

        root.addWidget(rail)
        root.addWidget(self.stack, 1)

    # QTabWidget-compatible surface
    def addTab(self, widget, label, glyph="●"):
        idx = self.stack.count()
        self.stack.addWidget(widget)
        b = NavButton(glyph, label)
        b.clicked.connect(lambda _c=False, i=idx: self.setCurrentIndex(i))
        self._navbox.insertWidget(self._navbox.count() - 2, b)
        self._btns.append(b)
        if idx == 0:
            b.setChecked(True)
        return idx

    def setCurrentIndex(self, i):
        if i < 0 or i >= self.stack.count():
            return
        if self.stack.currentIndex() == i:
            for j, b in enumerate(self._btns):
                b.setChecked(j == i)
            return
        self.stack.setCurrentIndex(i)
        for j, b in enumerate(self._btns):
            b.setChecked(j == i)
        self.currentChanged.emit(i)

    def currentIndex(self):
        return self.stack.currentIndex()

    def count(self):
        return self.stack.count()

    def setLink(self, connected, title, sub):
        self.link_dot.setColor(T.ok if connected else T.bad)
        if self.link_txt.text() != title:
            self.link_txt.setText(title)
        if self.link_sub.text() != sub:
            self.link_sub.setText(sub)


# ============================================================== PAGE HEADER
class PageHeader(QtWidgets.QWidget):
    """Title + subtitle + right-aligned slot for live badges/actions."""

    def __init__(self, title, subtitle="", parent=None):
        super().__init__(parent)
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        tt = QtWidgets.QVBoxLayout()
        tt.setSpacing(1)
        self.title = QtWidgets.QLabel(title)
        self.title.setFont(T.font(21, QtGui.QFont.DemiBold, -0.3))
        self.title.setStyleSheet(f"color:{T.text}; background:transparent;")
        self.sub = QtWidgets.QLabel(subtitle)
        self.sub.setFont(T.font(11))
        self.sub.setStyleSheet(f"color:{T.text_mute}; background:transparent;")
        tt.addWidget(self.title)
        tt.addWidget(self.sub)
        h.addLayout(tt, 1)
        self.right = QtWidgets.QHBoxLayout()
        self.right.setSpacing(8)
        h.addLayout(self.right, 0)

    def add(self, w):
        self.right.addWidget(w)
        return w


def section(title):
    """Small uppercase divider label for grouping controls in the rail."""
    l = QtWidgets.QLabel(title.upper())
    l.setFont(T.font(9, QtGui.QFont.Bold, 1.4))
    l.setStyleSheet(f"color:{T.text_mute}; background:transparent; padding:2px 0 0 2px;")
    return l


# ============================================================== ICONS
# A small stroked icon set on a 24x24 grid, drawn with QPainter rather than pulled
# from a font. Emoji were doing this job before; they render at the mercy of the
# system emoji font, come in someone else's colours, and sit differently on every
# machine. These inherit the theme colour, stay crisp at any size, and keep one
# consistent stroke weight across the whole app.
#
# Ops:  pl   polyline (stroked)      plc  closed polyline (stroked)
#       fill filled polygon          circ circle (stroked)
#       circf filled circle          rect rounded rect (stroked)
#       rectf filled rounded rect    arc  (cx, cy, r, start_deg, span_deg)
_ICON_PATHS = {
    # navigation
    "activity": [("pl", [(2, 12), (7, 12), (10, 4), (14, 20), (17, 12), (22, 12)])],
    "link":     [("pl", [(4, 8), (20, 8)]), ("pl", [(16.5, 4.5), (20.5, 8), (16.5, 11.5)]),
                 ("pl", [(20, 16), (4, 16)]), ("pl", [(7.5, 12.5), (3.5, 16), (7.5, 19.5)])],
    "grid":     [("rect", (3, 3, 7.5, 7.5, 1.6)), ("rect", (13.5, 3, 7.5, 7.5, 1.6)),
                 ("rect", (3, 13.5, 7.5, 7.5, 1.6)), ("rect", (13.5, 13.5, 7.5, 7.5, 1.6))],
    "nodes":    [("circ", (5.5, 12, 2.6)), ("circ", (18.5, 6, 2.6)), ("circ", (18.5, 18, 2.6)),
                 ("pl", [(7.9, 10.9), (16.1, 7.1)]), ("pl", [(7.9, 13.1), (16.1, 16.9)])],
    "cpu":      [("rect", (6, 6, 12, 12, 2)), ("rect", (10, 10, 4, 4, 1)),
                 ("pl", [(9, 2), (9, 6)]), ("pl", [(15, 2), (15, 6)]),
                 ("pl", [(9, 18), (9, 22)]), ("pl", [(15, 18), (15, 22)]),
                 ("pl", [(2, 9), (6, 9)]), ("pl", [(2, 15), (6, 15)]),
                 ("pl", [(18, 9), (22, 9)]), ("pl", [(18, 15), (22, 15)])],
    # actions
    "play":     [("fill", [(8, 4.5), (19.5, 12), (8, 19.5)])],
    "stop":     [("rectf", (7, 7, 10, 10, 2))],
    "zap":      [("fill", [(13.5, 2), (4, 13.5), (10.5, 13.5), (10.5, 22), (20, 10.5), (13.5, 10.5)])],
    "target":   [("circ", (12, 12, 9)), ("circ", (12, 12, 4.8)), ("circf", (12, 12, 1.7))],
    "pulse":    [("pl", [(2, 12), (5.5, 12), (7.5, 6), (11, 18), (13, 12), (16, 12)]),
                 ("circ", (19.4, 12, 2.3))],
    "folder":   [("plc", [(3, 7.5), (9.3, 7.5), (11.4, 10), (21, 10), (21, 19.5), (3, 19.5)])],
    "table":    [("rect", (3, 4, 18, 16, 2)), ("pl", [(3, 9.6), (21, 9.6)]),
                 ("pl", [(9, 9.6), (9, 20)]), ("pl", [(15, 9.6), (15, 20)])],
    "check":    [("pl", [(4.5, 12.5), (9.5, 17.5), (19.5, 6.5)])],
    "clock":    [("circ", (12, 12, 9)), ("pl", [(12, 6.4), (12, 12), (16.2, 14.3)])],
    "refresh":  [("arc", (12, 12, 8, 55, 265)), ("fill", [(18.2, 1.8), (21.6, 8.4), (14.6, 7.6)])],
    "layers":   [("plc", [(12, 2.5), (21.5, 7.4), (12, 12.3), (2.5, 7.4)]),
                 ("pl", [(2.5, 12.2), (12, 17.1), (21.5, 12.2)]),
                 ("pl", [(2.5, 16.6), (12, 21.5), (21.5, 16.6)])],
    "edit":     [("plc", [(4, 20), (4, 15.6), (15.6, 4), (20, 8.4), (8.4, 20)])],
    "plus":     [("pl", [(12, 5), (12, 19)]), ("pl", [(5, 12), (19, 12)])],
    "minus":    [("pl", [(5, 12), (19, 12)])],
    "save":     [("pl", [(12, 3.5), (12, 15)]), ("pl", [(7, 10.4), (12, 15.6), (17, 10.4)]),
                 ("pl", [(4, 20), (20, 20)])],
    "warn":     [("plc", [(12, 3), (22, 20.5), (2, 20.5)]),
                 ("pl", [(12, 9.5), (12, 14.5)]), ("circf", (12, 17.8, 1.15))],
}

_icon_cache = {}


def icon_pixmap(name, color=None, size=18, width=1.9):
    """Cached QPixmap of an icon, tinted to `color`. Rendered at 2x for HiDPI."""
    color = color or T.text_dim
    key = (name, color, size, round(width, 2))
    hit = _icon_cache.get(key)
    if hit is not None:
        return hit
    ops = _ICON_PATHS.get(name)
    scale = 2
    pm = QtGui.QPixmap(size * scale, size * scale)
    pm.fill(QtCore.Qt.transparent)
    if ops:
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        k = size * scale / 24.0
        p.scale(k, k)
        pen = QtGui.QPen(_c(color), width, QtCore.Qt.SolidLine,
                         QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
        brush = QtGui.QBrush(_c(color))
        for op, arg in ops:
            if op in ("pl", "plc"):
                path = QtGui.QPainterPath(QtCore.QPointF(*arg[0]))
                for pt in arg[1:]:
                    path.lineTo(QtCore.QPointF(*pt))
                if op == "plc":
                    path.closeSubpath()
                p.setPen(pen); p.setBrush(QtCore.Qt.NoBrush); p.drawPath(path)
            elif op == "fill":
                poly = QtGui.QPolygonF([QtCore.QPointF(*pt) for pt in arg])
                p.setPen(QtCore.Qt.NoPen); p.setBrush(brush); p.drawPolygon(poly)
            elif op == "circ":
                cx, cy, r = arg
                p.setPen(pen); p.setBrush(QtCore.Qt.NoBrush)
                p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
            elif op == "circf":
                cx, cy, r = arg
                p.setPen(QtCore.Qt.NoPen); p.setBrush(brush)
                p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
            elif op in ("rect", "rectf"):
                x, y, w, h, r = arg
                p.setPen(QtCore.Qt.NoPen if op == "rectf" else pen)
                p.setBrush(brush if op == "rectf" else QtCore.Qt.NoBrush)
                p.drawRoundedRect(QtCore.QRectF(x, y, w, h), r, r)
            elif op == "arc":
                cx, cy, r, start, span = arg
                p.setPen(pen); p.setBrush(QtCore.Qt.NoBrush)
                p.drawArc(QtCore.QRectF(cx - r, cy - r, 2 * r, 2 * r),
                          int(start * 16), int(span * 16))
        p.end()
    pm.setDevicePixelRatio(scale)
    _icon_cache[key] = pm
    return pm


def icon(name, color=None, size=18, width=1.9):
    return QtGui.QIcon(icon_pixmap(name, color, size, width))
