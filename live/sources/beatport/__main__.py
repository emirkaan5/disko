"""Set up and check the Beatport connection from a terminal.

    python -m live.sources.beatport set-client        # store the client ID (and secret) in the Keychain,
                                                      # unless they're in .env (see .env.example)
    python -m live.sources.beatport login             # sign in through the browser
    python -m live.sources.beatport login --no-browser  # print the link and paste the code back
    python -m live.sources.beatport status
    python -m live.sources.beatport playlists
    python -m live.sources.beatport tracks my:123 [--limit 50]
    python -m live.sources.beatport track 17606729 [--raw]
    python -m live.sources.beatport logout | forget-client

--api and --redirect (or DISKO_BEATPORT_API / DISKO_BEATPORT_REDIRECT) point it elsewhere, such as
the fake server in tests/fake_beatport.py. Without --redirect, a borrowed docs client ID (see .env)
uses the docs client's own post-message page and asks for the code, because that client registers no
loopback callback this Mac could listen on.
"""
import argparse
import getpass
import json
import sys
import webbrowser

from ..base import SourceError, TrackId
from ..pool import candidate_pool
from ..policy import MATRICES
from .auth import ENV_ALLOW_BORROWED, ENV_CLIENT_ID, ENV_FILE, _truthy, env_values
from . import (ADAPTER_OPERATIONS, API, STREAMING_OPERATIONS, Credentials, KeychainStore, connect,
               docs_redirect_uri, login, logout, status, streaming_enabled)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m live.sources.beatport", description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", default=API, help=f"API root (default {API})")
    ap.add_argument("--redirect", default=None,
                    help="registered redirect URI; by default a loopback callback for disko's own client ID, "
                         "or the docs client's own post-message page when the client ID is borrowed")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("set-client", help="store disko's client ID (and secret) in the Keychain")
    sub.add_parser("forget-client", help="remove the client ID, secret and tokens from the Keychain")
    login_p = sub.add_parser("login", help="sign in through the browser")
    login_p.add_argument("--no-browser", action="store_true", help="print the sign-in link instead of opening it")
    login_p.add_argument("--manual", action="store_true",
                         help="don't listen for a local callback; paste the code from the redirect back")
    sub.add_parser("logout", help="revoke and forget the sign-in")
    sub.add_parser("status", help="what's set up and what the capability matrix allows")
    sub.add_parser("playlists", help="the account's playlists")
    tracks_p = sub.add_parser("tracks", help="the tracks of my:ID, catalog:ID or chart:ID")
    tracks_p.add_argument("playlist")
    tracks_p.add_argument("--limit", type=int, default=50)
    track_p = sub.add_parser("track", help="one catalog track")
    track_p.add_argument("id")
    track_p.add_argument("--raw", action="store_true", help="print Beatport's own JSON (to check the field mapping)")
    args = ap.parse_args(argv)
    store = KeychainStore()
    where = {"api": args.api, "redirect_uri": args.redirect}

    try:
        if args.command == "set-client":
            client_id = getpass.getpass("Client ID (hidden): ").strip()
            secret = getpass.getpass("Client secret, if Beatport gave one (hidden, Enter for none): ").strip()
            Credentials(client_id, secret or None).save(
                store, allow_borrowed=_truthy(env_values().get(ENV_ALLOW_BORROWED)))
            print("Saved in the Keychain (service disko.beatport). Next: python -m live.sources.beatport login")
            if env_values().get(ENV_CLIENT_ID):
                print(f"Note: {ENV_CLIENT_ID} is set in {ENV_FILE} or the environment, and that one is used instead.")
        elif args.command == "forget-client":
            for name in ("tokens", "client_secret", "client_id"):
                store.delete(name)
            print("Removed disko's Beatport client ID, secret and sign-in from the Keychain.")
        elif args.command == "login":
            manual = args.manual or None  # None lets the OAuth flow pick: manual for a non-loopback redirect

            def opener(url):  # called only once the gate and the client ID check out
                if args.no_browser:
                    print("Open this link in a browser signed in to Beatport and approve disko:\n"
                          f"{url}", flush=True)
                else:
                    webbrowser.open(url)

            def prompt(message: str) -> str:
                print("After you approve, the browser lands on Beatport's post-message page and bounces on "
                      "again, so the code isn't left in the address bar.\n"
                      "To get it: open Developer Tools (F12) > Network and tick 'Preserve log', then load "
                      "the sign-in link again and copy the 'code' value from the request to "
                      "/auth/o/post-message/.", flush=True)
                return input(message)

            login(store=store, open_browser=opener, manual=manual, prompt=prompt, **where)
            print("Signed in.")
        elif args.command == "logout":
            logout(store=store, **where)
            print("Signed out.")
        elif args.command == "status":
            gate = status(store=store)
            matrix = MATRICES / "beatport.json"
            print(f"permissions:            {matrix if matrix.exists() else 'built-in default'}")
            origin = Credentials.load(store).origin if gate.configured else None
            print(f"client ID:              {'from the ' + origin if origin else 'no' if gate.authorized else 'not checked'}")
            print(f"signed in:              {'yes' if gate.signed_in else 'no' if gate.configured else 'not checked'}")
            print(f"allowed:                {', '.join(gate.allowed) or 'nothing'}")
            built = STREAMING_OPERATIONS if streaming_enabled() else ADAPTER_OPERATIONS
            print(f"built here:             {', '.join(sorted(op.name for op in built))}")
            print(f"status:                 {gate.reason}")
            if gate.usable:
                user = connect(store=store, **where).account().get("username")
                print(f"account:                {user or 'signed in'}")
        elif args.command == "playlists":
            for p in connect(store=store, **where).playlists():
                count = "" if p.track_count is None else f"  ({p.track_count} tracks)"
                print(f"{p.id:<14} {p.name}{count}")
        elif args.command == "tracks":
            pool = candidate_pool(connect(store=store, **where), args.playlist, limit=args.limit)
            for t in pool.tracks:
                print(f"{t.id!s:<20} {t.bpm:6.1f}  {t.camelot:>3}  {t.artist} – {t.title}")
            for info, why in pool.skipped:
                print(f"{info.id!s:<20} left out: {why}  ({info.title})")
            print(pool.summary())
        elif args.command == "track":
            source = connect(store=store, **where)
            tid = TrackId("beatport", args.id.removeprefix("beatport:"))
            if args.raw:
                print(json.dumps(source.raw_track(tid), indent=2))
            else:
                info = source.info(tid)
                print(json.dumps({"id": str(info.id), "title": info.title, "artist": info.artist,
                                  "duration": info.duration, "availability": info.availability.value,
                                  "genres": list(info.genres), "features": info.features}, indent=2))
    except SourceError as e:
        message = str(e)
        print(message if message.startswith("beatport:") else f"beatport: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
