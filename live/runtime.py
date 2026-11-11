"""The one temp folder this run may write decoded audio to, shared by everything that needs it.

A streamed or provider-supplied track is decoded into a session temp folder so an analysis helper
can read it, and each file goes as soon as its analysis is done (live/sources/store.py, SessionTemp).
The folder is made once per run and removed when the app exits. It lives here, rather than in
live/__main__.py, because the window builds a Spotify pool before the set window exists and has to
hand the same folder to the analysis store, the fetch helper and the deep pass: two folders would
mean audio that outlives the run's one place for it.
"""
from .sources.store import SessionTemp

_temp: SessionTemp | None = None


def session_temp() -> SessionTemp:
    """This run's temp folder, created on first use."""
    global _temp
    if _temp is None:
        _temp = SessionTemp()
    return _temp


def close_session_temp():
    """Remove the folder and everything still in it; does nothing if it was never needed."""
    if _temp is not None:
        _temp.close()
