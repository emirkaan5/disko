#!/bin/bash
# Set up disko in this checkout: the three venvs, Kev, and the Essentia models. Safe to run again:
# anything already in place is left alone.
#
#   scripts/setup.sh                 set up whatever is missing
#   scripts/setup.sh --dry-run       print what it would do; change nothing
#   scripts/setup.sh --upgrade       also re-run `uv pip install -r` on existing venvs
#   scripts/setup.sh --kev-weights   also download Kev's weights (several GB, once)
#   scripts/setup.sh --spotify-audio also build the Spotify fetch helper's venv, which reads Spotify
#                                    audio through librespot: against Spotify's terms, so it is only
#                                    built when asked for (see requirements/spotify.txt)
#
# Works on macOS's stock bash 3.2.
set -eu

REPO=$(cd -P "$(dirname "$0")/.." && pwd)
cd "$REPO"

DRY=0
UPGRADE=0
WEIGHTS=0
SPOTIFY=0
for arg in "$@"; do
    case $arg in
        --dry-run|-n) DRY=1 ;;
        --upgrade) UPGRADE=1 ;;
        --kev-weights) WEIGHTS=1 ;;
        --spotify-audio) SPOTIFY=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "setup.sh: unknown option $arg (try --help)" >&2; exit 2 ;;
    esac
done

PYTHON_VERSION=3.14
KEV_REPO=https://github.com/jaredpalmer/kev.git
KEV_RUN=${KEV_RUN:-jaredpalmer/kev-4b}
MODELS_DIR=data/house/models/essentia  # where live/workers/fast.py reads them (MODELS)
MODELS_URL=https://essentia.upf.edu/models
# The fast pass's models (fast.py: EFFNET, MUSICNN, HEADS), as paths under MODELS_URL, without
# the extension: each has a .pb graph and a .json description.
MODELS="feature-extractors/discogs-effnet/discogs-effnet-bs64-1
feature-extractors/musicnn/msd-musicnn-1
classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1
classification-heads/danceability/danceability-discogs-effnet-1
classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1
classification-heads/emomusic/emomusic-msd-musicnn-2"

step() { printf '\n== %s\n' "$*"; }
say() { printf '   %s\n' "$*"; }
# Run a command, or under --dry-run just show it.
run() {
    if [ "$DRY" = 1 ]; then
        printf '   would run:'
        printf ' %q' "$@"
        printf '\n'
    else
        printf '   $'
        printf ' %q' "$@"
        printf '\n'
        "$@"
    fi
}

[ "$DRY" = 1 ] && echo "Dry run: nothing will be changed."

# --- the machine ------------------------------------------------------------------------------

step "Checking the machine"
if [ "$(uname -s)" != Darwin ] || [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != 1 ]; then
    say "Sorry: disko needs an Apple Silicon Mac. allin1 (the deep pass) and Kev both run on MLX,"
    say "which only exists there. This machine is $(uname -s) $(uname -m)."
    exit 1
fi
say "Apple Silicon, macOS $(sw_vers -productVersion)"

UV=$(command -v uv 2>/dev/null || true)
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -z "$UV" ]; then
    say "disko needs uv (https://docs.astral.sh/uv/) to build its Python environments and to run Kev."
    say "Install it, then run this again:"
    say "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    say "(or: brew install uv)"
    exit 1
fi
say "uv: $UV ($("$UV" --version | awk '{print $2}'))"

# --- the venvs --------------------------------------------------------------------------------

# One venv per job: essentia-tensorflow clashes with plain essentia.
VENVS="live:.venv-live:the app fast:.venv-essentia-tf:the fast pass audio:.venv-audio:the deep pass"
# The Spotify fetch helper's venv reads the account's audio through librespot, which is against
# Spotify's terms of service, so it is only built when the listener asks for it. Without it a Spotify
# playlist can be browsed and paused over but not played.
if [ "$SPOTIFY" = 1 ]; then
    VENVS="$VENVS spotify:.venv-spotify:the Spotify fetch helper (librespot)"
    if ! command -v ffmpeg >/dev/null 2>&1; then
        say "Note: the fetch helper decodes a fetched track with ffmpeg, which isn't on PATH."
        say "      Install it with \`brew install ffmpeg\`, or a fetch will fail when a track is picked."
    fi
