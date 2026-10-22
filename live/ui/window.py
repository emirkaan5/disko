"""The main window: deck A on the left, the mixer in the middle, and on the right either Kev's booth
(Kev DJs: it picks every track after the first and plans each mix) or up next / mix plan / deck B
(you DJ)."""
import math
import queue
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QScrollArea, QSlider,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..audio import SR, Deck, load_deck, sync_ratio
from ..dj import KevDJ
from ..engine import Engine
from ..indexer import DeepAnalyzer
from ..kev import Kev
from ..library import MAX_STRETCH, Library, Track, tempo_match
from .. import mixes
from ..mixes import LANE_DEFAULTS, TECHNIQUES, Plan, Scheduled, build
from . import theme as T
from .booth import Booth
from .browser import Browser
from .deck import DeckPanel, clock
from .mixer import MixerStrip
from .planner import Planner
from .waveform import WaveImage
from .widgets import Segmented, caps, label

LANES = ("fader", "low", "mid", "high", "filter", "echo", "reverb")
IDLE = {k: LANE_DEFAULTS[k] for k in LANES}
SILENT = {**IDLE, "fader": 0.0}


def prepare(track: Track, ratio: float = 1.0) -> tuple:
    """Load a deck and paint its waveform (runs off the UI thread)."""
    deck = load_deck(track, ratio)
    return deck, WaveImage(deck.wave)


def prepare_synced(track: Track, master_period: float) -> tuple:
    return prepare(track, sync_ratio(track, master_period))


class TopBar(QFrame):
    def __init__(self, on_play, on_volume, playlist: str = ""):
        super().__init__()
        self.setObjectName("panel")
        self.setFixedHeight(66)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 8, 18, 8)
        lay.setSpacing(26)
        logo = QLabel(f"disko<span style='color:{T.VIOLET}'>.</span>")
        logo.setFont(T.font(20, QFont.Weight.Bold))
        lay.addWidget(logo)
        self.live = caps("live", T.MUTED, 8.5)
        lay.addWidget(self.live)
        if playlist:
            box = QVBoxLayout()
            box.setSpacing(1)
            box.addWidget(caps("Playlist"))
            name = label(playlist, 12, QFont.Weight.DemiBold)
            name.setMaximumWidth(220)
            box.addWidget(name)
            lay.addLayout(box)
        self.play = QPushButton("▶")
        self.play.setObjectName("round")
        self.play.setFixedSize(38, 38)
        self.play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play.clicked.connect(on_play)
        lay.addWidget(self.play)
        box = QVBoxLayout()
        box.setSpacing(3)
        box.addWidget(caps("Who DJs"))
        self.mode = Segmented((("Kev", True), ("You", False)), True)
        box.addWidget(self.mode)
        lay.addLayout(box)
        self.values = {}
        for key, title in (("tempo", "Set tempo"), ("time", "Set time"), ("tracks", "Tracks played"), ("status", "Now")):
            box = QVBoxLayout()
            box.setSpacing(1)
            box.addWidget(caps(title))
            value = label("—", 13.5, QFont.Weight.DemiBold, mono=key != "status")
            value.setMinimumWidth(120 if key in ("tempo", "status") else 70)
            box.addWidget(value)
            self.values[key] = value
            lay.addLayout(box)
        lay.addStretch(1)
        box = QVBoxLayout()
        box.setSpacing(4)
        box.addWidget(caps("Volume"))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(85)
        self.volume.setFixedWidth(130)
        self.volume.valueChanged.connect(lambda v: on_volume(v / 100))
        box.addWidget(self.volume)
        lay.addLayout(box)
        hint = label("space  play / pause      esc  back", 9, color=T.FAINT)
        lay.addWidget(hint)

    def set(self, playing: bool, bpm, seconds: float, tracks: int, status: str):
        self.play.setText("❚❚" if playing else "▶")
        self.live.setText("● live" if playing else "live")
        self.live.setStyleSheet(f"color: {T.BAD if playing else T.MUTED};")
        for key, text in (("tempo", f"{bpm:.1f} BPM" if bpm else "—"), ("time", clock(seconds)),
                          ("tracks", str(tracks)), ("status", status)):
            if self.values[key].text() != text:
                self.values[key].setText(text)


