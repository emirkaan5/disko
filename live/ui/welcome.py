"""The welcome screen: pick a playlist folder, then watch disko get to know it before the set starts."""
import json
import queue
import time
from pathlib import Path

from PySide6.QtCore import QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetrics, QLinearGradient
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..indexer import FastIndexer
from ..playlist import Playlist, camelot, guess_names
from . import theme as T
from .widgets import Painted, caps, clear, label

RECENT = 5
STEPS = (
    ("1", "Index", "Tempo, key, loudness, energy, mood, vocals and style for every track, about half a second "
                   "each. Saved next to your music, so it happens once."),
    ("2", "Open", "You pick the first track. It starts right away."),
    ("3", "Kev DJs", "Kev picks each next track. While the current one plays, allin1 maps the pick's bars, "
                     "sections and stems, then Kev plans the mix into it."),
)


def _left(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"about {seconds} s left" if seconds < 90 else f"about {round(seconds / 60)} min left"


class Progress(Painted):
    """A rounded bar that fills in the accent color."""

    def __init__(self):
        super().__init__(400, 10)
        self.frac = 0.0

    def set(self, frac: float):
        self.frac = max(0.0, min(1.0, frac))
        self.update()

    def paintEvent(self, _):
        p = self.painter()
        r = QRectF(self.rect()).adjusted(0, 1, 0, -1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qc(T.BORDER))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        if self.frac > 0:
            fill = QRectF(r.left(), r.top(), max(r.height(), r.width() * self.frac), r.height())
            g = QLinearGradient(fill.topLeft(), fill.topRight())
            g.setColorAt(0, T.qc(T.DECK_A))
            g.setColorAt(1, T.qc(T.VIOLET))
            p.setBrush(g)
            p.drawRoundedRect(fill, r.height() / 2, r.height() / 2)


class FeedRow(Painted):
    """A track the fast pass just finished: title and artist, tempo, key and energy."""

    def __init__(self, title: str, artist: str, bpm: float, code: str, energy: float):
        super().__init__(500, 40)
        self.title, self.artist, self.bpm, self.code, self.energy = title, artist, bpm, code, energy

    def paintEvent(self, _):
        p = self.painter()
        r = QRectF(self.rect()).adjusted(0.5, 2, -0.5, -2)
        p.setPen(T.pen(T.qc(T.BORDER)))
        p.setBrush(T.qc(T.PANEL_HI))
        p.drawRoundedRect(r, 9, 9)
        right = r.right() - 12
        # energy: a short bar
        bar = QRectF(right - 56, r.center().y() - 2.5, 56, 5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qc(T.BORDER))
        p.drawRoundedRect(bar, 2.5, 2.5)
        p.setBrush(T.qc(T.GOOD))
        p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(5.0, bar.width() * self.energy), bar.height()), 2.5, 2.5)
        p.setFont(T.font(6.5, QFont.Weight.DemiBold, spacing=0.8))
        p.setPen(T.qc(T.FAINT))
        p.drawText(QRectF(bar.left(), r.top() + 4, bar.width(), 9), Qt.AlignmentFlag.AlignLeft, "ENERGY")
        right -= 72
        # key badge
        kc = T.camelot_color(self.code)
        badge = QRectF(right - 38, r.center().y() - 9, 38, 18)
        p.setBrush(T.qc(kc.name(), 50))
        p.setPen(T.pen(kc, 1.1))
        p.drawRoundedRect(badge, 5, 5)
        p.setFont(T.font(9, QFont.Weight.Bold, mono=True))
        p.setPen(kc)
        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, self.code)
        right -= 48
        p.setFont(T.font(10.5, QFont.Weight.DemiBold, mono=True))
        p.setPen(T.qc(T.TEXT))
        p.drawText(QRectF(right - 70, r.top(), 70, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   f"{self.bpm:.1f}")
        right -= 80
        left = r.left() + 12
        width = right - left
        p.setFont(T.font(10.5, QFont.Weight.DemiBold))
        p.setPen(T.qc(T.TEXT))
        fm = QFontMetrics(p.font())
        p.drawText(QRectF(left, r.top() + 3, width, 17), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(self.title, Qt.TextElideMode.ElideRight, int(width)))
        p.setFont(T.font(8.5))
        p.setPen(T.qc(T.MUTED))
        fm = QFontMetrics(p.font())
        p.drawText(QRectF(left, r.top() + 19, width, 14), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(self.artist, Qt.TextElideMode.ElideRight, int(width)))


class RecentCard(QPushButton):
    """A folder opened before: its name, where it is, and how far disko got with it."""

    def __init__(self, folder: str, summary: str):
        super().__init__()
        self.folder = folder
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("recent")
        self.setFixedHeight(58)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 8, 16, 8)
        text = QVBoxLayout()
        text.setSpacing(2)
        name = label(Path(folder).name, 12, QFont.Weight.DemiBold)
        where = label(f"{summary}   ·   {folder.replace(str(Path.home()), '~')}", 9, color=T.MUTED)
        where.setMinimumWidth(10)
        text.addWidget(name)
        text.addWidget(where)
        lay.addLayout(text, 1)
        arrow = label("→", 14, color=T.VIOLET)
        lay.addWidget(arrow)
        for w in (name, where, arrow):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet(f"""
            QPushButton#recent {{ background: {T.PANEL_HI}; border: 1px solid {T.BORDER}; border-radius: 11px;
                                 padding: 0; text-align: left; }}
            QPushButton#recent:hover {{ border-color: {T.VIOLET}; background: {T.RAISED}; }}""")


