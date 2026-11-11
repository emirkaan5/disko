"""The welcome screen: pick a playlist folder or a Spotify playlist, then watch disko get to know it
before the set starts. Spotify audio is the listener's own decision and is asked for here, in the
app, before any of it is read (SPOTIFY_INTEGRATION_PLAN.md, Milestones 2 and 6)."""
import json
import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetrics, QLinearGradient
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..indexer import FastIndexer
from ..playlist import Playlist, camelot, guess_names
from ..sources.spotify.consent import WARNING as SPOTIFY_WARNING
from . import theme as T
from .widgets import Painted, caps, clear, label

RECENT = 5
# The listener's own acknowledgement, in the words the terminal uses (live/sources/spotify/consent.py):
# the same text wherever the choice is made, so it can't be softened in one place and not the other.
WARNING_LINES = tuple(part.strip() for part in SPOTIFY_WARNING.strip().split("\n\n"))
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


def _short(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


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


class SpotifyCard(QPushButton):
    """One of the account's playlists on the Spotify page: its name and how many tracks it holds."""

    def __init__(self, name: str, count: int | None):
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("recent")
        self.setFixedHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 16, 6)
        text = QVBoxLayout()
        text.setSpacing(2)
        title = label(name, 12, QFont.Weight.DemiBold)
        where = label("Spotify doesn't say how many" if count is None else f"{count} tracks", 9, color=T.MUTED)
        text.addWidget(title)
        text.addWidget(where)
        lay.addLayout(text, 1)
        arrow = label("→", 14, color=T.VIOLET)
        lay.addWidget(arrow)
        for w in (title, where, arrow):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet(f"""
            QPushButton#recent {{ background: {T.PANEL_HI}; border: 1px solid {T.BORDER}; border-radius: 11px;
                                 padding: 0; text-align: left; }}
            QPushButton#recent:hover {{ border-color: {T.VIOLET}; background: {T.RAISED}; }}""")


class PoolBuild:
    """Builds a Spotify pool on its own thread and reports it the way an indexer does.

    A pool over a Spotify playlist is network work: reading the playlist, fetching a 30 s clip per
    track (live/sources/spotify/preview.py), then analysing the clips. It runs off the window thread
    and reports into a queue of the same events the folder indexer sends - `start`, `done`, `failed`,
    `fatal`, `finished` - so the indexing page shows it the same way. `result` is what the set needs
    once it is done: (Library, DeepAnalyzer)."""

    def __init__(self, playlist: str, name: str, limit: int = 200):
        self.playlist, self.name, self.limit = playlist, name, limit
        self.events = queue.Queue()
        self.result = None
        self.pool = None
        self.error = None
        self.tracks = self.todo = 0
        self._cancelled = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="spotify-pool")

    def start(self):
        self._thread.start()

    def cancel(self):
        """Stop reporting and keep what came in: the pool is dropped when it finishes."""
        self._cancelled = True

    @property
    def total(self) -> int:
        """The clips that have to be listened to, known once the playlist has been listed."""
        return self.todo

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _run(self):
        from ..indexer import DeepAnalyzer
        from ..library import Library
        from ..runtime import session_temp
        from ..sources.pool import preview_pool
        from ..sources.spotify import ConsentStore, connect, load_policy
        from ..sources.spotify.fetch import Fetcher
        from ..sources.spotify.preview import PreviewFetcher
        from ..sources.store import SourceStore
        try:
            settings = ConsentStore()
            temp = session_temp()
            source = connect(policy=load_policy(settings=settings), settings=settings,
                             fetcher=Fetcher(temp.folder("fetch")))
            store = SourceStore.for_source(source, temp)
            previews = PreviewFetcher(store.dir / "previews")
            self.pool = preview_pool(source, self.playlist, previews, store, limit=self.limit,
                                     progress=self._progress)
            if self._cancelled or not self.pool.tracks:
                return
            analyzer = DeepAnalyzer(store, temp)
            analyzer.start()  # its imports take a few seconds: done before the first pick
            self.result = (Library(self.pool.tracks, name=self.name), analyzer)
        except Exception as e:  # a source error, or the analysis environment missing
            if not self._cancelled:
                self.error = str(e) or e.__class__.__name__
        finally:
            self.events.put(("finished", {}))

    def _progress(self, kind: str, info: dict):
        if self._cancelled:
            return
        if kind == "start":
            self.tracks, self.todo = info["tracks"], info["todo"]
        self.events.put((kind, info))


