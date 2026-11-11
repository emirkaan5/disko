"""Spotify: sign in with the listener's own Spotify developer app, browse playlists and Liked Songs,
read catalog metadata, and - only after the listener turns it on themselves - read and mix the audio
(SPOTIFY_INTEGRATION_PLAN.md, Milestones 1, 2 and 5).

    auth.py     OAuth2 authorization code + PKCE through a loopback redirect, Keychain storage, refresh
    api.py      Web API calls with bounded retries and mapped errors
    source.py   SpotifySource: playlists, tracks, metadata, and its audio through the fetch helper
    fetch.py    the fetch helper as a process: librespot decrypts a track into the session temp folder
    consent.py  the listener's own decision about audio, and the warning shown where it is made
    __main__.py python -m live.sources.spotify set-client | login | status | audio | playlists | tracks | track | logout

Spotify offers no licensed way for a third-party app to decode its audio, and its catalog carries no
tempo or key (the audio-features endpoint is closed to new apps), so a track is first listed and
matched by ISRC, then analysed from a 30 s preview (preview.py), and the full track is read only when
it is about to play.

Two things are deliberately narrow. Spotify audio is off until the listener turns it on with
`audio on` (or in the window) after reading what it means: see consent.py. Nothing else unlocks it,
not an environment variable and not a capabilities/spotify.json, which can only narrow the policy
down. And Kev never sees Spotify metadata: Spotify's Developer Policy forbids feeding Spotify Content
into an AI model, so SEND_TO_MODEL is left out unless a capability matrix records a decision
otherwise.
"""
from dataclasses import dataclass

from ..base import AuthExpired, Operation
from ..policy import MATRICES, Policy, PolicyDenied
from .api import Client
from .auth import (ACCOUNTS, API, REDIRECT_URI, KeychainStore, MemoryStore, NotConfigured, OAuth, Session, Tokens,
                   client_id)
from .consent import WARNING, Choice, ConsentStore
from .source import LIKED, PROVIDER, SpotifySource, parse_id, track_info

DEFAULT_OPERATIONS = frozenset({Operation.BROWSE, Operation.METADATA})
# What the listener's own choice adds (Milestone 2). RECORD and PERSIST_AUDIO are never in it.
AUDIO_OPERATIONS = frozenset({Operation.BROWSE, Operation.METADATA, Operation.DECODE, Operation.ANALYZE,
                              Operation.TEMP_AUDIO, Operation.PERSIST_ANALYSIS, Operation.MIX})
# Operations no policy may hold for this provider without the listener's own choice. PERSIST_AUDIO
# and RECORD are never granted at all: decoded audio lives in the session temp folder and nowhere else.
AUDIO_ONLY = frozenset({Operation.DECODE, Operation.ANALYZE, Operation.TEMP_AUDIO, Operation.PERSIST_ANALYSIS,
                        Operation.PERSIST_AUDIO, Operation.MIX, Operation.RECORD})
AUDIO_OFF_REASON = ("Spotify audio is off: it is read through an unofficial client, so the listener turns it on "
                    "themselves (`python -m live.sources.spotify audio on`, or in the window)")


def default_policy(settings=None) -> Policy:
    """Browsing and metadata, and - only when the listener has turned audio on - decoding, analyzing
    and mixing the account's tracks through the session temp folder."""
    choice = (settings or ConsentStore()).read()
    if choice is None or not choice.audio:
        return Policy(PROVIDER, DEFAULT_OPERATIONS,
                      agreement={"reference": "Spotify Web API with the listener's own developer app "
                                              "(Spotify Developer Terms)"},
                      reason=AUDIO_OFF_REASON,
                      notes="browsing and catalog metadata; no audio, and nothing sent to Kev")
    return Policy(PROVIDER, AUDIO_OPERATIONS, agreement={"reference": choice.reference},
                  analysis_on_disk=True, audio_in_session_temp=True,
                  notes="browse, metadata and audio, on the listener's own acknowledgement; nothing recorded")


def _gated(policy: Policy, choice: Choice | None) -> Policy:
    """`policy` with every audio operation taken out unless the listener's stored choice grants them.

    A capability matrix for this provider records no agreement that could override this, so it can
    only narrow the policy down (see consent.py)."""
    if choice is not None and choice.audio:
        return policy
    if not (policy.allowed & AUDIO_ONLY):
        return policy  # nothing to take away
    return Policy(policy.provider, frozenset(policy.allowed - AUDIO_ONLY), agreement=policy.agreement,
                  reason=AUDIO_OFF_REASON, notes=policy.notes)


