"""Colors, fonts and the stylesheet: a dark deck with one accent per deck."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontDatabase, QPen

BG = "#090b10"
PANEL = "#10131a"
PANEL_HI = "#151923"
RAISED = "#1c212d"
BORDER = "#232937"
TEXT = "#e9ecf3"
MUTED = "#8b93a7"
FAINT = "#535c70"
DECK_A = "#3cc6f5"
DECK_B = "#ff8f4a"
GOOD = "#38d996"
WARN = "#ffc24d"
BAD = "#ff6b81"
VIOLET = "#a68cff"
WAVE_LOW, WAVE_MID, WAVE_HIGH = "#2f6bff", "#ffad33", "#f4f7ff"
BASS_LANE, VOCAL_LANE = "#4f7dff", "#ff5fa2"

SECTION = {"intro": "#64748b", "verse": "#818cf8", "chorus": "#f43f5e", "bridge": "#eab308",
           "inst": "#22d3ee", "solo": "#c084fc", "break": "#34d399", "outro": "#64748b"}
TONE = {"good": GOOD, "warn": WARN, "bad": BAD, "info": VIOLET}


def qc(hex_: str, alpha: int = 255) -> QColor:
    c = QColor(hex_)
    c.setAlpha(alpha)
    return c


def rgba(hex_: str, alpha: float) -> str:
    c = QColor(hex_)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def pen(color: QColor, width: float = 1.0, style=Qt.PenStyle.SolidLine) -> QPen:
    """A round-capped pen (PySide6's QPen takes no keyword arguments)."""
    return QPen(QBrush(color), width, style, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def camelot_color(code: str) -> QColor:
    """The Camelot wheel's rainbow: one hue per hour, majors (B) a touch lighter."""
    n, letter = int(code[:-1]), code[-1]
    hue = int((n - 1) * 30 + 140) % 360
    return QColor.fromHsv(hue, 170 if letter == "A" else 120, 235 if letter == "A" else 250)


def font(size: float, weight=QFont.Weight.Normal, mono: bool = False, spacing: float = 0.0) -> QFont:
    f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont) if mono else QFont()
    f.setPointSizeF(size)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    if mono:
        f.setStyleHint(QFont.StyleHint.Monospace)
    return f


STYLESHEET = f"""
* {{ color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QFrame#panel {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#card {{ background: {PANEL_HI}; border: 1px solid {BORDER}; border-radius: 11px; }}
QLabel {{ background: transparent; }}
QLineEdit {{ background: {PANEL_HI}; border: 1px solid {BORDER}; border-radius: 9px; padding: 7px 10px;
            selection-background-color: {VIOLET}; }}
QLineEdit:focus {{ border-color: {rgba(VIOLET, 0.8)}; }}
QComboBox {{ background: {PANEL_HI}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 10px; }}
QComboBox:hover {{ border-color: {FAINT}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {RAISED}; border: 1px solid {BORDER}; selection-background-color: {rgba(VIOLET, 0.3)};
                               outline: none; padding: 4px; }}
QPushButton {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 9px; padding: 8px 14px; font-weight: 600; }}
QPushButton:hover {{ border-color: {FAINT}; }}
QPushButton:disabled {{ color: {FAINT}; }}
QPushButton#primary {{ background: {DECK_B}; color: #170b02; border: none; padding: 9px 18px; }}
QPushButton#primary:hover {{ background: #ffa266; }}
QPushButton#primary:disabled {{ background: {RAISED}; color: {FAINT}; }}
QPushButton#ghost {{ background: transparent; }}
QPushButton#chip {{ background: {PANEL_HI}; padding: 4px 10px; border-radius: 8px; font-weight: 500; }}
QPushButton#chip:checked {{ background: {rgba(VIOLET, 0.18)}; border-color: {VIOLET}; }}
QPushButton#chip:disabled {{ color: {FAINT}; background: transparent; }}
QPushButton#round {{ border-radius: 19px; padding: 0; font-size: 15px; }}
QListView {{ background: transparent; border: none; outline: none; }}
QScrollArea {{ background: transparent; border: none; }}
QWidget#qt_scrollarea_viewport, QWidget#body {{ background: transparent; }}
QScrollBar:vertical {{ width: 8px; background: transparent; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ height: 6px; background: transparent; margin: 1px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {MUTED}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}
"""
