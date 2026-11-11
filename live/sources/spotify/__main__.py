"""Set up and check the Spotify connection from a terminal.

    python -m live.sources.spotify set-client       # store your Spotify app's client ID in the Keychain,
                                                    # unless DISKO_SPOTIFY_CLIENT_ID is in .env
    python -m live.sources.spotify login            # sign in through the browser
    python -m live.sources.spotify status
    python -m live.sources.spotify audio [on|off|show]   # the listener's own choice about Spotify audio
    python -m live.sources.spotify playlists
    python -m live.sources.spotify tracks liked | PLAYLIST_ID_OR_LINK [--limit 50]
    python -m live.sources.spotify track ID [--raw]
    python -m live.sources.spotify logout | forget-client

`audio on` prints the warning and needs an acknowledgement: typing 'on', or --yes when it is being
done from a script. Nothing else unlocks Spotify audio. The choice is a file of its own
(~/.disko/spotify/consent.json) that `logout` clears.

--accounts and --api (or DISKO_SPOTIFY_ACCOUNTS / DISKO_SPOTIFY_API) point it elsewhere, such as the
fake server in tests/fake_spotify.py.
"""
import argparse
import getpass
import json
import sys
import webbrowser

from ..base import SourceError, TrackId
from ..policy import MATRICES
from .auth import ENV_CLIENT_ID, ENV_FILE, REDIRECT_URI
from . import (ACCOUNTS, API, PROVIDER, WARNING, ConsentStore, KeychainStore, audio_choice, connect, login, logout,
               parse_id, set_audio, status)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m live.sources.spotify", description=__doc__.split("\n\n")[0])
    ap.add_argument("--accounts", default=ACCOUNTS, help=f"accounts service root (default {ACCOUNTS})")
    ap.add_argument("--api", default=API, help=f"Web API root (default {API})")
    ap.add_argument("--redirect", default=REDIRECT_URI, help=f"registered redirect URI (default {REDIRECT_URI})")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("set-client", help="store your Spotify app's client ID in the Keychain")
    sub.add_parser("forget-client", help="remove the client ID and sign-in from the Keychain")
    login_p = sub.add_parser("login", help="sign in through the browser")
    login_p.add_argument("--no-browser", action="store_true", help="print the sign-in link instead of opening it")
    sub.add_parser("logout", help="forget the sign-in and the audio choice with it")
    sub.add_parser("status", help="what's set up and what disko may do with Spotify tracks")
    audio_p = sub.add_parser("audio", help="the listener's own choice about reading Spotify's audio")
    audio_p.add_argument("setting", nargs="?", choices=["on", "off", "show"], default="show")
    audio_p.add_argument("--yes", action="store_true",
                         help="accept the warning without typing 'on' (for a script, not for a person)")
    sub.add_parser("playlists", help="the account's playlists")
    tracks_p = sub.add_parser("tracks", help="the tracks of a playlist (or `liked`)")
    tracks_p.add_argument("playlist")
    tracks_p.add_argument("--limit", type=int, default=50)
    track_p = sub.add_parser("track", help="one catalog track")
    track_p.add_argument("id")
    track_p.add_argument("--raw", action="store_true", help="print Spotify's own JSON (to check the field mapping)")
    args = ap.parse_args(argv)
    store = KeychainStore()
    settings = ConsentStore()
    where = {"accounts": args.accounts, "redirect_uri": args.redirect, "settings": settings}

    try:
        if args.command == "set-client":
            print(f"Create an app at https://developer.spotify.com/dashboard (Web API) and add the redirect URI "
                  f"{args.redirect}.")
            value = getpass.getpass("Client ID (hidden): ").strip()
            if not value:
                print("Nothing saved.")
                return 1
            store.set("client_id", value)
            print("Saved in the Keychain (service disko.spotify). Next: python -m live.sources.spotify login")
        elif args.command == "forget-client":
            for name in ("tokens", "client_id"):
                store.delete(name)
            settings.clear()
            print("Removed the Spotify client ID, the sign-in and the audio choice from this machine.")
        elif args.command == "login":
            def opener(url):
                if args.no_browser:
                    print(f"Open this link in a browser and approve disko:\n{url}", flush=True)
                else:
                    webbrowser.open(url)
            login(store=store, open_browser=opener, **where)
            print("Signed in.")
        elif args.command == "logout":
            logout(store, settings)
            print("Signed out here, and the audio choice is cleared with it. "
                  "To remove disko's access for good: https://www.spotify.com/account/apps/")
        elif args.command == "audio":
            current = audio_choice(settings)
            if args.setting == "show":
                print(current.summary if current else
                      "Spotify audio has never been turned on here: browsing only.")
                if current and current.audio:
                    print(f"agreement: {current.reference}")
            elif args.setting == "off":
                set_audio(settings, audio=False)
                print("Spotify audio is off. Browsing is unchanged.")
            else:
                print(WARNING)
                if not args.yes:
                    answer = input("Type 'on' to accept all of that and let disko read Spotify audio: ")
                    if answer.strip().lower() != "on":
                        print("Nothing changed: Spotify audio stays off.")
                        return 1
                choice = set_audio(settings, audio=True)
                print(f"Spotify audio is on. {choice.reference}")
        elif args.command == "status":
            gate = status(store=store, settings=settings)
            matrix = MATRICES / f"{PROVIDER}.json"
            print(f"permissions:  {matrix if matrix.exists() else 'built-in default'}")
            print(f"client ID:    {'set' if gate.configured else 'no' if gate.authorized else 'not checked'}"
                  f"{'' if gate.configured or not gate.authorized else f' (put {ENV_CLIENT_ID} in {ENV_FILE})'}")
            print(f"signed in:    {'yes' if gate.signed_in else 'no' if gate.configured else 'not checked'}")
            print(f"audio:        {'on since ' + gate.audio_since if gate.audio else 'off (browsing only)'}")
            if gate.audio:
                print(f"audio helper: {'ready' if gate.audio_ready else 'still to do: ' + gate.audio_missing}")
            print(f"allowed:      {', '.join(gate.allowed) or 'nothing'}")
            print(f"status:       {gate.reason}")
            if gate.usable:
                print(f"account:      {connect(store=store, api=args.api, **where).account().get('name') or 'signed in'}")
        elif args.command == "playlists":
            for p in connect(store=store, api=args.api, **where).playlists():
                count = "" if p.track_count is None else f"  ({p.track_count} tracks)"
                print(f"{p.id:<24} {p.name}{count}")
        elif args.command == "tracks":
            source = connect(store=store, api=args.api, **where)
            playlist = args.playlist if args.playlist == "liked" else parse_id(args.playlist, "playlist")
            page = source.tracks(playlist, 0, max(1, min(args.limit, 500)))
            for info in page.items:
                isrc = info.isrc or "no ISRC"
                print(f"{info.id!s:<32} {isrc:<12}  {info.availability.value:<18} {info.artist} – {info.title}")
            shown = f"{len(page.items)} of {page.total}" if page.total is not None else str(len(page.items))
            print(f"{shown} tracks")
        elif args.command == "track":
            source = connect(store=store, api=args.api, **where)
            tid = TrackId(PROVIDER, parse_id(args.id.removeprefix("spotify:track:").removeprefix("spotify:"), "track"))
            if args.raw:
                print(json.dumps(source.raw_track(tid), indent=2))
            else:
                info = source.info(tid)
                print(json.dumps({"id": str(info.id), "title": info.title, "artist": info.artist,
                                  "duration": info.duration, "isrc": info.isrc,
                                  "availability": info.availability.value}, indent=2))
    except SourceError as e:
        message = str(e)
        print(message if message.startswith("spotify:") else f"spotify: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