class WelcomeWindow(QMainWindow):
    ready = Signal(object)  # Playlist, indexed and loaded

    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self.settings = settings
        self.indexer = None
        self.playlist = None
        self._done = self._failed = 0
        self._failures = []
        self._started = 0.0
        self.setAcceptDrops(True)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 14, 14, 14)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)
        self.home = self._home_page()
        self.index = self._index_page()
        self.pages.addWidget(self.home)
        self.pages.addWidget(self.index)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.setWindowTitle("disko live")
        self.setMinimumSize(1100, 760)
        screen = self.screen().availableGeometry()
        self.resize(min(1580, int(screen.width() * 0.96)), min(960, int(screen.height() * 0.94)))
        self.show_home()

    # ------------------------------------------------------------ pages

    def _column(self, page: QWidget, width: int) -> QVBoxLayout:
        """A centered column inside a page."""
        row = QHBoxLayout(page)
        row.addStretch(1)
        col = QWidget()
        col.setFixedWidth(width)
        row.addWidget(col)
        row.addStretch(1)
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        return lay

    def _home_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("panel")
        lay = self._column(page, 660)
        lay.addStretch(3)
        logo = QLabel(f"disko<span style='color:{T.VIOLET}'>.</span>")
        logo.setFont(T.font(46, QFont.Weight.Bold))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(logo)
        tag = label("Kev DJs your music. Point it at a folder of tracks.", 14, color=T.MUTED)
        tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(tag)
        lay.addSpacing(30)

        self.drop = QFrame()
        self.drop.setObjectName("drop")
        self._drop_style(False)
        dl = QVBoxLayout(self.drop)
        dl.setContentsMargins(24, 22, 24, 20)
        dl.setSpacing(10)
        choose = QPushButton("Choose a playlist folder…")
        choose.setObjectName("primary")
        choose.setCursor(Qt.CursorShape.PointingHandCursor)
        choose.setFont(T.font(13, QFont.Weight.DemiBold))
        choose.setFixedHeight(44)
        choose.clicked.connect(self._choose)
        dl.addWidget(choose)
        for text, color in (("or drop a folder anywhere on this window", T.MUTED),
                            ("MP3 · WAV · FLAC · AIFF · Ogg · M4A, subfolders included", T.FAINT)):
            hint = label(text, 9.5, color=color)
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dl.addWidget(hint)
        self.error = label("", 10.5, color=T.BAD)
        self.error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error.setWordWrap(True)
        self.error.hide()
        dl.addWidget(self.error)
        lay.addWidget(self.drop)

        lay.addSpacing(22)
        self.recent_title = caps("Open again")
        lay.addWidget(self.recent_title)
        self.recent = QVBoxLayout()
        self.recent.setSpacing(6)
        lay.addLayout(self.recent)

        lay.addSpacing(26)
        steps = QHBoxLayout()
        steps.setSpacing(12)
        for n, title, text in STEPS:
            card = QFrame()
            card.setObjectName("card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 12, 14, 12)
            cl.setSpacing(4)
            head = QLabel(f"<span style='color:{T.VIOLET}'>{n}</span>&nbsp;&nbsp;{title}")
            head.setFont(T.font(11, QFont.Weight.DemiBold))
            cl.addWidget(head)
            body = label(text, 9.5, color=T.MUTED)
            body.setWordWrap(True)
            cl.addWidget(body)
            cl.addStretch(1)
            steps.addWidget(card, 1)
        lay.addLayout(steps)
        lay.addStretch(4)
        return page

    def _index_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("panel")
        lay = self._column(page, 720)
        lay.addSpacing(36)
        top = QHBoxLayout()
        logo = QLabel(f"disko<span style='color:{T.VIOLET}'>.</span>")
        logo.setFont(T.font(20, QFont.Weight.Bold))
        top.addWidget(logo)
        top.addStretch(1)
        self.cancel = QPushButton("Cancel")
        self.cancel.setObjectName("ghost")
        self.cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel.clicked.connect(self._cancel)
        top.addWidget(self.cancel)
        lay.addLayout(top)
        lay.addSpacing(26)

        self.heading = label("", 22, QFont.Weight.DemiBold)
        lay.addWidget(self.heading)
        self.where = label("", 10, color=T.MUTED)
        lay.addWidget(self.where)
        lay.addSpacing(18)
        self.progress = Progress()
        lay.addWidget(self.progress)
        row = QHBoxLayout()
        self.count = label("", 11, QFont.Weight.DemiBold, mono=True)
        row.addWidget(self.count)
        row.addStretch(1)
        self.eta = label("", 10, color=T.MUTED)
        row.addWidget(self.eta)
        lay.addLayout(row)
        lay.addSpacing(10)
        self.explain = label("Listening to every track once: tempo, key, loudness, energy, mood, vocals and style. "
                             "Bars, sections and stems come later, for each track when it's about to play.",
                             10, color=T.MUTED)
        self.explain.setWordWrap(True)
        lay.addWidget(self.explain)
        self.note = label("", 10, color=T.WARN)
        self.note.setWordWrap(True)
        self.note.hide()
        lay.addWidget(self.note)
        lay.addSpacing(18)
        lay.addWidget(caps("Just analyzed"))
        self.feed = QVBoxLayout()
        self.feed.setSpacing(4)
        lay.addLayout(self.feed)
        lay.addStretch(1)
        self.back = QPushButton("Back")
        self.back.setObjectName("chip")
        self.back.clicked.connect(lambda: self.show_home())
        self.back.hide()
        lay.addWidget(self.back, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addSpacing(24)
        return page

    def _drop_style(self, hot: bool):
        self.drop.setStyleSheet(f"QFrame#drop {{ background: {T.rgba(T.VIOLET, 0.08) if hot else T.PANEL_HI}; "
                                f"border: 1.5px dashed {T.VIOLET if hot else T.BORDER}; border-radius: 14px; }}")

    # ------------------------------------------------------------ recent folders

    def recent_folders(self) -> list:
        if self.settings is None:
            return []
        try:
            folders = json.loads(self.settings.value("recent", "[]"))
        except (TypeError, ValueError):
            return []
        return [f for f in folders if Path(f).is_dir()][:RECENT]

    def _remember(self, folder: str):
        if self.settings is None:
            return
        folders = [folder] + [f for f in self.recent_folders() if f != folder]
        self.settings.setValue("recent", json.dumps(folders[:RECENT]))

    def show_home(self, recent: list | None = None):
        """recent: [(folder, summary)] to show instead of the saved list (for snapshots)."""
        self.timer.stop()
        self.pages.setCurrentWidget(self.home)
        clear(self.recent)
        if recent is None:
            recent = [(f, Playlist(f).summary()) for f in self.recent_folders()]
        for folder, summary in recent:
            card = RecentCard(folder, summary)
            card.clicked.connect(lambda _=False, f=folder: self.open(f))
            self.recent.addWidget(card)
        self.recent_title.setVisible(bool(recent))

    def _show_error(self, text: str):
        self.show_home()
        self.error.setText(text)
        self.error.show()

    # ------------------------------------------------------------ opening a folder

    def _choose(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose a playlist folder", str(Path.home() / "Music"))
        if folder:
            self.open(folder)

    def dragEnterEvent(self, e):
        urls = e.mimeData().urls()
        if self.pages.currentWidget() is self.home and urls and Path(urls[0].toLocalFile()).is_dir():
            self._drop_style(True)
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._drop_style(False)

    def dropEvent(self, e):
        self._drop_style(False)
        self.open(e.mimeData().urls()[0].toLocalFile())

    def open(self, folder: str):
        self.error.hide()
        pl = Playlist(folder)
        if not pl.files:
            self._show_error(f"No audio files in “{pl.name}”. disko reads MP3, WAV, FLAC, AIFF, Ogg and M4A.")
            return
        self._remember(str(pl.folder))
        self.playlist = pl
        self.indexer = FastIndexer(pl)
        self._done = len(pl.files) - len(self.indexer.todo)
        self._failed, self._failures = 0, []
        clear(self.feed)
        self.note.hide()
        self.back.hide()
        self.cancel.show()
        self.heading.setText(f"Getting “{pl.name}” ready")
        self.where.setText(QFontMetrics(self.where.font()).elidedText(
            str(pl.folder).replace(str(Path.home()), "~"), Qt.TextElideMode.ElideMiddle, 720))
        self.explain.show()
        self._started = time.time()
        self._first = len(self.indexer.todo)
        self._update_count()
        self.pages.setCurrentWidget(self.index)
        self.indexer.start()
        self.timer.start(80)

    def _cancel(self):
        if self.indexer is not None:
            self.indexer.cancel()  # what's done is kept: next time only the rest is analyzed
        self.show_home()

    def _update_count(self):
        total = len(self.playlist.files)
        seen = self._done + self._failed
        self.progress.set(seen / total if total else 1.0)
        self.count.setText(f"{seen} of {total}")
        new = seen - (total - self._first)
        if self._first and 3 <= new < self._first:
            per = (time.time() - self._started) / new
            self.eta.setText(_left(per * (self._first - new)))
        elif not self._first:
            self.eta.setText("all analyzed before")
        else:
            self.eta.setText("starting the analysis…" if new < 3 else "")

    def add_feed(self, title: str, artist: str, bpm: float, key: str, arousal: float):
        """Show a track that just finished (newest on top, a handful kept)."""
        tonic, scale = key.split(" ")
        row = FeedRow(title, artist or "Unknown artist", bpm, camelot(tonic, scale), max(0.0, min(1.0, (arousal - 4) / 3)))
        self.feed.insertWidget(0, row)
        while self.feed.count() > 7:
            item = self.feed.takeAt(self.feed.count() - 1)
            item.widget().hide()
            item.widget().deleteLater()

    def _poll(self):
        while self.indexer is not None:
            try:
                kind, msg = self.indexer.events.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                self._done += 1
                title, artist = msg.get("title"), msg.get("artist")
                if not title:  # untagged: named the way the library will name it
                    title, guessed = guess_names(msg["rel"])
                    artist = artist or guessed
                self.add_feed(title, artist, msg["bpm"], msg["key"], msg["arousal"])
            elif kind == "failed":
                self._failed += 1
                self._failures.append(Path(msg["rel"]).name)
            elif kind == "fatal":
                self.timer.stop()
                self.heading.setText("The analysis couldn't run")
                self.note.setText(msg["error"])
                self.note.show()
                self.cancel.hide()
                self.back.show()
                return
            elif kind == "finished":
                self.timer.stop()
                self._finish()
                return
            self._update_count()

    def _finish(self):
        pl = self.playlist
        tracks = pl.load()
        self._update_count()
        self.progress.set(1.0)
        if len(tracks) < 2:
            self.heading.setText("Not enough tracks to mix")
            self.note.setText("disko needs at least two tracks it can read." +
                              (f" Couldn't read: {', '.join(self._failures)}" if self._failures else ""))
            self.note.show()
            self.cancel.hide()
            self.back.show()
            return
        self.heading.setText(f"“{pl.name}” is ready: {len(tracks)} tracks")
        self.eta.setText("")
        self.explain.setText("Starting the set…")
        self.cancel.hide()
        wait = 900
        if self._failures:
            shown = ", ".join(self._failures[:4]) + (f" and {len(self._failures) - 4} more" if len(self._failures) > 4 else "")
            self.note.setText(f"{len(self._failures)} file{'s' if len(self._failures) > 1 else ''} couldn't be read "
                              f"and {'are' if len(self._failures) > 1 else 'is'} left out: {shown}")
            self.note.show()
            wait = 2600
        QTimer.singleShot(wait if self._first else 0, lambda: self.ready.emit(pl))

    def closeEvent(self, e):
        if self.indexer is not None:
            self.indexer.cancel()
        super().closeEvent(e)
