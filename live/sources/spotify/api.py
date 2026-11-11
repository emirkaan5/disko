"""Spotify Web API calls: bearer auth with one refresh on 401, bounded retries on 429/5xx, mapped
errors. Every call is a GET; the token never appears in a URL, a log or an error message.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..base import AuthExpired, SourceError, SourceTimeout, TrackUnavailable
from .auth import API, HTTP_TIMEOUT_S

MAX_RETRY_AFTER_S = 10.0   # wait for a rate limit this long at most, then give up for now
RETRIES = 2


class NotFound(TrackUnavailable):
    """404: gone, private to someone else, or not visible to this app."""


class Forbidden(TrackUnavailable):
    """403: this account or app isn't allowed it (for example Spotify's own editorial playlists,
    which apps in development mode can't read)."""


class Client:
    def __init__(self, session, api: str = API, timeout: float = HTTP_TIMEOUT_S, sleep=time.sleep):
        self.session, self.api, self.timeout, self.sleep = session, api.rstrip("/"), timeout, sleep

    def get(self, path: str, params: dict | None = None) -> dict:
        if not path.startswith("/") or ".." in path:
            raise ValueError(f"bad API path {path!r}")
        url = self.api + path + ("?" + urllib.parse.urlencode(params) if params else "")
        refreshed = False
        for attempt in range(RETRIES + 1):
            token = self.session.access_token()
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                       "Accept": "application/json", "User-Agent": "disko"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                e.read()
                e.close()
                if e.code == 401 and not refreshed:  # the token died early: renew it once, then ask again
                    refreshed = True
                    self.session.access_token(force_refresh=True)
                    continue
                if e.code == 401:
                    raise AuthExpired("Spotify no longer accepts this sign-in: sign in again") from None
                if e.code == 403:
                    raise Forbidden(f"Spotify doesn't allow this app or account {path}") from None
                if e.code == 404:
                    raise NotFound(f"not on Spotify, or not visible to this account ({path})") from None
                if e.code == 429 or e.code >= 500:
                    wait = _retry_after(e.headers.get("Retry-After"), attempt)
                    if attempt < RETRIES and wait <= MAX_RETRY_AFTER_S:
                        self.sleep(wait)
                        continue
                    raise SourceTimeout(f"Spotify is busy ({e.code}); try again shortly") from None
                raise SourceError(f"Spotify answered {e.code} for {path}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < RETRIES:
                    self.sleep(_retry_after(None, attempt))
                    continue
                raise SourceTimeout(f"couldn't reach Spotify ({getattr(e, 'reason', e)})") from None
            except ValueError:
                raise SourceError(f"Spotify sent something that isn't JSON for {path}") from None
        raise SourceTimeout("Spotify didn't answer")  # pragma: no cover (the loop always returns or raises)

    def page(self, path: str, offset: int, limit: int, params: dict | None = None) -> tuple:
        """(items, total or None) for one offset/limit page of a Spotify paging object."""
        data = self.get(path, {**(params or {}), "offset": offset, "limit": limit})
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise SourceError(f"Spotify's answer for {path} has no items list")
        total = data.get("total")
        return items, total if isinstance(total, int) and total >= 0 else None


def _retry_after(header, attempt: int) -> float:
    try:
        return max(0.0, float(header))
    except (TypeError, ValueError):
        return 0.5 * 2 ** attempt
