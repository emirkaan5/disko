"""The listener's own files: a playlist folder (live/playlist.py) seen as a source.

Track keys are the folder's file ids (from each file's path inside the folder), so analysis
saved in <folder>/disko/ keeps matching. Local files are the one source with every operation
allowed, analysis on disk included.
"""
from pathlib import Path

from .base import Availability, AudioHandle, Page, PlaylistRef, Source, SourceError, TrackId, TrackInfo
from .policy import Policy


class FileAudio(AudioHandle):
    def __init__(self, track_id: TrackId, path: Path):
        super().__init__(track_id)
        self.path = Path(path)

    def _read(self) -> tuple:
        from ..audio import decode  # the decoder needs the audio stack; browsing doesn't
        if not self.path.exists():
            raise SourceError(f"{self.path} is gone")
        return decode(self.path)


class LocalSource(Source):
    provider = "local"
    label = "This Mac"

    def __init__(self, playlist):
        super().__init__(Policy.local())
        self.playlist = playlist

    def _playlists(self) -> list:
        return [PlaylistRef(self.playlist.folder.as_posix(), self.playlist.name, len(self.playlist.files))]

    def _tracks(self, playlist_id: str, offset: int, limit: int) -> Page:
        if playlist_id != self.playlist.folder.as_posix():
            raise SourceError(f"no playlist {playlist_id!r} here")
        files = self.playlist.files[offset:offset + limit]
        return Page(tuple(self._info(TrackId(self.provider, f.id)) for f in files), offset, len(self.playlist.files))

    def _info(self, track_id: TrackId) -> TrackInfo:
        f = self.playlist.by_id.get(track_id.key)
        if f is None:
            raise SourceError(f"no file {track_id.key} in {self.playlist.name}")
        t = next((t for t in self.playlist.tracks if t.uid == track_id.key), None)
        if t is None:  # not through the fast pass yet: only the file name is known
            from ..playlist import guess_names
            title, artist = guess_names(f.rel)
            return TrackInfo(track_id, title, artist or None, availability=Availability.AVAILABLE,
                             provenance={"title": "file name"})
        features = {k: getattr(t, k) for k in ("bpm", "camelot", "energy", "mood", "vocals") if t.known(k)}
        return TrackInfo(track_id, t.title, t.artist, t.duration, Availability.AVAILABLE,
                         tuple(t.genre_list), features, dict(t.provenance))

    def _open_audio(self, track_id: TrackId) -> AudioHandle:
        f = self.playlist.by_id.get(track_id.key)
        if f is None:
            raise SourceError(f"no file {track_id.key} in {self.playlist.name}")
        return FileAudio(track_id, f.path)
