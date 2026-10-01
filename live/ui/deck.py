"""A deck panel: what is playing, where we are in it, and how it feels."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..audio import SR, Deck
from ..library import KEY_NAMES, Library
from . import theme as T
from .waveform import OverviewView, WaveformView, WaveImage
from .widgets import Badge, Meter, PhraseDots, caps, label


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class Stat(QWidget):
    """A caps title over a big value and a small note."""

    def __init__(self, title: str, value: QWidget, note: QWidget):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        lay.addWidget(caps(title))
        lay.addWidget(value)
        lay.addWidget(note)


class DeckPanel(QFrame):
    seek = Signal(float)

    def __init__(self, deck_label: str, accent: str, library: Library, compact: bool = False,
                 empty: str = "Nothing playing"):
        super().__init__()
        self.setObjectName("panel")
        self.accent, self.library, self.empty = accent, library, empty
        self.deck: Deck | None = None
        self._shown = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)

        head = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {accent}; font-size: 11px;")
        head.addWidget(dot)
        head.addWidget(caps(deck_label, T.TEXT, 9))
        head.addStretch(1)
        self.status = caps("", accent, 9)
        head.addWidget(self.status)
        lay.addLayout(head)

        self.title = label(empty, 19 if not compact else 16, QFont.Weight.DemiBold)
        self.title.setMinimumWidth(10)
        self.artist = label("", 11.5, color=T.MUTED)
        self.artist.setMinimumWidth(10)
        lay.addWidget(self.title)
        lay.addWidget(self.artist)

        stats = QHBoxLayout()
        stats.setSpacing(26)
        self.bpm = label("—", 21, QFont.Weight.DemiBold, mono=True)
        self.bpm_note = label("", 9, color=T.MUTED)
        stats.addWidget(Stat("Tempo", self.bpm, self.bpm_note))
        key_row = QWidget()
        kr = QHBoxLayout(key_row)
        kr.setContentsMargins(0, 2, 0, 2)
        kr.setSpacing(8)
        self.badge = Badge()
        self.key_name = label("", 12, QFont.Weight.Medium)
        kr.addWidget(self.badge)
        kr.addWidget(self.key_name)
        kr.addStretch(1)
        self.key_note = label("", 9, color=T.MUTED)
        stats.addWidget(Stat("Key", key_row, self.key_note))
        self.time = label("0:00", 21, QFont.Weight.DemiBold, mono=True)
        self.remain = label("", 9, color=T.MUTED, mono=True)
        stats.addWidget(Stat("Time", self.time, self.remain))
        self.bar = label("—", 21, QFont.Weight.DemiBold, mono=True)
        self.dots = PhraseDots(accent)
        stats.addWidget(Stat("Bar · beat", self.bar, self.dots))
        stats.addStretch(1)
        self.section = label("", 10, QFont.Weight.DemiBold)
        stats.addWidget(self.section, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(stats)

        self.wave = WaveformView(accent)
        lay.addWidget(self.wave, 1)
        self.overview = OverviewView(accent)
        self.overview.seek.connect(self.seek)
        lay.addWidget(self.overview)
        self.meters = {}
        if compact:  # deck B under Kev's booth: just the track, its waveform and where it is
            self.wave.setMinimumHeight(96)
            lay.setContentsMargins(20, 12, 20, 12)
            lay.setSpacing(7)
            return

        grid = QGridLayout()
        grid.setHorizontalSpacing(26)
        grid.setVerticalSpacing(4)
        for i, (key, name) in enumerate((("energy", "Energy"), ("mood", "Mood"), ("danceability", "Dance"),
                                         ("busyness", "Busy"), ("brightness", "Bright"), ("vocals", "Vocals"))):
            m = Meter(name, accent)
            self.meters[key] = m
            grid.addWidget(m, i % 3, i // 3)
        lay.addLayout(grid)

    # ------------------------------------------------------------ content

    def set_deck(self, deck: Deck | None, image: WaveImage | None, note: str = ""):
        self.deck = deck
        self._shown = {}
        self.wave.set_deck(deck, image)
        self.overview.set_deck(deck)
        if deck is None:
            self.title.setText(self.empty)
            self.artist.setText("")
            self.bpm.setText("—")
            for w in (self.bpm_note, self.key_name, self.key_note, self.remain, self.section):
                w.setText("")
            self.badge.set("")
            return
        t = deck.track
        self.title.setText(t.title)
        genres = " · ".join(t.genre_list[:3])
        self.artist.setText(f"{t.artist}" + (f"   ·   {genres}" if genres else ""))
        self.bpm.setText(f"{deck.bpm:.1f}")
        stretch = (deck.ratio - 1) * 100
        self.bpm_note.setText(note or (f"native {deck.bpm / deck.ratio:.1f} · {stretch:+.1f}%" if abs(stretch) > 0.05
                                       else "native tempo"))
        self.badge.set(t.camelot)
        self.key_name.setText(KEY_NAMES.get(t.camelot, ""))
        self.key_note.setText(f"confidence {t.key_strength:.2f}")
        fmt = {"energy": f"{t.energy:.1f}", "mood": f"{t.mood:.1f}", "danceability": f"{t.danceability:.2f}",
               "busyness": f"{t.busyness:.1f}/s", "brightness": f"{t.brightness / 1000:.1f}k", "vocals": f"{t.vocals:.0%}"}
        for key, m in self.meters.items():
            m.set(self.library.percentile(t, key), fmt[key])

    def set_status(self, text: str):
        self.status.setText(text.upper())

    def set_transition(self, region=None, marks=(), enters_at=None):
        """Highlight a transition: region and marks in this deck's playing seconds."""
        self.wave.region = region
        self.wave.marks = list(marks)
        self.wave.enters_at = enters_at
        length = self.deck.length / SR if self.deck else 1
        self.overview.region = None if region is None else (region[0] / length, region[1] / length)
        self.wave.update()
        self.overview.update()

    def set_position(self, samples: int):
        if self.deck is None:
            return
        self.wave.set_position(samples)
        self.overview.set_position(max(samples, 0))
        d = self.deck
        secs = samples / SR
        bar, beat, section = d.where(max(samples, 0)) if samples >= 0 else (0, 1, "")
        self._text(self.time, clock(secs) if samples >= 0 else f"-{clock(-secs)}")
        self._text(self.remain, f"−{clock(d.length / SR - secs)} left · {d.length / SR / 60:.1f} min")
        self._text(self.bar, f"{bar}.{beat}" if bar else "—")
        self.dots.set((bar - 1) % 8 + 1 if bar else 0)
        color = T.SECTION.get(section, T.MUTED)
        if self._shown.get("section") != section:
            self._shown["section"] = section
            self.section.setText(section.upper())
            self.section.setStyleSheet(f"color: {color}; background: {T.rgba(color, 0.14)}; border: 1px solid "
                                       f"{T.rgba(color, 0.5)}; border-radius: 7px; padding: 3px 9px;"
                                       if section else "")

    def _text(self, widget: QLabel, text: str):
        if self._shown.get(id(widget)) != text:
            self._shown[id(widget)] = text
            widget.setText(text)
