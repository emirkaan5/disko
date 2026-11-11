"""The welcome screen's DJ-model page: Kev on this Mac, Jev from TypeSafe, or a server of your own
(live/models.py). Installing Kev and checking a key or a server run on threads; the page polls them."""
import html
import queue
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from .. import models
from . import theme as T
from .widgets import label

KEV_TEXT = ("Kev-4B, running on this Mac. Free and private: nothing about your music leaves the computer. "
            "Needs an Apple Silicon Mac and a one-time download of a few GB; the app starts and stops it.")
JEV_TEXT = ("Jev, hosted by TypeSafe. Nothing to install, and quick on any Mac, but it needs an API key, is "
            "billed per request, and the track descriptions the DJ weighs are sent to TypeSafe.")
CUSTOM_TEXT = ("Any server that answers the System One protocol (/v1/systemone): Laya, a Kev on another "
               "machine, or a model you fine-tuned. The key is optional.")


def _button(text: str, primary: bool = False, chip: bool = False, height: int = 34) -> QPushButton:
    b = QPushButton(text)
    if primary or chip:
        b.setObjectName("primary" if primary else "chip")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if not chip:
        b.setFixedHeight(height)
    return b


def _field(placeholder: str, secret: bool = False) -> QLineEdit:
    f = QLineEdit()
    f.setPlaceholderText(placeholder)
    if secret:
        f.setEchoMode(QLineEdit.EchoMode.Password)
    return f


def _wrap(text: str, size: float = 9.5, color: str = T.MUTED, rich: bool = False):
    lab = label(text, size, color=color)
    lab.setWordWrap(True)
    if rich:
        lab.setTextFormat(Qt.TextFormat.RichText)
        lab.setOpenExternalLinks(True)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    return lab


class Option(QFrame):
    """One kind of model: what it is, its state, and what it needs."""

    def __init__(self, kind: str, title: str, text: str):
        super().__init__()
        self.kind = kind
        self.setObjectName(f"model_{kind}")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 14, 16, 14)
        self.lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label(title, 11.5, QFont.Weight.DemiBold))
        head.addStretch(1)
        self.badge = label("", 8.5, QFont.Weight.DemiBold, color=T.VIOLET)
        head.addWidget(self.badge)
        self.lay.addLayout(head)
        self.lay.addWidget(_wrap(text))
        self.status = _wrap("", 9.5, T.FAINT, rich=True)
        self.status.hide()
        self.set_current(False)

    def add_status(self):
        self.lay.addWidget(self.status)

    def say(self, text: str, color: str = T.FAINT, rich: bool = False):
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text if rich else html.escape(text))
        self.status.setVisible(bool(text))

    def set_current(self, current: bool):
        self.badge.setText("IN USE" if current else "")
        border = T.VIOLET if current else T.BORDER
        self.setStyleSheet(f"QFrame#model_{self.kind} {{ background: {T.PANEL_HI}; border: 1px solid {border}; "
                           f"border-radius: 11px; }}")


