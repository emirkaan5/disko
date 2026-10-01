"""The up-next list: every unplayed track ranked against what is playing."""
from PySide6.QtCore import QAbstractListModel, QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLineEdit, QListView, QStyle, QStyledItemDelegate,
                               QVBoxLayout)

from ..library import MAX_STRETCH, Candidate, Library, Track
from . import theme as T
from .widgets import Segmented, caps, label

SORTS = ("Best match", "Closest tempo", "Best key", "Most energy")


class CandidateModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.items = []

    def set_items(self, items: list):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.UserRole and index.isValid():
            return self.items[index.row()]
        return None


def _structure(p: QPainter, r: QRectF, t: Track):
    """Sections, then bass and vocal lanes, squeezed into a small strip."""
    total = t.sections[-1].end if t.sections else 1
    p.setPen(Qt.PenStyle.NoPen)
    for s in t.sections:
        p.setBrush(T.qc(T.SECTION.get(s.label, T.MUTED), 170))
        p.drawRect(QRectF(r.left() + s.start / total * r.width(), r.top(), (s.end - s.start) / total * r.width() - 0.6, 3))
    for row, (bits, color) in enumerate(((t.bass_bars, T.BASS_LANE), (t.vocal_bars, T.VOCAL_LANE))):
        if not bits:
            continue
        w = r.width() / len(bits)
        p.setBrush(T.qc(color, 210))
        y = r.top() + 5 + row * 5
        i = 0
        while i < len(bits):  # draw runs, not single bars
            if bits[i] == "1":
                j = i
                while j < len(bits) and bits[j] == "1":
                    j += 1
                p.drawRect(QRectF(r.left() + i * w, y, (j - i) * w, 3))
                i = j
            else:
                i += 1


class CandidateDelegate(QStyledItemDelegate):
    H = 76

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.H)

    def paint(self, p: QPainter, option, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        c = item if isinstance(item, Candidate) else None
        t = c.track if c else item
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(2, 3, -10, -3)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        p.setPen(QPen(T.qc(T.FAINT if hover else T.BORDER), 1))
        p.setBrush(T.qc(T.RAISED if hover else T.PANEL_HI))
        p.drawRoundedRect(r, 10, 10)

        right = r.right() - 14
        if c:
            score = c.score
            color = T.GOOD if score >= 0.8 else T.WARN if score >= 0.65 else T.MUTED
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.qc(color))
            p.drawRoundedRect(QRectF(r.left() + 8, r.top() + 12, 3, r.height() - 24), 1.5, 1.5)
            p.setFont(T.font(16, QFont.Weight.DemiBold, mono=True))
            p.setPen(T.qc(color))
            p.drawText(QRectF(right - 40, r.top() + 12, 40, 24), Qt.AlignmentFlag.AlignRight, f"{score * 100:.0f}")
            p.setFont(T.font(7, QFont.Weight.DemiBold, spacing=1))
            p.setPen(T.qc(T.FAINT))
            p.drawText(QRectF(right - 50, r.top() + 40, 50, 12), Qt.AlignmentFlag.AlignRight, "MATCH")
            right -= 62

        # energy change
        if c:
            d = c.energy_delta
            arrow, col = ("▲", T.GOOD) if d > 0.15 else ("▼", T.DECK_A) if d < -0.15 else ("●", T.MUTED)
            p.setFont(T.font(9.5, QFont.Weight.DemiBold, mono=True))
            p.setPen(T.qc(col))
            p.drawText(QRectF(right - 54, r.top() + 14, 54, 16), Qt.AlignmentFlag.AlignRight, f"{arrow} {d:+.1f}")
            p.setFont(T.font(7, QFont.Weight.DemiBold, spacing=1))
            p.setPen(T.qc(T.FAINT))
            p.drawText(QRectF(right - 54, r.top() + 40, 54, 12), Qt.AlignmentFlag.AlignRight, "ENERGY")
            right -= 70

        # key
        kc = T.camelot_color(t.camelot)
        badge = QRectF(right - 40, r.top() + 13, 40, 20)
        fill = T.qc(kc.name(), 50)
        p.setBrush(fill)
        p.setPen(QPen(kc, 1.1))
        p.drawRoundedRect(badge, 6, 6)
        p.setFont(T.font(10, QFont.Weight.Bold, mono=True))
        p.setPen(kc)
        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, t.camelot)
        if c:
            p.setFont(T.font(8.5))
            p.setPen(T.qc(T.GOOD if c.key_score >= 0.85 else T.WARN if c.key_score >= 0.55 else T.BAD))
            p.drawText(QRectF(right - 90, r.top() + 38, 90, 14), Qt.AlignmentFlag.AlignRight, c.key_label)
        right -= 104

        # tempo
        p.setFont(T.font(12, QFont.Weight.DemiBold, mono=True))
        p.setPen(T.qc(T.TEXT))
        p.drawText(QRectF(right - 60, r.top() + 13, 60, 18), Qt.AlignmentFlag.AlignRight, f"{t.bpm:.1f}")
        if c:
            pct = (c.stretch - 1) * 100
            col = T.GOOD if c.deviation <= 0.03 else T.WARN if c.deviation <= MAX_STRETCH else T.BAD
            p.setFont(T.font(8.5, QFont.Weight.Medium, mono=True))
            p.setPen(T.qc(col))
            p.drawText(QRectF(right - 70, r.top() + 38, 70, 14), Qt.AlignmentFlag.AlignRight,
                       f"{pct:+.1f}%" if c.deviation <= MAX_STRETCH else "no sync")
        else:
            p.setFont(T.font(7, QFont.Weight.DemiBold, spacing=1))
            p.setPen(T.qc(T.FAINT))
            p.drawText(QRectF(right - 60, r.top() + 40, 60, 12), Qt.AlignmentFlag.AlignRight, "BPM")
        right -= 78

        # title, artist, structure
        left = r.left() + (22 if c else 14)
        width = right - left
        p.setFont(T.font(12, QFont.Weight.DemiBold))
        p.setPen(T.qc(T.TEXT))
        fm = QFontMetrics(p.font())
        p.drawText(QRectF(left, r.top() + 10, width, 18), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(t.title, Qt.TextElideMode.ElideRight, int(width)))
        p.setFont(T.font(10))
        p.setPen(T.qc(T.MUTED))
        fm = QFontMetrics(p.font())
        sub = t.artist + (f"  ·  {', '.join(t.genre_list[:2])}" if t.genre_list else "")
        p.drawText(QRectF(left, r.top() + 29, width, 16), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(sub, Qt.TextElideMode.ElideRight, int(width)))
        _structure(p, QRectF(left, r.top() + 52, min(width, 220), 14), t)
        p.restore()


