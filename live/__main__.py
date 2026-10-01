"""Run the live deck.

    .venv-live/bin/python -m live
    .venv-live/bin/python -m live --snapshot out.png --scenario mix   # render offscreen, no sound
"""
import argparse
import os
import sys
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


def snapshot(win, engine, args):
    import time
    from .audio import SR
    lib = win.library
    kev = args.scenario.startswith("kev")
    if not kev:  # the manual scenarios plan the mix by hand
        win.top.mode.set(False)
        win._set_autopilot(False)
    if args.scenario != "start":
        track = lib.by_uid[args.track]
        win._picked(track)
        win.tick()
        engine.seek(int(SR * args.at))
        engine.advance(SR // 2)
        win.tick()
    if args.scenario in ("plan", "queued", "mix", "after"):
        nxt = lib.by_uid[args.next] if args.next else lib.rank(win.current.track, win.current.bpm, set(win.played))[0].track
        win._picked(nxt)
        win.tick()
        if args.technique:
            win.planner._select(args.technique)
        win.tick()
    if kev:  # let Kev decide (real model calls), then optionally run into its mix
        deadline = time.time() + 120
        while win.scheduled is None and time.time() < deadline:
            win.tick()
            time.sleep(0.05)
        s = win.scheduled
        if s is not None and args.scenario == "kevmix":
            engine.advance(s.start - engine.pos + int(len(s.buffer) * args.progress))
        elif s is not None and args.scenario == "kevafter":  # through the mix; Kev starts on the next pick
            engine.advance(s.start - engine.pos + len(s.buffer) + 4 * SR)
            win.tick()
            deadline = time.time() + 120
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


def schedule_dev(app, win, args):
    """Dev helpers for a real run: start a track, skip to the mix, save screenshots, quit."""
    from PySide6.QtCore import QTimer
    if args.start:
        QTimer.singleShot(500, lambda: win._picked(win.library.by_uid[args.start]))
    for when in args.skip_at or []:
        QTimer.singleShot(int(when * 1000), win._skip_to_mix)
    for spec in args.grab or []:
        path, when = spec.rsplit("@", 1)
        QTimer.singleShot(int(float(when) * 1000), lambda p=path: (win.grab().save(p), print("saved", p, win.state)))
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), win.close)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", help="render the window to this PNG offscreen and exit")
    ap.add_argument("--scenario", default="mix",
                    choices=["start", "browse", "plan", "queued", "mix", "after", "kev", "kevmix", "kevafter"])
    ap.add_argument("--start", help="(real run) open this track uid right away")
    ap.add_argument("--volume", type=float, help="(real run) output volume 0-1")
    ap.add_argument("--grab", action="append", help="(real run) PATH@SECONDS: save a window screenshot then")
    ap.add_argument("--skip-at", type=float, action="append", help="(real run) press 'Skip to the mix' at these seconds")
    ap.add_argument("--quit-after", type=float, help="(real run) close after this many seconds")
    ap.add_argument("--track", default="jamendo_1011780", help="opening track (snapshot)")
    ap.add_argument("--next", help="incoming track (snapshot); default: the top-ranked one")
    ap.add_argument("--technique", help="technique to plan (snapshot)")
    ap.add_argument("--at", type=float, default=95.0, help="seconds into the opening track (snapshot)")
    ap.add_argument("--progress", type=float, default=0.5, help="how far into the transition (snapshot)")
    args = ap.parse_args()
    if args.snapshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from . import dsp
    from .engine import Engine
    from .library import Library
    from .ui import theme
    from .ui.window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("disko live")
    app.setStyleSheet(theme.STYLESHEET)
    app.setFont(theme.font(12.5))
    dsp.warmup()
    library = Library()
    if args.snapshot:
        engine = Engine(audio=False)
        win = MainWindow(library, engine, pool=Immediate())
        win.timer.stop()
        win.resize(1580, 960)
        win.show()
        snapshot(win, engine, args)
        return
    engine = Engine()
    win = MainWindow(library, engine)
    if args.volume is not None:
        win.top.volume.setValue(int(args.volume * 100))
    win.show()
    schedule_dev(app, win, args)
    code = app.exec()
    engine.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