class History(QFrame):
    """The set so far: tracks and the mixes between them."""

    def __init__(self):
        super().__init__()
        self.setObjectName("panel")
        self.setFixedHeight(54)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 8, 12, 8)
        lay.addWidget(caps("Set", T.TEXT, 9))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        inner = QWidget()
        inner.setObjectName("body")
        self.row = QHBoxLayout(inner)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(8)
        self.row.addStretch(1)
        scroll.setWidget(inner)
        self.scroll = scroll
        lay.addWidget(scroll, 1)
        self.empty = label("Nothing played yet", 10, color=T.FAINT)
        self.row.insertWidget(0, self.empty)

    def _add(self, w: QWidget):
        self.empty.hide()
        self.row.insertWidget(self.row.count() - 1, w)
        QTimer.singleShot(50, lambda: self.scroll.horizontalScrollBar().setValue(
            self.scroll.horizontalScrollBar().maximum()))

    def add_track(self, n: int, track: Track):
        lab = QLabel(f"<span style='color:{T.MUTED}'>{n}</span>&nbsp;&nbsp;{track.title}")
        lab.setFont(T.font(10, QFont.Weight.Medium))
        lab.setStyleSheet(f"background: {T.PANEL_HI}; border: 1px solid {T.BORDER}; border-radius: 8px; padding: 4px 10px;")
        lab.setToolTip(f"{track.display}\n{track.bpm:.1f} BPM · {track.camelot}")
        self._add(lab)

    def add_mix(self, plan: Plan):
        t = TECHNIQUES[plan.technique]
        text = (f"→ {t.name.lower()}" + (f" {plan.params['bars']}" if "bars" in plan.params else "")
                + "".join(f" + {mixes.EFFECTS[k].name.lower()}" for k in plan.effects) + " →")
        lab = QLabel(text)
        lab.setFont(T.font(9, QFont.Weight.DemiBold))
        lab.setStyleSheet(f"color: {T.VIOLET};")
        self._add(lab)


