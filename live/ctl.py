"""Control a running disko from the terminal (the window app serves control unless started with
--no-control; --headless always does). The local socket needs no token: disko checks that you are
the user running it. A TCP address (--url) needs the token from ~/.disko/control.token.

    .venv-live/bin/python -m live.ctl status
    .venv-live/bin/python -m live.ctl start ["part of a title"]
    .venv-live/bin/python -m live.ctl energy up|hold|down
    .venv-live/bin/python -m live.ctl pause | resume | stop | pick-again
    .venv-live/bin/python -m live.ctl tracks [query]
"""
import argparse
import json
import sys
from urllib.parse import urlencode

from .control import Client, ControlUnavailable


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "start", "pause", "resume", "stop", "pick-again", "energy", "tracks"])
    ap.add_argument("arg", nargs="?", help="start: a track; energy: up/hold/down; tracks: a search")
    ap.add_argument("--url", help="a TCP control address (http://host:port) instead of the local socket")
    ap.add_argument("--token", help="the token for --url (default: read from ~/.disko/control.token); "
                                    "not used on the local socket")
    args = ap.parse_args(argv)
    client = Client(url=args.url, token=args.token)
    if args.command == "status":
        call = ("GET", "/status", None)
    elif args.command == "tracks":
        call = ("GET", "/tracks?" + urlencode({"q": args.arg or "", "limit": 20}), None)
    elif args.command == "start":
        call = ("POST", "/start", {"track": args.arg})
    elif args.command == "energy":
        if not args.arg:
            ap.error("energy needs up, hold or down")
        call = ("POST", "/energy", {"energy": args.arg})
    else:
        call = ("POST", f"/{args.command}", {})
    try:
        code, payload = client.request(*call)
    except ControlUnavailable as e:
        print(e, file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