fi
for spec in $VENVS; do
    name=${spec%%:*}
    rest=${spec#*:}
    venv=${rest%%:*}
    purpose=${rest#*:}
    req="requirements/$name.txt"
    step "$venv ($purpose, $req)"
    if [ -x "$venv/bin/python" ]; then
        if [ "$UPGRADE" = 1 ]; then
            run "$UV" pip install --python "$venv" -r "$req"
        else
            say "present, left as is (--upgrade re-installs $req into it)"
        fi
    else
        if [ -e "$venv" ]; then
            say "$venv exists but has no working python. It is not deleted automatically:"
            say "move it aside (mv $venv $venv.broken) and run this again."
            exit 1
        fi
        run "$UV" venv "$venv" --python "$PYTHON_VERSION"
        run "$UV" pip install --python "$venv" -r "$req"
    fi
done

# --- Kev --------------------------------------------------------------------------------------

step "Kev (external/kev)"
if [ -f external/kev/pyproject.toml ]; then
    say "present"
elif [ -e external/kev ]; then
    say "external/kev exists but isn't a Kev checkout; move it aside and run this again."
    exit 1
else
    run mkdir -p external
    run git clone "$KEV_REPO" external/kev
fi
if [ -x external/kev/.venv/bin/python ]; then
    say "its environment (external/kev/.venv): present"
else
    say "its environment (external/kev/.venv), which the app's \`uv run\` would otherwise build on first start:"
    if [ "$DRY" = 1 ]; then
        say "would run: (cd external/kev && $UV sync --extra serve)"
    else
        (cd external/kev && run "$UV" sync --extra serve)
    fi
fi

# --- Essentia models --------------------------------------------------------------------------

step "Essentia models ($MODELS_DIR)"
need=""
for path in $MODELS; do
    for ext in pb json; do
        file="$MODELS_DIR/$(basename "$path").$ext"
        if [ -s "$file" ]; then
            say "present: $(basename "$file")"
        else
            need="$need $path.$ext"
        fi
    done
done
if [ -n "$need" ]; then
    say "These models are by the MTG at Universitat Pompeu Fabra, licensed CC BY-NC-SA 4.0:"
    say "non-commercial use only (https://essentia.upf.edu/models/)."
    run mkdir -p "$MODELS_DIR"
    for path in $need; do
        file="$MODELS_DIR/$(basename "$path")"
        # essentia.upf.edu is slow and sometimes drops connections: retry, and only keep complete files.
        run curl -fL --retry 5 --retry-all-errors --connect-timeout 20 -o "$file.part" "$MODELS_URL/$path"
        run mv "$file.part" "$file"
    done
fi

# --- Kev's weights ----------------------------------------------------------------------------

step "Kev's weights ($KEV_RUN)"
if [ "$WEIGHTS" = 1 ]; then
    say "Downloading by starting Kev's server once with network access (the app starts it offline),"
    say "then stopping it as soon as it answers. Several GB; this can take a while."
    if [ "$DRY" = 1 ]; then
        say "would run: (cd external/kev && $UV run --extra serve python -m kev.serve --run $KEV_RUN --port 8009)"
        say "           until http://127.0.0.1:8009/v1/models answers, then stop it"
    else
        if curl -sf -m 2 http://127.0.0.1:8009/v1/models >/dev/null 2>&1; then
            say "A Kev server is already answering on port 8009, so its weights are in place; nothing to do."
        else
            mkdir -p results
            log="$REPO/results/kev_setup.log"
            say "$ (cd external/kev && $UV run --extra serve python -m kev.serve --run $KEV_RUN --port 8009)"
            say "  log: $log"
            (cd external/kev && exec "$UV" run --extra serve python -m kev.serve --run "$KEV_RUN" --port 8009) \
                >>"$log" 2>&1 &
            pid=$!
            stop_kev() {
                kill "$pid" 2>/dev/null || true
                for p in $(lsof -ti tcp:8009 -sTCP:LISTEN 2>/dev/null); do kill "$p" 2>/dev/null || true; done
            }
            trap 'stop_kev; exit 130' INT TERM
            up=0
            while kill -0 "$pid" 2>/dev/null; do
                if curl -sf -m 2 http://127.0.0.1:8009/v1/models >/dev/null 2>&1; then up=1; break; fi
                sleep 5
            done
            stop_kev
            trap - INT TERM
            if [ "$up" = 1 ]; then
                say "Kev answered; its weights are cached."
            else
                say "Kev's server exited before it answered. The end of its log:"
                tail -n 20 "$log" | sed 's/^/     /'
                exit 1
            fi
        fi
    fi
else
    say "Not downloaded by default (several GB). The app starts Kev offline, so the weights must be in"
    say "the Hugging Face cache beforehand. To fetch them once, run:"
    say "    bin/disko setup --kev-weights"
    say "or by hand (Ctrl-C once it says it is serving):"
    say "    cd external/kev && uv run --extra serve python -m kev.serve --run $KEV_RUN --port 8009"
fi

# --- check ------------------------------------------------------------------------------------

step "Checking"
status=0
"$REPO/bin/disko" doctor || status=$?

cat <<EOF

Start disko:            bin/disko
Put disko on your PATH: ln -s "$REPO/bin/disko" ~/.local/bin/disko
Build the Dock app:     bin/disko app --install
EOF
[ "$DRY" = 1 ] && exit 0
exit "$status"