class Toast(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFont(T.font(11, QFont.Weight.Medium))
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, tone: str = "info"):
        color = T.TONE.get(tone, T.VIOLET)
        self.setText(text)
        self.setStyleSheet(f"background: {T.RAISED}; border: 1px solid {T.rgba(color, 0.7)}; border-radius: 10px;"
                           f"padding: 10px 16px; color: {T.TEXT};")
        self.adjustSize()
        p = self.parentWidget()
        self.move((p.width() - self.width()) // 2, p.height() - 14 - 27 - self.height() // 2)  # over the set strip
        self.raise_()
        self.show()
        self._timer.start(3500)


class MainWindow(QMainWindow):
    def __init__(self, library: Library, engine: Engine, pool=None, kev: Kev | None = None,
                 analyzer: DeepAnalyzer | None = None):
        """analyzer maps tracks (allin1) as they come up; None when the library is mapped in full already."""
        super().__init__()
        self.library, self.engine, self.analyzer = library, engine, analyzer
        self.pool = pool or ThreadPoolExecutor(max_workers=3)
        self.kev = kev or Kev()
        self.dj = KevDJ(library, self.kev, prepare, prepare_synced, structure=self._structure)
        self.autopilot = True       # Kev DJs; off = you pick and plan every mix
        self.kev_ready = False
        self.b_view = None          # the deck B panel showing the queued/playing mix
        self.images = {}            # uid -> WaveImage
        self.current: Deck | None = None
        self.played = []
        self.mixes = []  # the set's transitions so far, as Plans
        self.state = "start"        # start, playing, planning, queued, mixing, ended
        self.b_native = self.b_synced = None
        self.scheduled: Scheduled | None = None
        self.jobs = []
        self._token = 0             # bumps when the user changes their mind; stale jobs are dropped
        self.set_seconds = 0.0
        self._last = time.monotonic()
        self._ticks = 0

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(12)
        self.top = TopBar(self.toggle_play, self._volume, f"{library.name} · {len(library.tracks)}")
        v.addWidget(self.top)
        body = QHBoxLayout()
        body.setSpacing(12)
        self.left = DeckPanel("Deck A · now playing", T.DECK_A, library)
        self.left.seek.connect(self._seek)
        self.mixer = MixerStrip()
        self.right = QStackedWidget()
        self.browser = Browser(library)
        self.browser.picked.connect(self._picked)
        self.planner = Planner()
        self.planner.back.connect(self._back)
        self.planner.start.connect(self._start_mix)
        self.incoming = DeckPanel("Deck B · incoming", T.DECK_B, library)
        self.kev_deck = DeckPanel("Deck B · Kev's pick", T.DECK_B, library, compact=True,
                                  empty="Kev hasn't picked yet")
        self.booth = Booth()
        self.booth.repick.connect(self._kev_repick)
        self.booth.lift.connect(self._kev_lift)
        self.booth.skip.connect(self._skip_to_mix)
        self.kev_page = QWidget()
        kv = QVBoxLayout(self.kev_page)
        kv.setContentsMargins(0, 0, 0, 0)
        kv.setSpacing(12)
        kv.addWidget(self.kev_deck, 0)
        kv.addWidget(self.booth, 1)
        for w in (self.browser, self.planner, self.incoming, self.kev_page):
            self.right.addWidget(w)
        body.addWidget(self.left, 11)
        body.addWidget(self.mixer)
        body.addWidget(self.right, 10)
        v.addLayout(body, 1)
        self.history = History()
        v.addWidget(self.history)
        self.toast = Toast(root)

        self.cancel = QPushButton("Cancel queued mix")
        self.cancel.setObjectName("chip")
        self.cancel.clicked.connect(self._cancel_mix)
        self.mixer.layout().addWidget(self.cancel)
        self.cancel.hide()

        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.toggle_play)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._escape)
        self.top.mode.changed.connect(self._set_autopilot)

        self.browser.show_start()
        self.pool.submit(self.kev.start)  # Kev loads while you pick the opening track
        self.setWindowTitle("disko live")
        self.setMinimumSize(1240, 780)
        screen = self.screen().availableGeometry()
        self.resize(min(1580, int(screen.width() * 0.96)), min(960, int(screen.height() * 0.94)))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    # ------------------------------------------------------------ background work

    def _submit(self, fn, *args, then):
        self.jobs.append((self.pool.submit(fn, *args), then, self._token))

    def _poll_jobs(self):
        done = [j for j in self.jobs if j[0].done()]
        for job in done:
            self.jobs.remove(job)
            future, then, token = job
            if token != self._token:
                continue
            try:
                result = future.result()
            except Exception as e:  # a broken file or a failed render shouldn't take the set down
                self.toast.show_message(f"Couldn't do that: {e}", "bad")
                if self.state == "planning":
                    self.planner.tick(self.engine.snapshot().pos)
                continue
            then(result)

    # ------------------------------------------------------------ actions

    def toggle_play(self):
        if self.current is not None:
            self.engine.toggle()

    def _volume(self, v: float):
        self.engine.volume = v

    def _seek(self, frac: float):
        if self.current is None or self.state == "mixing":
            return
        target = int(frac * self.current.length)
        if self.engine.seek(target):
            return
        if self.scheduled is not None and self.engine.cancel():  # jumped past the queued mix: plan a new one
            self.scheduled = None
            self.state = "playing"
            self.left.set_transition()
            self.engine.seek(target)
            if self.autopilot:
                self.booth.log("You skipped past the mix — Kev plans a new one")
                self._kev_think()
            else:
                self.right.setCurrentWidget(self.browser)

    def _skip_to_mix(self):
        """Jump to a few bars before the queued transition, to hear it now."""
        s = self.scheduled
        if s is not None and self.state == "queued":
            self.engine.seek(max(int(s.start - 8 * s.spb), self.engine.snapshot().pos))

    def _structure(self, track: Track) -> float:
        """Block until allin1 has mapped the track (background threads only)."""
        if track.deep or self.analyzer is None:
            return 0.0
        return self.analyzer.analyze(track).result()

    def _picked(self, track: Track):
        if self.current is None or self.state == "ended":
            self._token += 1
            if self.analyzer is not None and not track.deep:  # map it while it plays; Kev's plan waits for it
                self.analyzer.analyze(track)
            self.toast.show_message(f"Loading “{track.title}”…")
            self._submit(prepare, track, 1.0, then=self._opened)
            return
        if self.state not in ("playing", "planning"):
            return
        a = self.current
        if self.analyzer is not None and not (a.track.deep and track.deep):  # the planner needs both mapped
            self._token += 1
            self.browser.heading.setText(f"Mapping “{track.title}”: bars, sections and stems…")
            self.toast.show_message(f"Mapping “{track.title}” before planning the mix, about 15 s…")
            self._submit(lambda: (self._structure(a.track), self._structure(track)), then=lambda _: self._picked(track))
            return
        self._token += 1
        self.b_native = self.b_synced = None
        self.planner.open(a, self.images[a.track.uid], track, self.engine.snapshot().pos)
        self.right.setCurrentWidget(self.planner)
        self.state = "planning"
        self._submit(prepare, track, 1.0, then=self._b_native_ready)
        if tempo_match(a.bpm, track.bpm)[1] <= MAX_STRETCH:
            self._submit(prepare_synced, track, a.period, then=self._b_synced_ready)

    def _b_native_ready(self, result):
        self.b_native, image = result
        self.images[self.b_native.track.uid] = image
        self.planner.set_b(self.b_native, self.b_synced, image)

    def _b_synced_ready(self, result):
        self.b_synced, image = result
        self.images.setdefault(self.b_synced.track.uid, image)
        self.planner.set_b(self.b_native, self.b_synced, self.images[self.b_synced.track.uid])

    def _opened(self, result):
        deck, image = result
        self.images[deck.track.uid] = image
        self.current = deck
        self.engine.load(deck)
        self.engine.play()
        self.played.append(deck.track.uid)
        self.history.add_track(len(self.played), deck.track)
        self.left.set_deck(deck, image)
        self.left.set_transition()
        self.state = "playing"
        self._next_step()

    def _next_step(self):
        """After a track starts: Kev decides what follows, or the list waits for you."""
        if self.autopilot:
            self.right.setCurrentWidget(self.kev_page)
            self._kev_think()
        else:
            self.right.setCurrentWidget(self.browser)
            self.browser.show_next(self.current.track, self.current.bpm, set(self.played))

    # ------------------------------------------------------------ Kev at the decks

    def _kev_think(self, avoid=()):
        if self.current is None or self.scheduled is not None:
            return
        self.kev_deck.set_deck(None, None)
        self.booth.begin()
        recent = [self.library.by_uid[u] for u in self.played]
        self.dj.think(self.current, lambda: self.engine.snapshot().pos, self.played, recent,
                      self.set_seconds / 60, avoid, self.mixes)

    def _set_autopilot(self, on: bool):
        self.autopilot = on
        if self.current is None:
            return
        if on:
            if self.scheduled is None and self.state in ("playing", "planning"):
                self.state = "playing"
                self._next_step()
        else:
            self.dj.cancel()  # a mix Kev already queued still plays; only its next decisions stop
            if self.scheduled is None:
                self.right.setCurrentWidget(self.browser)
                self.browser.show_next(self.current.track, self.current.bpm, set(self.played))

    def _kev_repick(self):
        if not self.autopilot or self.current is None or self.state == "mixing":
            return
        avoid = set()
        if self.scheduled is not None and self.engine.cancel():
            avoid.add(self.scheduled.deck.track.uid)
            self.scheduled = None
            self.state = "playing"
            self.left.set_transition()
        self.booth.log("You asked Kev for another pick")
        self._kev_think(avoid)

    def _kev_lift(self, lift: int):
        self.dj.lift = lift
        if self.autopilot and self.scheduled is None and self.current is not None:
            self._kev_think()  # rethink with the new direction

    def _drain_dj(self):
        while True:
            try:
                kind, data = self.dj.events.get_nowait()
            except queue.Empty:
                return
            self.booth.handle(kind, data)
            if kind == "ready" and self.autopilot:
                s = data["scheduled"]
                self.images[s.deck.track.uid] = data["image"]
                self._rendered(s)
            elif kind == "exhausted":
                self.toast.show_message("That's every track in the playlist — this one closes the set", "warn")
            elif kind == "error" and self.autopilot:
                self.toast.show_message("Kev hit a problem — trying again", "bad")
                QTimer.singleShot(3000, lambda: self.scheduled is None and self._kev_think())

    def _drain_analyzer(self):
        """allin1 finished a track: if it's playing, switch its deck to the real bars and sections."""
        while self.analyzer is not None:
            try:
                kind, track, info = self.analyzer.events.get_nowait()
            except queue.Empty:
                return
            playing = self.current is not None and self.current.track is track
            if kind == "analyzed":
                if playing:
                    self.current.reset_grid()
                    self.left.refresh()
                self.browser.view.viewport().update()
            elif kind == "failed" and playing:
                self.toast.show_message(f"Couldn't map “{track.title}” — mixing by its rough beat grid", "warn")
                self.left.refresh()

    def _back(self):
        self._token += 1
        self.state = "playing"
        self.right.setCurrentWidget(self.browser)

    def _escape(self):
        if self.state == "planning":
            self._back()

    def _start_mix(self, plan: Plan):
        b = self.planner.b_deck()
        if b is None or self.current is None:
            return
        self.planner.go.setEnabled(False)
        self.planner.status.setText("Rendering the transition…")
        self._submit(build, plan, self.current, b, then=self._rendered)

    def _rendered(self, s: Scheduled):
        if self.state == "ended":  # the track ran out before the mix was ready: just start the next one
            self.engine.load(s.deck, max(s.b_offset + int(s.auto.b_start * s.spb), 0))
            self.engine.play()
            self.scheduled = s
            self._promote()
            return
        if not self.engine.schedule(s):
            if self.autopilot:
                self.booth.log("Missed the bar while deciding — Kev rethinks", "warn")
                self._kev_think()
            else:
                self.toast.show_message("Missed that bar — pick a later mix-out point", "warn")
                self.planner.tick(self.engine.snapshot().pos)
            return
        self.scheduled = s
        self.state = "queued"
        mixes.log(s.report)
        self.b_view = self.kev_deck if self.autopilot else self.incoming
        self.b_view.set_deck(s.deck, self.images[s.deck.track.uid],
                             note=f"synced from {s.deck.bpm / s.deck.ratio:.1f}" if s.plan.sync and abs(s.deck.ratio - 1) > 5e-4 else "")
        in_loop = lambda beat: any(at < beat <= until for deck, at, until, _ in s.auto.loops if deck == "A")
        self.left.set_transition((s.start / SR, s.a_at(len(s.buffer)) / SR),  # a mark inside a loop has no one place on A
                                 [(s.a_at(int(b * s.spb)) / SR, t) for b, t in s.auto.marks if not in_loop(b)])
        self.b_view.set_transition((s.b_offset / SR, s.b_after / SR),
                                   [(s.b_at(int(b * s.spb)) / SR, t) for b, t in s.auto.marks],
                                   enters_at=(s.b_offset + s.auto.b_start * s.spb) / SR)
        self.right.setCurrentWidget(self.kev_page if self.autopilot else self.incoming)
        self.cancel.setVisible(not self.autopilot)
        who = "Kev queued" if self.autopilot else "Queued"
        self.toast.show_message(f"{who} {s.plan.tech.name} for bar {s.plan.out_bar}")
        if self.autopilot:
            self.booth.log(f"Queued {s.plan.tech.name.lower()} for bar {s.plan.out_bar} of “{self.current.track.title}”")
            self.booth.log(mixes.describe(s.report), "info" if s.report["out_on_phrase"] and s.report["in_on_phrase"] else "warn")

    def _cancel_mix(self):
        if self.scheduled is not None and self.engine.cancel():
            self.scheduled = None
            self.state = "planning"
            self.cancel.hide()
            self.left.set_transition()
            self.right.setCurrentWidget(self.planner)
            self.planner.tick(self.engine.snapshot().pos)

    def _promote(self):
        """The transition finished: deck B is now the one playing."""
        s = self.scheduled
        self.scheduled = None
        self.cancel.hide()
        self.current = s.deck
        self.played.append(s.deck.track.uid)
        self.mixes.append(s.plan)
        self.history.add_mix(s.plan)
        self.history.add_track(len(self.played), s.deck.track)
        self.left.set_deck(s.deck, self.images[s.deck.track.uid])
        self.left.set_transition()
        if self.b_view is not None:
            self.b_view.set_deck(None, None)
        self.b_view = None
        self.b_native = self.b_synced = None
        self.state = "playing"
        if self.autopilot:
            self.booth.log(f"Now playing “{s.deck.track.title}” — Kev picks what follows")
        self._next_step()

    # ------------------------------------------------------------ the 60 fps loop

    def tick(self):
        self._poll_jobs()
        self._drain_dj()
        self._drain_analyzer()
        if not self.kev_ready and self.kev.info:
            self.kev_ready = True
            self.booth.handle("model", {"name": self.kev.name})
            if self.current is None:
                self.booth.set_status("Kev is ready — pick an opening track", "good")
        now = time.monotonic()
        dt, self._last = now - self._last, now
        self._ticks += 1
        snap = self.engine.snapshot()
        if snap.playing:
            self.set_seconds += dt
        s = self.scheduled
        if s is not None:
            if snap.sched is s:
                self.state = "mixing"
            elif snap.sched is None and snap.deck is s.deck:
                self._promote()
                snap = self.engine.snapshot()
                s = None
        if (self.current is not None and s is None and not snap.playing and self.state in ("playing", "planning")
                and snap.pos >= self.current.length - SR // 5):
            self.state = "ended"
            self._token += 1
            if self.autopilot:  # Kev's pick starts the moment it is ready
                self.booth.log("The track ran out before the mix was ready", "warn")
            else:
                self.right.setCurrentWidget(self.browser)
                self.toast.show_message("Track ended — pick the next one to start it", "warn")

        if self.current is not None:
            self.left.set_position(snap.pos)
        a_vals, b_vals = (dict(IDLE) if self.current is not None else dict(SILENT)), dict(SILENT)
        if s is not None:
            name = s.plan.tech.name
            if snap.sched is s:
                beat = snap.tpos / s.spb
                self.b_view.set_position(s.b_at(snap.tpos))
                a_vals = {k: s.auto.value("A", k, beat) for k in LANES}
                b_vals = {k: s.auto.value("B", k, beat) for k in LANES}
                for deck, vals in (("A", a_vals), ("B", b_vals)):  # a cut on the dry lane shows on the fader
                    vals["fader"] *= s.auto.value(deck, "dry", beat)
                if beat < s.auto.b_start:
                    b_vals["fader"] = 0.0
                total = s.auto.beats / 4
                bar = min(int(beat // 4) + 1, max(math.ceil(total), 1))
                upcoming = next(((b, t) for b, t in s.auto.marks if b > beat + 0.5), None)
                if upcoming:
                    bars_to = (upcoming[0] - beat) / 4
                    detail = f"{upcoming[1]} in {bars_to:.0f} bar{'s' if round(bars_to) != 1 else ''}" \
                        if bars_to >= 1 else f"{upcoming[1]} in {upcoming[0] - beat:.0f} beats"
                else:
                    detail = "B takes over" if beat >= s.auto.b_start else "building up"
                self.mixer.set_info("mixing", f"{name} · bar {bar}/{total:g}", detail, snap.tpos / len(s.buffer),
                                    [b / s.auto.beats for b, _ in s.auto.marks])
                self.left.set_status("mixing out")
                self.b_view.set_status("mixing in")
                if self.autopilot:
                    self.booth.set_status(f"Kev is mixing · bar {bar}/{total:g}", "info")
            else:  # deck B waits on its cue, like a real deck
                self.b_view.set_position(s.b_offset + int(s.auto.b_start * s.spb))
                bars = (s.start - snap.pos) / s.spb / 4
                seconds = (s.start - snap.pos) / SR
                self.mixer.set_info("queued", name, f"starts in {bars:.0f} bars, on bar {s.plan.out_bar}", 0.0)
                self.left.set_status(f"mix in {bars:.0f} bars")
                self.b_view.set_status("cued")
                if self.autopilot:
                    self.booth.set_status(f"Mix queued · starts in {clock(seconds)}", "good")
        else:
            status = {"start": ("idle", "No track yet", "Pick an opening track"),
                      "playing": ("playing", "Playing", "Kev is choosing what's next" if self.autopilot
                                  else "Pick what comes next"),
                      "planning": ("planning", "Planning a mix", "Choose a mix, then Start"),
                      "ended": ("stopped", "Track ended", "Pick the next track")}.get(self.state)
            if status:
                self.mixer.set_info(*status)
            if self.current is not None:
                self.left.set_status("playing" if snap.playing else "paused" if self.state != "ended" else "ended")
            if not snap.playing:
                a_vals["fader"] = 0.0 if self.current is None else a_vals["fader"]
        self.mixer.set_channels(a_vals, b_vals)
        self.mixer.level.set(snap.level if snap.playing else 0.0)
        self.cancel.setVisible(self.state == "queued" and not self.autopilot)  # with Kev, "Pick again" does this
        self.booth.skip_button.setEnabled(self.state == "queued")
        if self.state == "planning" and self._ticks % 30 == 0:
            self.planner.tick(snap.pos)
        status = {"start": "Pick a track", "playing": "Playing", "planning": "Planning", "queued": "Mix queued",
                  "mixing": "Mixing", "ended": "Stopped"}[self.state]
        self.top.set(snap.playing, self.current.bpm if self.current else None, self.set_seconds, len(self.played), status)

    def closeEvent(self, e):
        self.timer.stop()
        self.dj.cancel()
        self.engine.close()
        if self.analyzer is not None:
            self.analyzer.close()
        self.kev.stop()  # only if this app started the server
        super().closeEvent(e)
