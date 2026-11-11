"""Spotify: sign in with the listener's own Spotify developer app, browse playlists and Liked Songs,
read catalog metadata (SPOTIFY_INTEGRATION_PLAN.md, Milestone 1).

    auth.py     OAuth2 authorization code + PKCE through a loopback redirect, Keychain storage, refresh
    api.py      Web API calls with bounded retries and mapped errors
    source.py   SpotifySource: playlists, tracks and metadata as a Source
    __main__.py python -m live.sources.spotify set-client | login | status | playlists | tracks | track | logout

Browsing and metadata only. Spotify offers no licensed way for a third-party app to decode its audio,
and its catalog carries no tempo or key, so a Spotify track can be listed and matched (by ISRC) but
not analyzed or mixed here.

Kev doesn't see Spotify metadata by default: Spotify's Developer Policy forbids feeding Spotify
Content into an AI model, so SEND_TO_MODEL is left out. A capabilities/spotify.json file, if present,
replaces the default policy; a malformed one denies everything.
"""
from dataclasses import dataclass

from ..base import AuthExpired, Operation
from ..policy import MATRICES, Policy, PolicyDenied
from .api import Client
from .auth import (ACCOUNTS, API, REDIRECT_URI, KeychainStore, MemoryStore, NotConfigured, OAuth, Session, Tokens,
                   client_id)
from .source import LIKED, PROVIDER, SpotifySource, parse_id, track_info

DEFAULT_OPERATIONS = frozenset({Operation.BROWSE, Operation.METADATA})


def default_policy() -> Policy:
    return Policy(PROVIDER, DEFAULT_OPERATIONS,
                  agreement={"reference": "Spotify Web API with the listener's own developer app "
                                          "(Spotify Developer Terms)"},
                  notes="browsing and catalog metadata; no audio, and nothing sent to Kev")


def load_policy(folder=MATRICES) -> Policy:
    """capabilities/spotify.json if there is one, else the built-in default."""
    path = folder / f"{PROVIDER}.json"
    return Policy.load(PROVIDER, folder) if path.exists() else default_policy()


@dataclass(frozen=True)
class GateStatus:
    authorized: bool     # the policy lets disko browse Spotify
    configured: bool     # a client ID is set (.env, environment or Keychain)
    signed_in: bool
    allowed: tuple       # operations the policy grants
    reason: str

    @property
    def usable(self) -> bool:
        return self.authorized and self.configured and self.signed_in


def status(policy: Policy | None = None, store=None, env: dict | None = None) -> GateStatus:
    policy = policy or load_policy()
    allowed = tuple(op.name for op in Operation if policy.allows(op))
    if not policy.agreement or not policy.allows(Operation.BROWSE):
        return GateStatus(False, False, False, allowed, policy.reason or "browsing Spotify isn't allowed")
    store = store if store is not None else KeychainStore()
    try:
        client_id(store, env)
    except NotConfigured as e:
        return GateStatus(True, False, False, allowed, str(e))
    if Tokens.load(store) is None:
        return GateStatus(True, True, False, allowed, "not signed in: run `python -m live.sources.spotify login`")
    return GateStatus(True, True, True, allowed, "ready")


def _authorized(policy: Policy) -> Policy:
    if not policy.agreement:
        raise PolicyDenied(PROVIDER, Operation.BROWSE, policy.reason or "browsing Spotify isn't allowed")
    policy.require(Operation.BROWSE)
    return policy


def login(policy: Policy | None = None, store=None, accounts: str = ACCOUNTS, redirect_uri: str = REDIRECT_URI,
          open_browser=None, env: dict | None = None) -> Tokens:
    """Sign in through the browser and keep the tokens in the Keychain."""
    _authorized(policy or load_policy())
    store = store if store is not None else KeychainStore()
    oauth = OAuth(client_id(store, env), accounts, redirect_uri)
    tokens = oauth.login(**({"open_browser": open_browser} if open_browser else {}))
    tokens.save(store)
    return tokens


def connect(policy: Policy | None = None, store=None, accounts: str = ACCOUNTS, api: str = API,
            redirect_uri: str = REDIRECT_URI, env: dict | None = None, **client_options) -> SpotifySource:
    """The signed-in account as a source. A policy that doesn't allow browsing is refused before any
    Keychain or network access."""
    policy = _authorized(policy or load_policy())
    store = store if store is not None else KeychainStore()
    session = Session(OAuth(client_id(store, env), accounts, redirect_uri), store)
    if not session.signed_in:
        raise AuthExpired("not signed in to Spotify: run `python -m live.sources.spotify login`")
    return SpotifySource(Client(session, api, **client_options), policy, session)


def logout(store=None):
    """Forget the sign-in here. The client ID stays; Spotify has no revocation endpoint, so access
    is removed for good at https://www.spotify.com/account/apps/."""
    store = store if store is not None else KeychainStore()
    store.delete("tokens")


__all__ = ["Client", "DEFAULT_OPERATIONS", "GateStatus", "KeychainStore", "LIKED", "MemoryStore", "NotConfigured",
           "OAuth", "PROVIDER", "Session", "SpotifySource", "Tokens", "client_id", "connect", "default_policy",
           "load_policy", "login", "logout", "parse_id", "status", "track_info"]
