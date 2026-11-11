"""Run the live deck.

    .venv-live/bin/python -m live                          # welcome screen: pick a playlist folder
    .venv-live/bin/python -m live --playlist ~/Music/set   # open that folder straight away
    .venv-live/bin/python -m live --house                  # the house test set, analyzed in full in advance
    .venv-live/bin/python -m live --fake 12                # a synthetic catalog (live/sources/fake.py): no real music
    .venv-live/bin/python -m live --beatport chart:1234     # a Beatport list as the library (needs a sign-in;
                                                            # DISKO_BEATPORT_STREAMING=1 also plays it)
    .venv-live/bin/python -m live --spotify liked          # a Spotify playlist as the library (needs a sign-in
                                                            # and Spotify audio turned on by the listener)
    .venv-live/bin/python -m live --playlist DIR --no-control  # don't take commands from live.ctl (live/control.py);
                                                               # the control socket is on by default
    .venv-live/bin/python -m live --headless --playlist DIR   # no window: Kev DJs, controlled through live.ctl
    .venv-live/bin/python -m live --snapshot out.png --scenario mix   # render offscreen, no sound
"""
import argparse
import os
import sys
import time
from concurrent.futures import Future


class Immediate:
    """An executor that runs jobs on the spot (for snapshots)."""

    def submit(self, fn, *args):
        f = Future()
        try:
            f.set_result(fn(*args))
        except Exception as e:
            f.set_exception(e)
        return f


def find_track(library, key: str):
    """A track by uid, or the first whose title contains `key`."""
    if key in library.by_uid:
        return library.by_uid[key]
    match = next((t for t in library.tracks if key.lower() in t.title.lower()), None)
    if match is None:
        sys.exit(f"no track matches {key!r}")
    return match


def snapshot(win, engine, args):
    from .audio import SR
    lib = win.library
    kev = args.scenario.startswith("kev")
    if not kev:  # the manual scenarios plan the mix by hand
        win.top.mode.set(False)
        win._set_autopilot(False)
    if args.scenario != "start":
        track = find_track(lib, args.track)
        win._picked(track)
        win.tick()
        engine.seek(int(SR * args.at))
        engine.advance(SR // 2)
        win.tick()
    if args.scenario in ("plan", "queued", "mix", "after"):
        nxt = find_track(lib, args.next) if args.next else lib.rank(win.current.track, win.current.bpm, set(win.played))[0].track
        win._picked(nxt)
        win.tick()
        if args.technique:
            win.planner._select(args.technique)
        for key in filter(None, (args.effects or "").split(",")):
            win.planner._effect(key, True)
        if args.riser_end:
            win.planner._riser_end(args.riser_end)
        win.tick()
    if kev:  # let Kev decide (real model calls), then optionally run into its mix
        deadline = time.time() + 240
        while win.scheduled is None and time.time() < deadline:
            win.tick()
            time.sleep(0.05)
        s = win.scheduled
        if s is not None and args.scenario == "kevmix":
            engine.advance(s.start - engine.pos + int(len(s.buffer) * args.progress))
        elif s is not None and args.scenario == "kevafter":  # through the mix; Kev starts on the next pick
            engine.advance(s.start - engine.pos + len(s.buffer) + 4 * SR)
            win.tick()
            deadline = time.time() + 240
            while win.scheduled is None and time.time() < deadline:
                win.tick()
                time.sleep(0.05)
        win.tick()
    if args.scenario in ("queued", "mix", "after"):
        win.planner._start()
        win.tick()
        s = win.scheduled
        if s is not None and args.scenario == "mix":
            engine.advance(s.start - engine.pos + int(len(s.buffer) * args.progress))
        elif s is not None and args.scenario == "after":
            engine.advance(s.start - engine.pos + len(s.buffer) + 8 * SR)
        else:
            engine.advance(SR)
        win.tick()
    from PySide6.QtWidgets import QApplication
    for _ in range(5):  # let layouts settle: one pass can queue another
        QApplication.processEvents()
        win.tick()
    win.grab().save(args.snapshot)
    print("saved", args.snapshot, "state:", win.state)


def welcome_snapshot(args):
    """The welcome screen (with --playlist: as that folder is being indexed for real)."""
    from PySide6.QtWidgets import QApplication

    from .ui.welcome import WelcomeWindow
    w = WelcomeWindow()
    w.resize(1580, 960)
    w.show()
    if args.scenario == "welcome":
        home = os.path.expanduser("~")
        w.show_home([(f"{home}/Music/Friday warm-up", "64 tracks · 12 played before"),
                     (f"{home}/Projects/disko/data/house/audio", "568 tracks")])
    elif args.scenario == "spotify":     # the state a listener meets just after signing in
        from .sources.spotify import GateStatus
        w.show_spotify([("liked", "Liked Songs", 812), ("37i9dQZF1DXcBWIGoYBM5M", "Today's Top Hits", 50),
                        ("2v3iNvBX8Ay1Gt2uXtUKUT", "late night drive", 128), ("69F2g4zXZ2nQKTECAj4bQl", "warm up", None)],
                       state=GateStatus(True, True, True, ("BROWSE", "METADATA"), "ready", audio=False))
    else:
        if not args.playlist:
            sys.exit("--scenario indexing needs --playlist (a folder that isn't indexed yet)")
        w.open(args.playlist)
        deadline = time.time() + 300
        total = len(w.playlist.files)
        while time.time() < deadline and w.pages.currentWidget() is w.index and w.timer.isActive():
            QApplication.processEvents()
            if w._done + w._failed >= total * args.progress:
                break
            time.sleep(0.05)
    for _ in range(5):
        QApplication.processEvents()
    w.grab().save(args.snapshot)
    print("saved", args.snapshot)
    if w.indexer is not None:
        w.indexer.cancel()


def schedule_dev(app, win, args):
    """Dev helpers for a real run: start a track, skip to the mix, save screenshots, quit."""
    from PySide6.QtCore import QTimer
    if args.start:
        QTimer.singleShot(500, lambda: win._picked(find_track(win.library, args.start)))
    for when in args.skip_at or []:
        QTimer.singleShot(int(when * 1000), win._skip_to_mix)
    for spec in args.grab or []:
        path, when = spec.rsplit("@", 1)
        QTimer.singleShot(int(float(when) * 1000), lambda p=path: (win.grab().save(p), print("saved", p, win.state)))
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), win.close)