def load_policy(folder=MATRICES, settings=None) -> Policy:
    """capabilities/spotify.json if there is one, else the default for the listener's stored choice."""
    path = folder / f"{PROVIDER}.json"
    policy = Policy.load(PROVIDER, folder) if path.exists() else default_policy(settings)
    return _gated(policy, (settings or ConsentStore()).read())


def audio_choice(settings=None) -> Choice | None:
    """The listener's stored choice, or None when they have never been asked (which is browse only)."""
    return (settings or ConsentStore()).read()


def set_audio(settings=None, audio: bool = True, when: str | None = None) -> Choice:
    """Record the listener's choice. Called from the window and from `audio on|off`; sign-out clears it."""
    return (settings or ConsentStore()).choose(audio, when)


@dataclass(frozen=True)
class GateStatus:
    authorized: bool     # the policy lets disko browse Spotify
    configured: bool     # a client ID is set (.env, environment or Keychain)
    signed_in: bool
    allowed: tuple       # operations the policy grants
    reason: str
    audio: bool = False  # the listener turned Spotify audio on
    audio_since: str = ""

    @property
    def usable(self) -> bool:
        return self.authorized and self.configured and self.signed_in


def status(policy: Policy | None = None, store=None, env: dict | None = None, settings=None) -> GateStatus:
    choice = (settings or ConsentStore()).read()
    policy = policy or load_policy(settings=settings)
    allowed = tuple(op.name for op in Operation if policy.allows(op))
    on, when = bool(choice and choice.audio), (choice.date if choice else "")
    if not policy.agreement or not policy.allows(Operation.BROWSE):
        return GateStatus(False, False, False, allowed, policy.reason or "browsing Spotify isn't allowed", on, when)
    store = store if store is not None else KeychainStore()
    try:
        client_id(store, env)
    except NotConfigured as e:
        return GateStatus(True, False, False, allowed, str(e), on, when)
    if Tokens.load(store) is None:
        return GateStatus(True, True, False, allowed, "not signed in: run `python -m live.sources.spotify login`",
                          on, when)
    return GateStatus(True, True, True, allowed, "ready", on, when)


def _authorized(policy: Policy) -> Policy:
    if not policy.agreement:
        raise PolicyDenied(PROVIDER, Operation.BROWSE, policy.reason or "browsing Spotify isn't allowed")
    policy.require(Operation.BROWSE)
    return policy


def login(policy: Policy | None = None, store=None, accounts: str = ACCOUNTS, redirect_uri: str = REDIRECT_URI,
          open_browser=None, env: dict | None = None, settings=None) -> Tokens:
    """Sign in through the browser and keep the tokens in the Keychain."""
    _authorized(policy or load_policy(settings=settings))
    store = store if store is not None else KeychainStore()
    oauth = OAuth(client_id(store, env), accounts, redirect_uri)
    tokens = oauth.login(**(dict(open_browser=open_browser) if open_browser else {}))
    tokens.save(store)
    return tokens


def connect(policy: Policy | None = None, store=None, accounts: str = ACCOUNTS, api: str = API,
            redirect_uri: str = REDIRECT_URI, env: dict | None = None, fetcher=None, settings=None,
            **client_options) -> SpotifySource:
    """The signed-in account as a source. A policy that doesn't allow browsing is refused before any
    Keychain or network access. `fetcher` (live/sources/spotify/fetch.py) is only needed to play a
    track, and is only wired in when the policy allows decoding it."""
    policy = _authorized(policy or load_policy(settings=settings))
    store = store if store is not None else KeychainStore()
    session = Session(OAuth(client_id(store, env), accounts, redirect_uri), store)
    if not session.signed_in:
        raise AuthExpired("not signed in to Spotify: run `python -m live.sources.spotify login`")
    return SpotifySource(Client(session, api, **client_options), policy, session, fetcher=fetcher)


def logout(store=None, settings=None):
    """Forget the sign-in here, and the audio choice with it (a forgotten account leaves no switch on).
    The client ID stays; Spotify has no revocation endpoint, so access is removed for good at
    https://www.spotify.com/account/apps/."""
    store = store if store is not None else KeychainStore()
    store.delete("tokens")
    (settings or ConsentStore()).clear()


__all__ = ["AUDIO_OFF_REASON", "AUDIO_OPERATIONS", "AUDIO_ONLY", "Choice", "Client", "ConsentStore",
           "DEFAULT_OPERATIONS", "GateStatus", "KeychainStore", "LIKED", "MemoryStore", "NotConfigured", "OAuth",
           "PROVIDER", "Session", "SpotifySource", "Tokens", "WARNING", "audio_choice", "client_id", "connect",
           "default_policy", "load_policy", "login", "logout", "parse_id", "set_audio", "status", "track_info"]