class Browser(QFrame):
    picked = Signal(object)  # Track

    def __init__(self, library: Library):
        super().__init__()
        self.setObjectName("panel")
        self.library = library
        self.current = None
        self.master_bpm = 0.0
        self.played = set()
        self.ranked = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 14, 12)
        lay.setSpacing(10)
        head = QHBoxLayout()
        dot = label("●", 9, color=T.DECK_B)
        head.addWidget(dot)
        head.addWidget(caps("Up next", T.TEXT, 9))
        head.addStretch(1)
        self.count = caps("", T.MUTED)
        head.addWidget(self.count)
        lay.addLayout(head)
        self.heading = label("Pick a track to open the set", 17, QFont.Weight.DemiBold)
        lay.addWidget(self.heading)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.energy = Segmented((("▼ Cool down", -1), ("● Hold", 0), ("▲ Lift", 1)), 0)
        self.energy.changed.connect(lambda _: self.refresh())
        controls.addWidget(self.energy)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, artist, genre")
        self.search.textChanged.connect(lambda _: self.apply())
        controls.addWidget(self.search, 1)
        self.sort = QComboBox()
        self.sort.addItems(SORTS)
        self.sort.currentIndexChanged.connect(lambda _: self.apply())
        controls.addWidget(self.sort)
        lay.addLayout(controls)

        self.model = CandidateModel()
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(CandidateDelegate())
        self.view.setMouseTracking(True)
        self.view.setUniformItemSizes(True)
        self.view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.clicked.connect(self._clicked)
        lay.addWidget(self.view, 1)

    def show_start(self):
        self.current = None
        self.heading.setText("Pick a track to open the set (calmest first)")
        self.energy.setVisible(False)
        self.refresh()

    def show_next(self, current: Track, master_bpm: float, played: set):
        self.current, self.master_bpm, self.played = current, master_bpm, set(played)
        self.heading.setText(f"What follows “{current.title}”?")
        self.energy.setVisible(True)
        self.refresh()

    def refresh(self):
        if self.current is None:  # sets usually open low and build
            self.ranked = sorted(self.library.tracks, key=lambda t: t.energy)
        else:
            self.ranked = self.library.rank(self.current, self.master_bpm, self.played, self.energy.value())
        self.apply()

    def apply(self):
        q = self.search.text().strip().lower()
        track = lambda x: x.track if isinstance(x, Candidate) else x
        items = [x for x in self.ranked
                 if not q or q in f"{track(x).title} {track(x).artist} {track(x).genres}".lower()]
        sort = self.sort.currentText()
        if self.current is not None and sort != "Best match":
            key = {"Closest tempo": lambda c: c.deviation, "Best key": lambda c: -c.key_score,
                   "Most energy": lambda c: -c.track.energy}[sort]
            items = sorted(items, key=key)
        elif self.current is None and sort == "Closest tempo":
            items = sorted(items, key=lambda t: t.bpm)
        self.model.set_items(items)
        self.count.setText(f"{len(items)} tracks")
        self.view.scrollToTop()

    def _clicked(self, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        self.picked.emit(item.track if isinstance(item, Candidate) else item)