def start_control(session, tcp, into: dict):
    from .control import ControlServer
    if "control" in into:  # a second window takes the commands over
        into.pop("control").close()
    server = ControlServer(session, tcp=tcp).start()
    into["control"] = server
    where = f" and {server.tcp_address[0]}:{server.tcp_address[1]}" if server.tcp_address else ""
    print(f"control: {server.socket_path}{where} (python -m live.ctl status)", flush=True)
    return server


def headless(args, tcp) -> int:
    """Kev DJs with no window; the set is started and steered through the control interface."""
    import signal

    from .control import write_install_record
    from .engine import Engine
    from .indexer import DeepAnalyzer, FastIndexer
    from .kev import Kev
    from .library import Library
    from .playlist import Playlist
    from .runtime import close_session_temp
    from .session import Session
    from .sources import SourceError

    def prepare(track, ratio=1.0):
        from .audio import load_deck
        return load_deck(track, ratio), None

    def prepare_synced(track, master_period):
        from .audio import sync_ratio
        return prepare(track, sync_ratio(track, master_period))

    analyzer = None
    if args.fake:
        library, analyzer = analyzed(fake_source(args.fake))
    elif args.beatport:
        library, analyzer = analyzed(*beatport_pool(args.beatport, args.beatport_limit))
    elif args.spotify:
        try:
            library, analyzer = spotify_set(args.spotify, args.spotify_limit)
        except SourceError as e:
            print(f"spotify: {e}", file=sys.stderr)
            return 1
    elif args.playlist:
        playlist = Playlist(args.playlist)
        indexer = FastIndexer(playlist)
        indexer.start()
        print(f"indexing {len(indexer.todo)} new files in {playlist.folder}…", flush=True)
        while True:
            kind, info = indexer.events.get()
            if kind == "fatal":
                print("indexing failed:", info["error"], file=sys.stderr)
                return 1
            if kind == "finished":
                break
        library = Library(playlist.load(), name=playlist.name)
        analyzer = DeepAnalyzer(playlist)
        analyzer.start()
    elif args.house:
        library = Library()
    else:
        print("--headless needs --playlist DIR, --beatport LIST, --spotify PLAYLIST, --house or --fake N",
              file=sys.stderr)
        return 2
    write_install_record()
    kev = Kev()
    engine = Engine()
    session = Session(library, engine, kev, prepare, prepare_synced, analyzer=analyzer)  # owned by this thread
    live = {}
    stop = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    try:
        start_control(session, tcp, live)  # before Kev's server: if control can't start, nothing is left running
        session.pool.submit(kev.start)
        print(f"{library.name}: {len(library.tracks)} tracks. Start the set with: python -m live.ctl start", flush=True)
        while not stop:
            session.tick()
            time.sleep(0.02)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        if "control" in live:
            live["control"].close()
        session.close()
        engine.close()
        if analyzer is not None:
            analyzer.close()
        close_session_temp()
        kev.stop()  # only if this run started it
    return 0


