"""Beatport: sign in with disko's own client ID, browse playlists and charts, read catalog metadata.

    auth.py     OAuth2 (authorization code + PKCE, loopback redirect), Keychain storage, refresh
    api.py      v4 REST calls with bounded retries and mapped errors
    source.py   BeatportSource: playlists, tracks and metadata as a Source
    __main__.py python -m live.sources.beatport set-client | login | status | playlists | tracks | track | logout

Two things stand between this code and a working connection, checked in this order:

1. disko's own client ID, from .env / the environment or the Keychain (auth.py). The docs
   page's client ID is refused.
2. A signed-in account (`login`).

What disko may do with Beatport tracks is built in: browsing, catalog metadata, and passing that
metadata to Kev on this Mac (DEFAULT_OPERATIONS). A capabilities/beatport.json file, if present,
replaces that default (to restrict it, say); a malformed one denies everything.

Audio is not part of this: see source.py.
"""
from dataclasses import dataclass

from ..base import AuthExpired, Operation
from ..policy import MATRICES, Policy, PolicyDenied
from .auth import API, REDIRECT_URI, Credentials, KeychainStore, NotConfigured, OAuth, Session, Tokens
from .api import Client
from .source import PROVIDER, BeatportSource, track_info

ADAPTER_OPERATIONS = frozenset({Operation.BROWSE, Operation.METADATA, Operation.SEND_TO_MODEL})  # what's built
DEFAULT_OPERATIONS = ADAPTER_OPERATIONS


def default_policy() -> Policy:
    return Policy(PROVIDER, DEFAULT_OPERATIONS,
                  agreement={"reference": "built in: Beatport's API with the listener's own client ID"},
                  notes="browsing and catalog metadata; no audio")


def load_policy(folder=MATRICES) -> Policy:
    """capabilities/beatport.json if there is one, else the built-in default."""
    path = folder / f"{PROVIDER}.json"
    return Policy.load(PROVIDER, folder) if path.exists() else default_policy()


@dataclass(frozen=True)
class GateStatus:
    authorized: bool     # the policy lets disko browse Beatport
    configured: bool     # disko's client ID is set (.env, environment or Keychain)
    signed_in: bool      # an account is signed in
    allowed: tuple       # operations the policy grants
    reason: str

    @property
    def usable(self) -> bool:
        return self.authorized and self.configured and self.signed_in


def status(policy: Policy | None = None, store=None) -> GateStatus:
    policy = policy or load_policy()
    allowed = tuple(op.name for op in Operation if policy.allows(op))
    if not policy.agreement or not policy.allows(Operation.BROWSE):
        return GateStatus(False, False, False, allowed, policy.reason or "browsing Beatport isn't allowed")
    store = store if store is not None else KeychainStore()
    try:
        Credentials.load(store)
    except NotConfigured as e:
        return GateStatus(True, False, False, allowed, str(e))
    if Tokens.load(store) is None:
        return GateStatus(True, True, False, allowed, "not signed in: run `python -m live.sources.beatport login`")
    return GateStatus(True, True, True, allowed, "ready")


def _authorized(policy: Policy) -> Policy:
    if not policy.agreement:
        raise PolicyDenied(PROVIDER, Operation.BROWSE, policy.reason or "browsing Beatport isn't allowed")
    policy.require(Operation.BROWSE)
    return policy


def login(policy: Policy | None = None, store=None, api: str = API, redirect_uri: str = REDIRECT_URI,
          open_browser=None) -> Tokens:
    """Sign in through the browser and keep the tokens in the Keychain."""
    policy = _authorized(policy or load_policy())
    store = store if store is not None else KeychainStore()
    oauth = OAuth(Credentials.load(store), api, redirect_uri)
    tokens = oauth.login(**({"open_browser": open_browser} if open_browser else {}))
    tokens.save(store)
    return tokens


def connect(policy: Policy | None = None, store=None, api: str = API, redirect_uri: str = REDIRECT_URI,
            **client_options) -> BeatportSource:
    """The signed-in account as a source. A policy that doesn't allow browsing is refused before any
    Keychain or network access."""
    policy = _authorized(policy or load_policy())
    store = store if store is not None else KeychainStore()
    session = Session(OAuth(Credentials.load(store), api, redirect_uri), store)
    if not session.signed_in:
        raise AuthExpired("not signed in to Beatport: run `python -m live.sources.beatport login`")
    return BeatportSource(Client(session, api, **client_options), policy, session)


def logout(store=None, api: str = API, redirect_uri: str = REDIRECT_URI):
    """Revoke the tokens at Beatport (best effort) and forget them here. The client ID stays."""
    store = store if store is not None else KeychainStore()
    try:
        creds = Credentials.load(store)
    except NotConfigured:
        store.delete("tokens")
        return
    Session(OAuth(creds, api, redirect_uri), store).logout()


__all__ = ["ADAPTER_OPERATIONS", "BeatportSource", "Client", "Credentials", "DEFAULT_OPERATIONS", "GateStatus",
           "KeychainStore", "NotConfigured", "OAuth", "PROVIDER", "Session", "Tokens", "connect", "default_policy",
           "load_policy", "login", "logout", "status", "track_info"]