class WelcomeWindow(QMainWindow):
    ready = Signal(object)       # a Playlist from a folder, indexed and loaded
    ready_set = Signal(object)   # (Library, DeepAnalyzer) for a playlist the window pooled itself

    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self.settings = settings
        self.indexer = None
        self.playlist = None
        self.job = None                 # a PoolBuild, while a Spotify playlist is being pooled
        self.spotify = None             # the status the Spotify page was last drawn from
        self.spot = queue.Queue()       # (kind, payload) from the playlist-listing thread
        self._done = self._failed = self._first = 0   # progress: how much of the work is reported
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
        self.spot_page = self._spotify_page()
        self.pages.addWidget(self.home)
        self.pages.addWidget(self.index)
        self.pages.addWidget(self.spot_page)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.spot_timer = QTimer(self)
        self.spot_timer.timeout.connect(self._spot_poll)
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

        lay.addSpacing(18)
        spot = QFrame()
        spot.setObjectName("card")
        sl = QVBoxLayout(spot)
        sl.setContentsMargins(16, 12, 16, 14)
        sl.setSpacing(6)
        sl.addWidget(label("A Spotify playlist", 11, QFont.Weight.DemiBold))
        spot_help = label("Sign in, then pick one of your playlists. disko lists it and reads a 30 s clip of each "
                          "track; the whole track is fetched only when it is about to play, and only if you turn "
                          "Spotify audio on here.", 9.5, color=T.MUTED)
        spot_help.setWordWrap(True)
        sl.addWidget(spot_help)
        use = QPushButton("Use a Spotify playlist…")
        use.setCursor(Qt.CursorShape.PointingHandCursor)
        use.setFixedHeight(36)
        use.clicked.connect(lambda _=False: self.show_spotify())
        sl.addWidget(use, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(spot)

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

    def _spotify_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("panel")
        lay = self._column(page, 660)
        lay.addSpacing(36)
        top = QHBoxLayout()
        logo = QLabel(f"disko<span style='color:{T.VIOLET}'>.</span>")
        logo.setFont(T.font(20, QFont.Weight.Bold))
        top.addWidget(logo)
        top.addStretch(1)
        back = QPushButton("Back")
        back.setObjectName("chip")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(lambda _=False: self.show_home())
        top.addWidget(back)
        lay.addLayout(top)

        lay.addSpacing(26)
        lay.addWidget(label("Spotify", 22, QFont.Weight.DemiBold))
        self.spot_state = label("", 10, color=T.MUTED)
        self.spot_state.setWordWrap(True)
        lay.addWidget(self.spot_state)
        lay.addSpacing(16)

        self.spot_warning = QFrame()
        self.spot_warning.setObjectName("card")
        wl = QVBoxLayout(self.spot_warning)
        wl.setContentsMargins(16, 14, 16, 14)
        wl.setSpacing(8)
        wl.addWidget(label("Before disko reads any Spotify audio", 11, QFont.Weight.DemiBold))
        for line in WARNING_LINES:
            part = label(line, 9.5, color=T.MUTED)
            part.setWordWrap(True)
            wl.addWidget(part)
        row = QHBoxLayout()
        self.spot_on = QPushButton("Turn on Spotify audio")
        self.spot_on.setObjectName("primary")
        self.spot_on.setCursor(Qt.CursorShape.PointingHandCursor)
        self.spot_on.setFixedHeight(38)
        self.spot_on.clicked.connect(lambda _=False: self._spot_audio(True))
        row.addWidget(self.spot_on)
        only = QPushButton("Browse only")
        only.setCursor(Qt.CursorShape.PointingHandCursor)
        only.setFixedHeight(38)
        only.clicked.connect(lambda _=False: self._spot_audio(False))
        row.addWidget(only)
        row.addStretch(1)
        wl.addLayout(row)
        lay.addWidget(self.spot_warning)

        self.spot_signin = QPushButton("Sign in with Spotify")
        self.spot_signin.setObjectName("primary")
        self.spot_signin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.spot_signin.setFixedHeight(42)
        self.spot_signin.clicked.connect(lambda _=False: self._spot_sign_in())
        lay.addWidget(self.spot_signin)

        self.spot_off = QPushButton("Turn Spotify audio off again")
        self.spot_off.setObjectName("chip")
        self.spot_off.setCursor(Qt.CursorShape.PointingHandCursor)
        self.spot_off.clicked.connect(lambda _=False: self._spot_audio(False))
        lay.addWidget(self.spot_off, 0, Qt.AlignmentFlag.AlignLeft)

        self.spot_note = label("", 10, color=T.WARN)
        self.spot_note.setWordWrap(True)
        self.spot_note.hide()
        lay.addWidget(self.spot_note)

        lay.addSpacing(16)
        self.spot_list_title = caps("Your playlists")
        lay.addWidget(self.spot_list_title)
        self.spot_list = QVBoxLayout()
        self.spot_list.setSpacing(6)
        lay.addLayout(self.spot_list)
        lay.addStretch(1)
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
        self.spot_timer.stop()
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

    # ------------------------------------------------------------ spotify

    def show_spotify(self, playlists=None, state=None):
        """The Spotify page: the listener's audio choice, the sign-in, and their playlists.

        `playlists` ([(id, name, tracks)]) and `state` (a GateStatus) are drawn as given instead of
        asking Spotify and the Keychain, for snapshots."""
        if playlists is not None and not isinstance(playlists, list):
            playlists = None            # clicked passes a bool
        self.timer.stop()
        self.spot_timer.stop()
        self.pages.setCurrentWidget(self.spot_page)
        self._spot_refresh(playlists, state)

    def _spot_refresh(self, playlists=None, state=None):
        """Draw the page from the stored choice and the sign-in state, then the account's playlists."""
        from ..sources.spotify import audio_choice, status
        try:
            state, choice = state or status(), audio_choice()
        except Exception as e:          # a Keychain that can't be read, a broken client ID
            self.spot_state.setText(f"Spotify's settings couldn't be read: {e}")
            for w in (self.spot_signin, self.spot_warning, self.spot_off, self.spot_list_title):
                w.hide()
            return
        self.spotify = state
        if not state.authorized:
            self.spot_state.setText(state.reason)
            for w in (self.spot_signin, self.spot_warning, self.spot_off, self.spot_list_title):
                w.hide()
            return
        if not state.configured:
            self.spot_state.setText("Spotify needs your own developer app: a client ID from "
                                    "developer.spotify.com, then `python -m live.sources.spotify set-client`. "
                                    "A Premium account is needed to play tracks.")
        elif not state.signed_in:
            self.spot_state.setText("Not signed in. disko browses your account only after you sign in.")
        elif state.audio:
            self.spot_state.setText(f"Signed in. {choice.summary if choice else 'Spotify audio is on'}. "
                                    "A track is fetched only as it comes up, one at a time, and nothing is "
                                    "recorded or kept.")
        else:
            self.spot_state.setText("Signed in. Spotify audio is off, so your playlists can be listed but their "
                                    "tracks can't be played yet.")
        self.spot_signin.setVisible(state.configured and not state.signed_in)   # nothing to sign in with yet
        self.spot_signin.setEnabled(True)
        self.spot_warning.setVisible(state.signed_in and not state.audio)
        self.spot_off.setVisible(state.audio)
        self.spot_list_title.setVisible(state.signed_in)
        if not state.signed_in:
            clear(self.spot_list)
            return
        if playlists is not None:
            self._spot_playlists(playlists)
            return
        self._spot_note("Reading your playlists…", warn=False)
        threading.Thread(target=self._list_playlists, daemon=True, name="spotify-playlists").start()
        self.spot_timer.start(300)

    def _list_playlists(self):
        """Ask Spotify for the account's playlists, on this thread (the window thread draws them)."""
        from ..sources.spotify import connect, load_policy
        try:
            source = connect(policy=load_policy())
            self.spot.put(("playlists", [(ref.id, ref.name, ref.track_count) for ref in source.playlists()]))
        except Exception as e:
            self.spot.put(("list_error", str(e) or e.__class__.__name__))

    def _spot_playlists(self, playlists):
        clear(self.spot_list)
        for playlist_id, name, count in playlists:
            card = SpotifyCard(name, count)
            card.clicked.connect(lambda _=False, i=playlist_id, n=name: self._spot_pick(i, n))
            self.spot_list.addWidget(card)
        self._spot_note("" if playlists else "This account has no playlists to show.", warn=False)

    def _spot_audio(self, on: bool):
        """Record the listener's own choice, here in the app, dated today (Milestone 2)."""
        from ..sources.spotify import set_audio
        set_audio(audio=on)
        self._spot_refresh()

    def _spot_sign_in(self):
        from ..sources.spotify import login
        self.spot_signin.setEnabled(False)
        self._spot_note("Finish the sign-in in your browser; this page waits for it.", warn=False)
        def work():
            try:
                login()
                self.spot.put(("signed_in", None))
            except Exception as e:
                self.spot.put(("sign_error", str(e) or e.__class__.__name__))
        threading.Thread(target=work, daemon=True, name="spotify-login").start()
        self.spot_timer.start(300)

    def _spot_poll(self):
        while True:
            try:
                kind, payload = self.spot.get_nowait()
            except queue.Empty:
                return
            if kind == "playlists":
                self.spot_timer.stop()
                self._spot_playlists(payload)
            elif kind == "signed_in":       # now the choice is asked for, before any audio is read
                self.spot_timer.stop()
                self._spot_refresh()
            elif kind == "sign_error":
                self.spot_timer.stop()
                self.spot_signin.setEnabled(True)
                self._spot_note(f"The sign-in didn't finish: {payload}")
            else:                           # list_error
                self.spot_timer.stop()
                self._spot_note(payload)

    def _spot_note(self, text: str, warn: bool = True):
        self.spot_note.setText(text)
        self.spot_note.setStyleSheet(f"color: {T.WARN if warn else T.MUTED};")
        self.spot_note.setVisible(bool(text))

    def _spot_pick(self, playlist_id: str, name: str):
        """Pool the playlist (Milestone 4) and hand the set what comes out of it."""
        from ..sources import Operation
        from ..sources.spotify import ConsentStore, load_policy
        if not load_policy(settings=ConsentStore()).allows(Operation.TEMP_AUDIO):
            self._spot_note("Spotify audio is off, so a track from this playlist couldn't be played. "
                            "Turning it on is your own choice; it is against Spotify's terms.")
            return
        if self.indexer is not None:
            self.indexer.cancel()
            self.indexer = None          # the page polls whichever of the two is set, so only one may be
        self.playlist = None
        self.job = PoolBuild(playlist_id, name)
        self._done = self._failed = 0
        self._failures, self._first = [], 0
        clear(self.feed)
        self.note.hide()
        self.back.hide()
        self.cancel.show()
        self.heading.setText(f"Getting “{name}” ready")
        self.where.setText("reading a 30 s clip of each track, then listening to every clip once")
        self.explain.setText("Tempo, key, loudness, energy, mood, vocals and style, measured from a 30 s clip of "
                             "each track. The whole track is fetched and mapped later, one at a time, when it is "
                             "about to play.")
        self._started = time.time()
        self._update_count()
        self.pages.setCurrentWidget(self.index)
        self.job.start()
        self.timer.start(80)

    def _spot_finish(self):
        job = self.job
        self._update_count()
        self.progress.set(1.0)
        self.cancel.hide()
        self.back.show()
        self.job = None
        if job.error:
            self.heading.setText("The playlist couldn't be read")
            self.note.setText(job.error)
            self.note.show()
            return
        if job.result is None:
            self.heading.setText("No tracks to mix")
            self.note.setText("Nothing in this playlist could be analysed, so there is no set to run."
                              if not job.cancelled else "Stopped.")
            self.note.show()
            return
        library, analyzer = job.result
        self.heading.setText(f"“{job.name}” is ready: {len(library.tracks)} tracks")
        self.eta.setText("")
        self.explain.setText("Starting the set…")
        left = [f"{info.artist} – {_short(info.title, 40)} ({why})" for info, why in job.pool.skipped[:4]]
        if left:
            more = f" and {len(job.pool.skipped) - 4} more" if len(job.pool.skipped) > 4 else ""
            self.note.setText(f"{job.pool.summary()}; left out: " + "; ".join(left) + more)
            self.note.show()
        QTimer.singleShot(2400 if left else 900, lambda: self.ready_set.emit(job.result))

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
        self.job = None                  # a Spotify pool that was running is not this folder's progress
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
        if self.job is not None:
            self.job.cancel()      # the clips already analyzed stay on disk, as for a folder
        self.show_home()

    def _update_count(self):
        total = self.job.total if self.job is not None else len(self.playlist.files)
        seen = self._done + self._failed
        self.progress.set(seen / total if total else (0.0 if self.job is not None else 1.0))
        self.count.setText(f"{seen} of {total}" if total else "reading the playlist…")
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
        watched = self.job if self.job is not None else self.indexer
        while watched is not None:
            try:
                kind, msg = watched.events.get_nowait()
            except queue.Empty:
                return
            if kind == "start":       # a Spotify pool: how many clips have to be listened to
                self._first = msg["todo"]
                self._update_count()
                continue
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
                if self.job is not None:
                    self._spot_finish()
                else:
                    self._finish()
                return
            self._update_count()
            watched = self.job if self.job is not None else self.indexer

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
        if self.job is not None:
            self.job.cancel()
        super().closeEvent(e)