def fake_source(n: int):
    from .sources.fake import FakeSource
    src = FakeSource(n=n, seconds=180.0)
    return src, src.library_tracks(), src.label


def fake_library(n: int):
    """The synthetic catalog as it comes, with no analysis (for snapshots)."""
    from .library import Library
    _, tracks, name = fake_source(n)
    return Library(tracks, name=name)


def spotify_pool(playlist: str, limit: int):
    """A Spotify playlist as a bounded candidate pool (SPOTIFY_INTEGRATION_PLAN.md, Milestones 4 and 6).

    Spotify's catalog carries no tempo and no key, so the pool is built from a 30 s preview of every
    track (Milestone 4) and nothing is fetched in full until a track is picked (Milestone 5). Audio
    has to be turned on first, by the listener themselves: without it a Spotify track can be browsed
    but not played, and there would be no set to run."""
    from .runtime import session_temp
    from .sources import Operation, SourceError
    from .sources.pool import preview_pool
    from .sources.spotify import ConsentStore, connect, load_policy
    from .sources.spotify.fetch import Fetcher
    from .sources.spotify.preview import PreviewFetcher
    from .sources.store import SourceStore
    settings = ConsentStore()
    policy = load_policy(settings=settings)
    if not policy.allows(Operation.TEMP_AUDIO):
        raise SourceError("Spotify audio is off. Turn it on first and read what it means:\n"
                          "  python -m live.sources.spotify audio on\n"
                          "  python -m live.sources.spotify status")
    temp = session_temp()
    source = connect(policy=policy, settings=settings, fetcher=Fetcher(temp.folder("fetch")))
    store = SourceStore.for_source(source, temp)
    previews = PreviewFetcher(store.dir / "previews")

    def progress(kind, info):
        if kind == "failed":
            print(f"  no features for {info.get('title') or info.get('rel')}: {info['error']}", file=sys.stderr)

    pool = preview_pool(source, playlist, previews, store, limit=limit, progress=progress)
    print(f"Spotify {playlist}: {pool.summary()}", flush=True)
    for info, why in pool.skipped[:10]:
        print(f"  left out: {info.artist} – {info.title}: {why}", flush=True)
    if len(pool.skipped) > 10:
        print(f"  and {len(pool.skipped) - 10} more left out", flush=True)
    if not pool.tracks:
        raise SourceError(f"nothing in this playlist can be played: {pool.summary()}")
    return source, pool, store, temp, f"Spotify {playlist}"


def spotify_set(playlist: str, limit: int):
    """(Library, DeepAnalyzer or None) for a Spotify playlist, ready for a set (Milestone 6)."""
    from .indexer import DeepAnalyzer
    from .library import Library
    source, pool, store, temp, name = spotify_pool(playlist, limit)
    analyzer = DeepAnalyzer(store, temp)
    try:  # its imports take a few seconds: done before the first pick, as for a folder
        analyzer.start()
    except RuntimeError as e:
        print(f"note: {e}; Spotify tracks will be mixed on their preview tempo", file=sys.stderr)
        analyzer = None
    return Library(pool.tracks, name=name), analyzer


def beatport_pool(list_id: str, limit: int):
    """A Beatport playlist or chart as a bounded candidate pool (metadata only until a track is
    played, when its stream is decoded). Reading the audio needs the streaming plan."""
    from .sources.beatport import connect, streaming_enabled
    from .sources.pool import candidate_pool
    source = connect()
    pool = candidate_pool(source, list_id, limit=limit)
    if not streaming_enabled():
        print("note: DISKO_BEATPORT_STREAMING isn't set, so playing a track will be refused; set it to 1 "
              "once your Beatport Streaming plan is active", file=sys.stderr)
    return source, pool.tracks, f"Beatport {list_id}"


