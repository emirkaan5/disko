"""Beatport: sign in with disko's own client ID, browse playlists and charts, read catalog metadata.

    auth.py     OAuth2 (authorization code + PKCE, loopback redirect), Keychain storage, refresh
    api.py      v4 REST calls with bounded retries and mapped errors
    source.py   BeatportSource: playlists, tracks and metadata as a Source
    __main__.py python -m live.sources.beatport set-client | login | status | playlists | tracks | track | logout

Two things stand between this code and a working connection, checked in this order:

1. disko's own client ID, from .env / the environment or the Keychain (auth.py). The docs
   page's own client ID is refused unless DISKO_BEATPORT_ALLOW_BORROWED_CLIENT is set; that
   fallback is only to try the system before a real client ID arrives.
2. A signed-in account (`login`). With a borrowed client ID there's no loopback redirect to
   receive the code, so `login --no-browser` uses the docs client's own post-message page and
   asks you to paste the code back (see auth.py).

What disko may do with Beatport tracks depends on the plan: browsing, catalog metadata and passing
that metadata to Kev (DEFAULT_OPERATIONS) always; reading, decoding and analyzing the stream
(STREAMING_OPERATIONS) only when the listener asserts a streaming plan with
DISKO_BEATPORT_STREAMING=1. A capabilities/beatport.json file, if present, replaces the default (to
restrict it, say); a malformed one denies everything. Audio, when permitted, is decoded in memory:
see source.py.
"""
from dataclasses import dataclass

from ..base import AuthExpired, Operation
from ..policy import MATRICES, Policy, PolicyDenied
from .auth import (API, REDIRECT_URI, Credentials, KeychainStore, NotConfigured, OAuth, Session, Tokens,
                   borrowed, docs_redirect_uri, parse_code, streaming_enabled)
from .api import Client
from .source import PROVIDER, BeatportSource, track_info

ADAPTER_OPERATIONS = frozenset({Operation.BROWSE, Operation.METADATA, Operation.SEND_TO_MODEL})  # what's built
STREAMING_OPERATIONS = frozenset(ADAPTER_OPERATIONS | {Operation.DECODE, Operation.ANALYZE, Operation.MIX})
DEFAULT_OPERATIONS = ADAPTER_OPERATIONS


def default_policy() -> Policy:
    """Browsing, and — only when the listener has asserted a streaming plan (DISKO_BEATPORT_STREAMING=1)
    — decoding, analyzing and mixing the stream on this Mac. No audio without that assertion."""
    if streaming_enabled():
        return Policy(PROVIDER, STREAMING_OPERATIONS,
                      agreement={"reference": "listener's Beatport Streaming plan (DISKO_BEATPORT_STREAMING=1)"},
                      notes="streaming playback: read, decode and analyze the account's stream on this Mac")
    return Policy(PROVIDER, DEFAULT_OPERATIONS,
                  agreement={"reference": "built in: Beatport's API with the listener's own client ID"},
                  notes="browsing and catalog metadata; no audio (set DISKO_BEATPORT_STREAMING=1 for playback)")


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


def login(policy: Policy | None = None, store=None, api: str = API, redirect_uri: str | None = None,
          open_browser=None, manual: bool | None = None, prompt=None) -> Tokens:
    """Sign in and keep the tokens in the Keychain.

    `redirect_uri` defaults to a loopback callback for disko's own client ID, or to the docs client's
    own post-message page when the client ID is borrowed (that client registers no loopback), in
    which case the sign-in is manual: the link is shown and the code is read back from the listener.
    """
    policy = _authorized(policy or load_policy())
    store = store if store is not None else KeychainStore()
    credentials = Credentials.load(store)
    is_borrowed = borrowed(credentials.client_id)
    if redirect_uri is None:
        redirect_uri = docs_redirect_uri(api) if is_borrowed else REDIRECT_URI
    oauth = OAuth(credentials, api, redirect_uri)
    options = {}
    if open_browser:
        options["open_browser"] = open_browser
    if manual is None and is_borrowed:
        manual = True  # the docs client can't come back to a loopback; the listener pastes the code
    if manual is not None:
        options["manual"] = manual
    if prompt is not None:
        options["prompt"] = prompt
    tokens = oauth.login(**options)
    tokens.save(store)
    return tokens


def connect(policy: Policy | None = None, store=None, api: str = API, redirect_uri: str | None = None,
            **client_options) -> BeatportSource:
    """The signed-in account as a source. A policy that doesn't allow browsing is refused before any
    Keychain or network access."""
    policy = _authorized(policy or load_policy())
    store = store if store is not None else KeychainStore()
    credentials = Credentials.load(store)
    if redirect_uri is None:
        redirect_uri = docs_redirect_uri(api) if borrowed(credentials.client_id) else REDIRECT_URI
    session = Session(OAuth(credentials, api, redirect_uri), store)
    if not session.signed_in:
        raise AuthExpired("not signed in to Beatport: run `python -m live.sources.beatport login`")
    return BeatportSource(Client(session, api, **client_options), policy, session)


def logout(store=None, api: str = API, redirect_uri: str | None = None):
    """Revoke the tokens at Beatport (best effort) and forget them here. The client ID stays."""
    store = store if store is not None else KeychainStore()
    try:
        creds = Credentials.load(store)
    except NotConfigured:
        store.delete("tokens")
        return
    redirect_uri = redirect_uri or (docs_redirect_uri(api) if borrowed(creds.client_id) else REDIRECT_URI)
    Session(OAuth(creds, api, redirect_uri), store).logout()


__all__ = ["ADAPTER_OPERATIONS", "BeatportSource", "Client", "Credentials", "DEFAULT_OPERATIONS", "GateStatus",
           "KeychainStore", "NotConfigured", "OAuth", "PROVIDER", "STREAMING_OPERATIONS", "Session", "Tokens",
           "borrowed", "connect", "default_policy", "docs_redirect_uri", "load_policy", "login", "logout",
           "parse_code", "status", "streaming_enabled", "track_info"]