class ModelPage(QFrame):
    """Pick the DJ's model and get it ready. `back` asks the window for the home page."""

    back = Signal()

    def __init__(self, store: models.ModelStore | None = None, column=None):
        super().__init__()
        self.store = store or models.ModelStore()
        self.setObjectName("panel")
        self.q = queue.Queue()          # (kind, payload) from the install / check threads
        self.work = set()               # which threads are running
        self.install_stop = None        # a threading.Event while Kev is being installed
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        lay = column(self, 660)
        lay.addSpacing(36)
        top = QHBoxLayout()
        logo = QLabel(f"disko<span style='color:{T.VIOLET}'>.</span>")
        logo.setFont(T.font(20, QFont.Weight.Bold))
        top.addWidget(logo)
        top.addStretch(1)
        back = _button("Back", chip=True)
        back.clicked.connect(lambda _=False: self.back.emit())
        top.addWidget(back)
        lay.addLayout(top)
        lay.addSpacing(26)
        lay.addWidget(label("The DJ", 22, QFont.Weight.DemiBold))
        self.state = _wrap("", 10)
        lay.addWidget(self.state)
        lay.addSpacing(14)

        # Kev
        self.kev = Option("kev", "Kev · on this Mac", KEV_TEXT)
        self.kev.add_status()
        row = QHBoxLayout()
        self.kev_install = _button("Install Kev", primary=True)
        self.kev_install.clicked.connect(lambda _=False: self.install_kev())
        row.addWidget(self.kev_install)
        self.kev_stop = _button("Stop", chip=True)
        self.kev_stop.clicked.connect(lambda _=False: self.stop_install())
        self.kev_stop.hide()
        row.addWidget(self.kev_stop)
        self.kev_use = _button("Use Kev")
        self.kev_use.clicked.connect(lambda _=False: self.use(models.ModelChoice("kev")))
        row.addWidget(self.kev_use)
        row.addStretch(1)
        self.kev.lay.addLayout(row)
        lay.addWidget(self.kev)
        lay.addSpacing(10)

        # Jev
        self.jev = Option("jev", "Jev · TypeSafe, hosted", JEV_TEXT)
        self.jev.lay.addWidget(_wrap(f"Get a key at <a style='color:{T.VIOLET}' href='{models.JEV_KEYS}'>"
                                     f"console.typesafe.ai</a>. It is kept in your Keychain.", rich=True))
        row = QHBoxLayout()
        self.jev_key = _field("API key", secret=True)
        self.jev_key.returnPressed.connect(self.use_jev)
        row.addWidget(self.jev_key, 1)
        self.jev_use = _button("Save and use Jev", primary=True)
        self.jev_use.clicked.connect(lambda _=False: self.use_jev())
        row.addWidget(self.jev_use)
        self.jev.lay.addLayout(row)
        self.jev.add_status()
        lay.addWidget(self.jev)
        lay.addSpacing(10)

        # Custom
        self.custom = Option("custom", "Something else · a server of your own", CUSTOM_TEXT)
        self.custom_url = _field("Server address, e.g. http://127.0.0.1:8010")
        self.custom.lay.addWidget(self.custom_url)
        row = QHBoxLayout()
        self.custom_model = _field("Model name (optional)")
        row.addWidget(self.custom_model, 1)
        self.custom_key = _field("API key (optional)", secret=True)
        row.addWidget(self.custom_key, 1)
        self.custom.lay.addLayout(row)
        row = QHBoxLayout()
        self.custom_use = _button("Save and use this server", primary=True)
        self.custom_use.clicked.connect(lambda _=False: self.use_custom())
        row.addWidget(self.custom_use)
        row.addStretch(1)
        self.custom.lay.addLayout(row)
        self.custom.add_status()
        lay.addWidget(self.custom)
        lay.addStretch(1)

    # ------------------------------------------------------------ drawing

    def options(self) -> dict:
        return {"kev": self.kev, "jev": self.jev, "custom": self.custom}

    def refresh(self, kev_state: models.KevState | None = None):
        """Draw the page from the stored choice and Kev's files (no network: checks run on a thread)."""
        choice = self.store.read()
        for kind, option in self.options().items():
            option.set_current(kind == choice.kind)
        state = kev_state or models.kev_state()
        if self.install_stop is None:
            self.kev_install.setVisible(not state.ready)
            self.kev_install.setText("Install Kev" if not (state.code or state.environment) else "Finish installing")
            self.kev.say("Installed." if state.ready else f"Still to do: {state.summary}.",
                         T.GOOD if state.ready else T.FAINT)
        self.kev_use.setVisible(choice.kind != "kev")
        self.kev_use.setEnabled(state.ready)
        has_key = bool(self.store.key("jev"))
        self.jev_key.setPlaceholderText("API key (saved; paste a new one to replace it)" if has_key else "API key")
        if choice.kind == "custom":
            self.custom_url.setText(self.custom_url.text() or choice.url)
            self.custom_model.setText(self.custom_model.text() or choice.model)
        self.state.setText(self.summary(choice, state))

    def summary(self, choice: models.ModelChoice, kev_state: models.KevState) -> str:
        if choice.kind == "kev":
            return ("Kev makes the calls: which track comes next and how to mix into it."
                    if kev_state.ready else "Kev is chosen but isn't installed yet: install it below, or choose "
                                            "another model.")
        if choice.kind == "jev":
            return "Jev makes the calls, through TypeSafe's API."
        return f"A server of your own makes the calls: {choice.base_url}."

    # ------------------------------------------------------------ choosing

    def use(self, choice: models.ModelChoice):
        self.store.write(choice)
        self.refresh()

    def use_jev(self):
        typed = self.jev_key.text().strip()
        if not typed and not self.store.key("jev"):
            self.jev.say("Paste your API key first.", T.WARN)
            return
        try:
            if typed:
                self.store.set_key("jev", typed)
        except Exception as e:
            self.jev.say(f"The key couldn't be saved in the Keychain: {e}", T.BAD)
            return
        self.jev_key.clear()
        self._check(models.ModelChoice("jev"), self.jev)

    def use_custom(self):
        url = self.custom_url.text().strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            self.custom.say("The address starts with http:// or https://.", T.WARN)
            return
        try:
            self.store.set_key("custom", self.custom_key.text())
        except Exception as e:
            self.custom.say(f"The key couldn't be saved in the Keychain: {e}", T.BAD)
            return
        self.custom_key.clear()
        self._check(models.ModelChoice("custom", url, self.custom_model.text().strip()), self.custom)

    def _check(self, choice: models.ModelChoice, option: Option):
        """Ask the server (on a thread) whether it answers and takes the key; use it only if it does."""
        option.say("Checking…")
        name = f"check-{choice.kind}"

        def work():
            try:
                problem = models.check(choice, self.store)
            except Exception as e:
                problem = f"{type(e).__name__}: {e}"
            self.q.put(("checked", (choice, problem)))
        self._start(name, work)

    # ------------------------------------------------------------ installing Kev

    def install_kev(self):
        if self.install_stop is not None:
            return
        stop = self.install_stop = threading.Event()
        self.kev_install.setEnabled(False)
        self.kev_install.setText("Installing…")
        self.kev_stop.show()
        self.kev.say("Starting…")

        def work():
            try:
                self.q.put(("installed", models.install_kev(lambda line: self.q.put(("kev_line", line)),
                                                            stop.is_set)))
            except models.SetupFailed as e:
                self.q.put(("install_error", str(e)))
            except Exception as e:
                self.q.put(("install_error", f"{type(e).__name__}: {e}"))
        self._start("install", work)

    def stop_install(self):
        if self.install_stop is not None:
            self.install_stop.set()

    def _install_end(self):
        self.install_stop = None
        self.kev_install.setEnabled(True)
        self.kev_stop.hide()

    # ------------------------------------------------------------ threads

    def _start(self, name: str, target):
        self.work.add(name)
        threading.Thread(target=target, daemon=True, name=f"model-{name}").start()
        self.timer.start(200)

    def _poll(self):
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "kev_line":
                self.kev.say(payload[:200])
            elif kind == "installed":
                self.work.discard("install")
                self._install_end()
                self.refresh(payload)
            elif kind == "install_error":
                self.work.discard("install")
                self._install_end()
                self.refresh()
                self.kev.say(payload, T.BAD)
                self.kev_install.setText("Try again")
            elif kind == "checked":
                choice, problem = payload
                self.work.discard(f"check-{choice.kind}")
                option = self.options()[choice.kind]
                if problem:
                    option.say(f"Not switched: {problem}.", T.BAD)
                else:
                    self.store.write(choice)
                    self.refresh()
                    option.say("It answers. The next set uses it.", T.GOOD)
        if not self.work:
            self.timer.stop()

    def close_work(self):
        """The window is closing: stop an install (a check just finishes on its own)."""
        self.stop_install()
        self.timer.stop()