def analyzed(source, tracks: list, name: str):
    """(Library, DeepAnalyzer or None) for a source's tracks. When its policy lets disko decode and
    analyze them through the session temp folder, every track gets the fast pass first (cached by
    track id) and each pick gets allin1 as it comes up; otherwise they go in as the source described
    them, on its own tempo and a rough grid."""
    from .indexer import DeepAnalyzer, SourceIndexer
    from .library import Library
    from .runtime import session_temp
    from .sources.store import TEMP_ANALYSIS, SourceStore
    if not all(source.policy.allows(op) for op in TEMP_ANALYSIS):
        return Library(tracks, name=name), None
    temp = session_temp()
    store = SourceStore.for_source(source, temp)
    indexer = SourceIndexer(store, tracks, temp)
    if indexer.todo:
        print(f"analyzing {len(indexer.todo)} {source.label} tracks (each decoded into {temp.dir}, "
              f"deleted once analyzed)…", flush=True)

        def progress(kind, info):
            if kind == "failed":
                print(f"  couldn't analyze {info.get('title') or info.get('rel')}: {info['error']}", file=sys.stderr)

        try:
            counts = indexer.run(progress)
            print(f"  {counts['done']} analyzed, {counts['failed']} failed", flush=True)
        except RuntimeError as e:
            print(f"  analysis stopped: {e}; going on with the source's own metadata", file=sys.stderr)
    analyzer = DeepAnalyzer(store, temp)
    try:
        analyzer.start()  # its imports take a few seconds: get them done before the first pick
    except RuntimeError as e:
        print(f"note: {e}; tracks will be mixed on the rough grid", file=sys.stderr)
        analyzer = None
    return Library(tracks, name=name), analyzer


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--playlist", help="open this folder instead of showing the welcome screen")
    ap.add_argument("--house", action="store_true", help="use the house test set (data/house), skipping the welcome screen")
    ap.add_argument("--fake", type=int, metavar="N",
                    help="use a synthetic catalog of N generated tracks (tests the source/policy path; no real music)")
    ap.add_argument("--beatport", metavar="LIST",
                    help="run a set from a Beatport list (my:ID, catalog:ID or chart:ID); needs a sign-in "
                         "and DISKO_BEATPORT_STREAMING=1 to play")
    ap.add_argument("--beatport-limit", type=int, default=100, metavar="N",
                    help="how many tracks to take from the Beatport list (default 100)")
    ap.add_argument("--spotify", metavar="PLAYLIST",
                    help="run a set from a Spotify playlist (an id, a link or `liked`); needs a sign-in and "
                         "Spotify audio turned on with `python -m live.sources.spotify audio on`")
    ap.add_argument("--spotify-limit", type=int, default=100, metavar="N",
                    help="how many tracks to take from the Spotify playlist (default 100)")
    ap.add_argument("--control", action="store_true",
                    help="accept commands on the local control socket (~/.disko/control.sock, this user only); "
                         "on by default, kept for compatibility")
    ap.add_argument("--no-control", action="store_true", help="don't serve the control socket")
    ap.add_argument("--control-tcp", metavar="HOST:PORT",
                    help="also accept them over TCP (token-protected; for a controller in a container)")
    ap.add_argument("--headless", action="store_true", help="no window: Kev DJs, controlled with python -m live.ctl")
    ap.add_argument("--snapshot", help="render the window to this PNG offscreen and exit")
    ap.add_argument("--scenario", default="mix",
                    choices=["welcome", "spotify", "indexing", "start", "browse", "plan", "queued", "mix", "after", "kev", "kevmix",
                             "kevafter"])
    ap.add_argument("--start", help="(real run) open this track (uid or part of its title) right away")
    ap.add_argument("--volume", type=float, help="(real run) output volume 0-1")
    ap.add_argument("--grab", action="append", help="(real run) PATH@SECONDS: save a window screenshot then")
    ap.add_argument("--skip-at", type=float, action="append", help="(real run) press 'Skip to the mix' at these seconds")
    ap.add_argument("--quit-after", type=float, help="(real run) close after this many seconds")
    ap.add_argument("--track", help="opening track, uid or part of its title (snapshot)")
    ap.add_argument("--next", help="incoming track (snapshot); default: the top-ranked one")
    ap.add_argument("--technique", help="technique to plan (snapshot)")
    ap.add_argument("--effects", help="comma-separated effects to add in the planner (snapshot)")
    ap.add_argument("--riser-end", choices=["cut", "release"], help="how a riser ends (snapshot)")
    ap.add_argument("--at", type=float, default=95.0, help="seconds into the opening track (snapshot)")
    ap.add_argument("--progress", type=float, default=0.5, help="how far into the transition, or the indexing (snapshot)")
    args = ap.parse_args()
    if args.snapshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    tcp = None
    if args.control_tcp:
        host, _, port = args.control_tcp.rpartition(":")
        tcp = (host or "127.0.0.1", int(port))
    if args.headless:
        sys.exit(headless(args, tcp))

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from . import dsp
    from .engine import Engine
    from .runtime import close_session_temp
    from .indexer import DeepAnalyzer
    from .kev import Kev
    from .library import Library
    from .playlist import Playlist
    from .ui import theme
    from .ui.welcome import WelcomeWindow
    from .ui.window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("disko live")
    app.setStyleSheet(theme.STYLESHEET)
    app.setFont(theme.font(12.5))

    if args.snapshot and args.scenario in ("welcome", "spotify", "indexing"):
        welcome_snapshot(args)
        return
    dsp.warmup()
    if args.snapshot:
        analyzer = None
        if args.playlist:  # a folder indexed already; allin1 maps tracks for real as they come up
            playlist = Playlist(args.playlist)
            library = Library(playlist.load(), name=playlist.name)
            analyzer = DeepAnalyzer(playlist)
        else:
            library = fake_library(args.fake) if args.fake else Library()
        args.track = args.track or ("fake:t00" if args.fake else "jamendo_1011780")
        engine = Engine(audio=False)
        win = MainWindow(library, engine, pool=Immediate(), analyzer=analyzer)
        win.timer.stop()
        win.resize(1580, 960)
        win.show()
        try:
            snapshot(win, engine, args)
        finally:
            win.close()
        return

    from .control import write_install_record
    write_install_record()
    kev = Kev()
    live = {}

    def open_set(library, analyzer=None, geometry=None):
        engine = Engine()
        win = MainWindow(library, engine, kev=kev, analyzer=analyzer)
        if geometry is not None:
            win.setGeometry(geometry)
        if args.volume is not None:
            win.top.volume.setValue(int(args.volume * 100))
        win.show()
        schedule_dev(app, win, args)
        live.update(win=win, engine=engine)
        if not args.no_control or tcp:
            try:
                start_control(win.session, tcp, live)
            except (RuntimeError, OSError) as e:  # on by default, so it mustn't keep the window from opening
                print(f"note: control is off ({e})", file=sys.stderr)

    if args.fake:
        open_set(*analyzed(*fake_source(args.fake)))
    elif args.beatport:
        open_set(*analyzed(*beatport_pool(args.beatport, args.beatport_limit)))
    elif args.spotify:
        from .sources import SourceError
        try:
            open_set(*spotify_set(args.spotify, args.spotify_limit))
        except SourceError as e:
            sys.exit(f"spotify: {e}")
    elif args.house:
        open_set(Library())
    else:
        welcome = WelcomeWindow(QSettings("disko", "disko live"))

        def ready(playlist):
            analyzer = DeepAnalyzer(playlist)
            analyzer.start()  # its imports take a few seconds: get them done before the first pick
            open_set(Library(playlist.tracks, name=playlist.name), analyzer, welcome.geometry())
            welcome.close()

        def ready_set(chosen):           # a Spotify playlist the window pooled itself
            open_set(*chosen, welcome.geometry())
            welcome.close()

        welcome.ready.connect(ready)
        welcome.ready_set.connect(ready_set)
        welcome.show()
        if args.playlist:
            welcome.open(args.playlist)
    code = app.exec()
    if "control" in live:
        live["control"].close()
    if "engine" in live:
        live["engine"].close()
    close_session_temp()
    kev.stop()
    sys.exit(code)


if __name__ == "__main__":
    main()
