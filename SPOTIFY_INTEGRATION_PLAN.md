# Spotify integration plan

Status (2026-11-11): the whole plan is built on branch `spotify/rest` - Milestones 1, 3, 4 and 6 as
written, and Milestones 2 and 5 (the in-app opt-in and the librespot audio path) on the listener's own
instruction, which overrides "Declined: the librespot audio path" below. See "Implementation status"
at the end. Nothing here has been run against a real Spotify account yet: every test runs on the fakes.

Repository baseline: 9f1faf2 (branch `playlist-welcome`).

## Goal and scope

Run a disko set from a Spotify playlist, with the same mix quality as a local playlist folder.

- Spotify becomes one more `Source` in `live/sources/`, next to `local`, `fake` and `beatport`.
- Previews (30 s clips) are analysed for every track in the playlist, because Spotify supplies no tempo or key and `candidate_pool` drops tracks without them.
- The full track is fetched only when it is about to play (the opener and each Kev pick), then analysed and mixed like a local file.
- Out of scope: Apple Music and Tidal as audio sources, recording or exporting a set that contains Spotify audio, keeping Spotify audio after the session.

## What this is, plainly

- Full-track audio comes through librespot, an unofficial open-source Spotify client. It needs a Spotify Premium account.
- This is against Spotify's terms of service whatever plan the account holds. There is no agreement to record in a capability matrix and there never will be.
- Risks the listener takes on: account suspension (highest for bulk fetching, low but not zero for one track every few minutes), and the integration going dark whenever Spotify changes its protocol until librespot is patched.
- Because of that, Spotify audio is off until the listener turns it on in the app after reading what it means (Milestone 2). It is an acknowledgement, not a permission.

## Gate 0: feasibility spike

Do this first; the rest of the plan depends on its answers. Keep it in `scripts/`, outside `live/`.

1. **Sign in.** Complete librespot's OAuth sign-in for a Premium account and keep the reusable credentials.
2. **Fetch one track.** Get one full track as decoded PCM at 44.1 kHz stereo. Try in this order and stop at the first that works:
   - a file fetch (the encrypted Ogg file, decrypted, then decoded with ffmpeg) through `librespot-python`;
   - the same through a small Rust helper on the `librespot` crates;
   - the stock `librespot` binary with its pipe backend (raw PCM on stdout).
3. **Time it.** Record seconds from request to full PCM for a 6-minute track. A file fetch should take seconds; the pipe backend may run at playback speed, which changes Milestone 4 (see "If only realtime works").
4. **Read a playlist.** List the account's playlists and one playlist's tracks with ISRCs. Find out whether the librespot sign-in can do this or whether the listener needs their own Spotify developer app client ID.
5. **Previews.** For one 100-track playlist, count how many tracks have a preview from each of: Spotify's embed page, iTunes Search API by ISRC, Deezer by ISRC.
6. **Preview accuracy.** For 20 tracks, compare the fast pass on the preview against the fast pass on the full track: tempo within 1%, key match rate, and how far loudness and arousal sit above the full-track values.

Exit criteria: a full track decodes to PCM, playlists can be read, and at least about 85% of tracks have a preview from some source. Write the measured answers into this file under "Gate 0 results".

## Current architecture and the gap

- **Already in place:** the `Source` interface (`live/sources/base.py`), `candidate_pool` (`live/sources/pool.py`), `open_track_audio` feeding the decks, an in-memory ffmpeg decode (`beatport/source.py: decode_stream`), Keychain storage and an OAuth flow (`beatport/auth.py`), failure handling during a set (`KevDJ.broken`, `AuthExpired`).
- **Gaps:**
  - `candidate_pool` skips any track missing `MIX_MINIMUM` (`bpm`, `camelot`). Spotify metadata has neither.
  - The fast worker (`live/workers/fast.py`) and the deep worker (`live/workers/deep.py`) both read file paths.
  - `DeepAnalyzer.analyze` (`live/indexer.py`) refuses anything that is not a local file with a path, and requires both kinds of disk retention.
  - Analysis is saved in `<playlist folder>/disko/`. A Spotify playlist has no folder.
  - `Policy` denies every non-local provider without a named agreement; Beatport's audio is unlocked by an environment variable, with no in-app switch.
  - There is no in-window picker for a non-local source, only `--beatport LIST`.

## Milestone 1: Spotify source, browsing only

- `live/sources/spotify/` mirroring `beatport/`:
  - `auth.py`: sign-in, Keychain storage, refresh, logout.
  - `api.py`: playlist and track calls with bounded retries and mapped errors (`TrackUnavailable`, `SourceTimeout`, `AuthExpired`).
  - `source.py`: `SpotifySource` with `_playlists`, `_tracks`, `_info`. `track_info` maps title, artists, duration and availability; the ISRC is kept for preview matching. No `bpm` or `camelot`.
  - `__main__.py`: `python -m live.sources.spotify login | status | playlists | tracks | logout`.
- Default policy: `BROWSE`, `METADATA`, `SEND_TO_MODEL`.
- `tests/fake_spotify.py` and `tests/test_spotify.py`, in the manner of the Beatport ones. Tests never call Spotify.

Done when: the CLI lists a real account's playlists and tracks, and the tests pass on the fake server.

## Milestone 2: the in-app opt-in

- A one-time confirmation shown at Spotify sign-in, before any audio is touched. It says, in plain words: this uses an unofficial client, it is against Spotify's terms, the account could be suspended, and it may stop working without notice.
- Two outcomes: "Browse only" (the default) and "Turn on Spotify audio". The choice is stored with the Spotify settings and can be changed later from the same place; signing out clears it.
- `spotify.default_policy()` reads that stored choice:
  - off: browsing operations only;
  - on: adds `DECODE`, `ANALYZE`, `MIX` and `PERSIST_ANALYSIS`, with the agreement reference worded honestly, for example "listener's own choice, made in the app on DATE (unofficial client; not permitted by Spotify)".
- Never granted: `RECORD`, and `PERSIST_AUDIO` beyond the session temp folder (Milestone 3).
- No environment variable unlocks audio. A headless run uses the stored choice.

Done when: with the choice off, `open_audio` raises `PolicyDenied` before any network call; with it on, the policy lists the audio operations; tests cover both and the reset on sign-out.

## Milestone 3: analysis for tracks without a file

Provider-neutral work. It also gives Beatport streams a deep pass.

- **Session temp audio.** A per-session temp folder, created at start and deleted on exit and on source disconnect. Decoded audio is written there only so the workers can read it, and each file is removed as soon as its analysis finishes. Express this in `Policy` as its own retention flag (for example `audio_in_session_temp`) rather than `audio_on_disk`, so the deep pass no longer needs permanent audio retention.
- **Analysis store.** A home for saved analysis that is not a playlist folder: `~/.disko/sources/<provider>/` with the same layout `live/playlist.py` uses (`tracks.csv`, `analysis/<id>.*.json`, `skipped.json`), keyed by track id. A track analysed once is not fetched again for analysis.
- **Deep pass.** `DeepAnalyzer.analyze` accepts a non-local track: open its audio through the source, write it to the session temp folder, send that path to the deep worker, delete the file when the result is in.
- **Fast pass.** The fast worker accepts jobs whose path is a temp file and whose id is a provider-qualified track id.

Done when: `--fake N` runs with the fake source's tracks going through both passes via the temp folder, the temp folder is empty after each analysis and gone after exit, and local playlists behave exactly as before.

## Milestone 4: previews and the pool

- **Preview fetcher** (`live/sources/spotify/preview.py`): for each track, try Spotify's embed page, then iTunes Search API by ISRC, then Deezer by ISRC. Record which one supplied it. Small, rate-limited, and cached in the analysis store by track id.
- **Preview pass.** Run the fast worker over the previews for the whole pool. Write the features into each `Track` with provenance "30 s preview (SOURCE)".
- **Pool order.** Build the pool after the preview pass, so `MIX_MINIMUM` is met. Tracks with no preview are left out with the reason "no preview to analyse".
- **UI.** The indexing screen shows the preview pass like the folder fast pass. The deck and browser mark preview-derived values as estimates.
- **Known bias.** Energy, loudness and arousal read high on previews, since they are usually the hook. Ranking within a Spotify playlist still holds because every track is biased the same way. Do not compare preview features against full-track features from another source.
- **Not available from a preview:** beat grid, phrase structure, intro and outro. Nothing downstream may read these before Milestone 5 has run for the track.

Done when: a 100-track Spotify playlist reaches the "pick an opener" state with tempo and key on every pooled track, with no full track fetched.

## Milestone 5: full-track fetch and playback

- **Fetch helper** (`live/workers/spotify_fetch.*`): a separate process, like the other workers, using whichever implementation Gate 0 chose. Given a track id it returns the audio; a crash or hang there never takes the app down.
- **`SpotifySource._open_audio`**: returns an `AudioHandle` whose `read` yields `(samples, 44100)`, decoded in memory in the manner of `decode_stream`.
- **On pick** (the opener, each Kev pick, both tracks in manual mode):
  1. fetch the full track;
  2. run the fast pass on the full audio and replace the preview-derived features, so the beat grid covers the whole track;
  3. run the deep pass (Milestone 3);
  4. queue the mix as today.
- **Pacing.** Fetch only on pick, one at a time, never ahead for the whole pool. This is the main protection against the account being flagged.
- **Failures.** Map to the existing behaviour: unavailable or unreadable goes on `KevDJ.broken` and Kev picks again; timeouts retry; a rejected sign-in raises `AuthExpired` and stops Spotify tracks with a reason. If the deep pass fails twice the pick is mixed on the rough grid, as for local tracks.

Done when: a full set runs from a Spotify playlist with Kev picking, cues land on phrase boundaries as they do for local tracks, and no audio remains on disk afterwards.

### If only realtime works

If Gate 0 finds only the pipe backend usable (audio arrives at playback speed), a pick cannot be analysed before it plays. Fall back to: start the fetch as soon as the pick is made, mix in on the fast-pass rough grid from whatever has arrived, and run the deep pass when the track completes, so only the mix out of that track is phrase-aligned. Decide whether that is good enough before building Milestone 5.

## Milestone 6: picking a Spotify playlist in the window

- The welcome screen (`live/ui/welcome.py`) gains "Spotify" next to the folder drop: sign in, the opt-in from Milestone 2, then a list of the account's playlists.
- `--spotify PLAYLIST` and `--spotify-limit N` as launch options, matching `--beatport`.
- A snapshot scenario for the Spotify welcome state, for offscreen checks.

## Verification

- Unit tests on the fake Spotify server: paging, metadata mapping, error mapping, policy off and on, sign-out.
- Temp-folder tests: files appear only during analysis, and nothing is left after exit or disconnect.
- Local parity: `tests/fixtures/rank_house.json` still matches, and a local playlist indexes and plays unchanged.
- One manual run on a real account per milestone from 4 on, with what was observed noted here.

## Suggested order and open questions

Order: Gate 0, then Milestones 1, 2, 3, 4, 5, 6. Milestone 3 does not depend on Spotify and can be done at any point before 5.

Open until Gate 0 answers them:
- Which librespot implementation fetches today, and how fast.
- Whether playlist reading works from the librespot sign-in or needs the listener's own developer app.
- What share of a playlist has a usable preview, and from which source.
- Whether one fetch every few minutes draws any rate limit or warning.
- Whether Apple's and Deezer's terms allow analysing their previews; nothing found either way.

## Gate 0 results

(never measured: nothing here has been run against Spotify. See "Not built" at the end.)

## First run on a real account (checklist)

Nothing in this plan has been run against Spotify. This is the order to do it in, with what each step
should say and what a failure at that step means. Do it once, on the machine disko runs on, and write
what you saw under "Real run results" at the end of this file. Steps 1 to 5 need no audio: they answer
Milestone 1 and the preview-coverage question on their own.

1. **A developer app.** At https://developer.spotify.com/dashboard create an app with redirect URI
   `http://127.0.0.1:8766/callback`, exactly (Spotify accepts a loopback IP literal, not `localhost`),
   and copy its client ID. A Premium account is needed later, for audio.
2. **Keep the client ID.** `.venv-live/bin/python -m live.sources.spotify set-client` (Keychain), or put
   it in `.env` as `DISKO_SPOTIFY_CLIENT_ID=…`; the environment wins if both are set.
3. **`… spotify status`** should say browsing is allowed, the client ID is set and you are not signed in.
   "no client ID" here means step 2 didn't land.
4. **`… spotify login`** opens the browser; approving it should end with a signed-in line and no error.
   A login that hangs is almost always a redirect URI that doesn't match the app's, character for
   character.
5. **`… spotify playlists`** and **`… spotify tracks liked --limit 5`** are Milestone 1's own "done
   when". If a listing comes back empty or errors, add `--raw` to one track call and check the field
   names against `track_info` in `live/sources/spotify/source.py`; the playlist API moved to `/items`
   with entries under `item`, and the source falls back to the old names once, so a wrong shape there is
   the likely cause.
6. **Preview coverage** (Gate 0 step 5, never measured). This asks no audio at all, only the preview
   sources, and takes a couple of minutes on 100 tracks:

   ```bash
   .venv-live/bin/python - <<'PY'
   from live.runtime import session_temp
   from live.sources.pool import listed
   from live.sources.spotify import connect, load_policy
   from live.sources.spotify.preview import PreviewFetcher

   source = connect(policy=load_policy())
   infos, skipped, truncated = listed(source, "liked", limit=100)
   previews, counts = PreviewFetcher(session_temp().folder("previews")), {}
   for info in infos:
       found = previews.fetch(info)
       counts[found.provider if found else "none"] = counts.get(found.provider if found else "none", 0) + 1
   print("tracks:", len(infos), "skipped:", len(skipped), counts)
   PY
   ```

   The plan's exit criterion was about 85% with a preview from somewhere. All of them "none" means the
   embed page stopped handing out preview URLs and the ISRC lookups are the only road left; that belongs
   in the results below, with the counts.
7. **Turn audio on.** `.venv-live/bin/python -m live.sources.spotify audio on`, read the warning, and
   confirm with `… status` that the audio operations are listed. Nothing else turns it on.
8. **The fetch helper's environment and its own sign-in.** `scripts/setup.sh --spotify-audio` builds
   `.venv-spotify` from `requirements/spotify.txt` (it refuses nothing; it just takes a while), and
   ffmpeg must be on PATH. Then, once: `.venv-spotify/bin/python live/workers/spotify_fetch.py --login`.
   This is a second, separate sign-in: librespot keeps its own credentials under
   `~/.disko/spotify/librespot/`. Its client ID is librespot's own, not yours.
9. **Fetch one track, alone** (Gate 0 steps 2 and 3, the likeliest failure point, and the cheapest place
   to see it). Take a 22-character track id from step 5's output:

   ```bash
   time .venv-spotify/bin/python live/workers/spotify_fetch.py --track <22-char-id> --out /tmp/one.wav
   ```

   Expect one JSON line with `seconds` and `bytes`, and a WAV of the track at 44.1 kHz. What a failure
   means: `auth` is the librespot sign-in (step 8) or a non-Premium account, `unavailable` is a
   region-locked or non-music track, a message about ffmpeg is `brew install ffmpeg`, and a timeout is
   the helper being stuck rather than Spotify being slow. Write the timing here: the plan's Gate 0 wanted
   seconds per 6-minute track, and it decides whether the pick-then-fetch order holds up.
10. **A real set.** In the window (the Spotify card on the welcome page) or by hand:
    `.venv-live/bin/python -m live --spotify liked --spotify-limit 30`. Watch for: the pool line and any
    "left out" reasons, the first pick playing at all, the mix into the second track landing on phrase
    boundaries the way it does for local files, and nothing left behind afterwards (the session temp
    folder is under `$TMPDIR/disko-session-*`; it should be gone after exit).

### Real run results

(to be filled in: the counts from step 6, the fetch seconds from step 9, and one line per step that
didn't do what this file says it should)

Gate 0 step 6, comparing a preview's fast pass against the full track's, is left out on purpose: it
needs real long tracks and the analysis workers, which makes it a second session rather than part of a
first run. Do it once a set has played through and the preview pool has been seen to pick well.

## Implementation status

### Built

**Milestone 1, browsing (`live/sources/spotify/`).** Sign-in through the official Web API with the
listener's own developer app (authorization code + PKCE, loopback redirect `http://127.0.0.1:8766/callback`,
no client secret), tokens in the Keychain under `disko.spotify`, refresh on expiry and on 401, sign-out
forgets the tokens (Spotify has no revocation endpoint). `SpotifySource` lists the account's playlists
plus Liked Songs (`liked`), pages playlist tracks, and maps title, artists, duration, ISRC and market
availability. Playlists can be named by id, `spotify:playlist:` URI or open.spotify.com link. It reads
both the 2026 playlist API names (`/playlists/{id}/items`, entries under `item`) and the older ones
(`/tracks`, `track`), falling back once and remembering which one worked. CLI: `python -m live.sources.spotify
set-client | login | status | playlists | tracks | track | logout | forget-client`. `TrackInfo` gained
an `isrc` field.

- Default policy: `BROWSE` and `METADATA` only. `SEND_TO_MODEL` is left out, unlike this plan's first
  draft: Spotify's Developer Policy forbids feeding Spotify Content into an AI model, which includes
  sending track descriptions to Kev. A `capabilities/spotify.json` can change that if the listener decides otherwise.
- Tests: `tests/test_spotify.py` against `tests/fake_spotify.py` (25 tests, no request leaves the machine).
- Not yet done: listing a real account's playlists (the milestone's own "done when").

**Milestone 3, analysis for tracks without a file (`live/sources/store.py`, `live/indexer.py`).**
- `Operation.TEMP_AUDIO` with its own retention flag, `audio_in_session_temp` (`retention.audio_in_session_temp`
  in a capability matrix); `audio_on_disk` implies it. The deep pass now needs `DECODE`, `ANALYZE` and
  `TEMP_AUDIO`, plus `PERSIST_ANALYSIS` only when its results are kept on disk, so it no longer needs
  permanent audio retention.
- `SessionTemp`: one folder per run, created on first use. Each decoded file is deleted when its
  analysis is in, a source's files are deleted when it disconnects (`Source.on_close`), and the
  folder is removed on exit. The deep worker keeps its stem WAVs there too (`"tmp"` in its request).
- `SourceStore`: `~/.disko/sources/<provider>/` with the playlist folder's layout, keyed by track id
  (a file-safe form of it), when the policy allows `PERSIST_ANALYSIS`. Otherwise the store lives in the
  session temp folder. A track analyzed once isn't decoded for analysis again. Results fill only what
  the source left unknown, plus the beat grid.
- `SourceIndexer`: the fast pass over a source's tracks, feeding `live/workers/fast.py` through its new
  `--jobs -` mode (one job per stdin line), so only a few decoded tracks sit in the temp folder at a time.
- `DeepAnalyzer` takes a Playlist or a SourceStore. A non-local track is decoded on a background thread,
  never on the caller's.
- `--fake N` (window and headless) runs both passes through the temp folder. `--beatport` does too
  only if its policy grants `TEMP_AUDIO`; the built-in Beatport policy doesn't, so it behaves as before.
- Tests: `tests/test_store.py` (13 tests) with stub helpers in `tests/stub_workers/`. The full suite
  passes (149 tests), local playlists included.
- Real run (Essentia and allin1, `--fake 4`, 180 s tracks): fast pass on 4 tracks in 10 s, deep
  pass on one in 17.5 s (92 bars). No audio or stems were left after either pass, and the session
  folder was gone after exit.

### The librespot audio path: built on the listener's instruction

This section used to say that Milestones 2 and 5, and Gate 0 steps 1-3, would not be written for
disko. The listener asked for them anyway, so they are built, and the objection in "What this is,
plainly" stands unchanged: fetching Spotify's encrypted audio and decrypting it outside Spotify's own
players gets around its copy protection, not only its terms of service, and a listener's
acknowledgement doesn't change that. Nothing in the integration pretends otherwise, and it is off
until the listener turns it on themselves.

**Milestone 2, the in-app opt-in (`live/sources/spotify/consent.py`, `__init__.py`, `__main__.py`).**
- Audio stays off until the listener turns it on, with the same warning text in the terminal and in
  the window (`WARNING` in consent.py), before any audio is read.
- The choice is the file `~/.disko/spotify/consent.json` (`{"version": 1, "audio": true, "date": ...}`).
  Deliberately not in the Keychain (it is not a secret), not in `.env` or the environment (a headless
  run must use the same choice as the window, and no variable may unlock audio on its own) and not in
  `QSettings` (the CLI reads it too). Signing out, and `forget-client`, clear it.
- `default_policy` reads it: off gives `BROWSE` and `METADATA`; on adds `DECODE`, `ANALYZE`,
  `TEMP_AUDIO`, `PERSIST_ANALYSIS` and `MIX`, with the agreement worded as the listener's own choice,
  dated, "unofficial client; not permitted by Spotify". `RECORD` and `PERSIST_AUDIO` are never granted,
  and a capability matrix can only narrow the policy down (`_gated`), never unlock audio.
- CLI: `audio on | off | show` (`--yes` skips the warning). Tests: off refuses `open_audio` with
  `PolicyDenied` before any request, a matrix alone cannot turn audio on, and sign-out clears it.

**Milestone 4, previews and the pool (`live/sources/spotify/preview.py`, `live/sources/pool.py`,
`live/sources/store.py`, `live/indexer.py`).**
- `preview_pool` lists the playlist's playable tracks, fetches a clip for each (embed page, then the
  iTunes Search API by ISRC, then Deezer by ISRC, recording which one supplied it), analyses the clips
  with the fast worker through its `--jobs -` mode (no decoding and no session temp folder: the clip on
  disk goes straight in), and only then decides the pool, leaving out a track with no clip
  ("no preview to analyse") or an unmeasured tempo/key ("unknown ...").
- The measured features go into the Tracks with provenance `30 s preview (SOURCE)`, and into the
  analysis store as `<key>.preview.json` beside whole-track results, so a later run neither fetches nor
  analyses them again (the clips are cached in the store too, under `previews/`).
- `PreviewIndexer.run(progress)` reports `start`, `done`, `failed` per clip and raises `RuntimeError`
  when the analysis can't run at all.
- Tests: `tests/fake_previews.py` and `tests/test_preview.py`; a playlist of 30 tracks reaches a pool
  of 27 tracks with a tempo and a key on every one of them, with one lookup and one download per clip,
  no full track fetched and nothing decoded into the session temp folder.

**Milestone 5, full-track fetch and playback (`live/workers/spotify_fetch.py`,
`live/sources/spotify/fetch.py`, `source.py`, `live/indexer.py`).**
- The helper is a process of its own in its own venv (`requirements/spotify.txt`: librespot, whose
  `protobuf==3.20.1` pin no other venv may hold), line-oriented JSON over stdin, one track at a time
  only when it is about to play, with `librespot-python` by default and the `librespot` binary's pipe
  backend selectable (`--backend`, `DISKO_SPOTIFY_FETCH_BACKEND`). The decrypted Ogg is decoded to WAV
  with ffmpeg in a temp folder of its own; the helper's own sign-in is separate from the Web API one
  (`--login` in the fetcher's venv, `DISKO_SPOTIFY_CREDENTIALS`).
- `SpotifySource._open_audio` returns a `FetchedAudio` that fetches on its first read, so an
  unavailable track costs no download, decodes in memory at 44.1 kHz stereo, and holds the last couple
  of WAVs; a crash, a hang or a rejected sign-in maps to `TrackUnavailable`, `SourceTimeout` and
  `AuthExpired` as everywhere else. A missing fetcher is reported plainly, never guessed at.
- `DeepAnalyzer` gained a `FullPass`: when a track is picked, its whole audio is fetched, the fast pass
  replaces the preview-derived estimates (so the grid covers the whole track) and the deep pass runs as
  for a local file. The mixing minimum is a pick-time concern now, not a listing-time one.
- Tests: `tests/stub_workers/spotify_fetch.py` speaks the same protocol with no network; the suite
  covers a decode through the source, one fetch per track, the helper stopping, a refused track, no
  fetcher, and that the full pass replaces what the previews measured.

**Milestone 6, picking a Spotify playlist in the window (`live/ui/welcome.py`, `live/__main__.py`).**
- The home page has a Spotify card next to the folder drop. Its page shows the sign-in state, the
  warning and its two outcomes ("Turn on Spotify audio" / "Browse only") where the choice is made, the
  sign-in button (the browser flow runs off the window thread), and the account's playlists. Picking one
  pools it with the same page's progress: the clips are analysed on a thread and reported into the
  indexer's event protocol, so the page shows a count of what is left and each clip as it lands.
- `--spotify PLAYLIST` and `--spotify-limit N`, matching `--beatport`; with audio off they refuse the
  run before the playlist is read and name the command that turns it on.
- `--scenario spotify` renders that page offscreen for checks (`--snapshot out.png --scenario spotify`),
  which is how it was checked: signed in, audio off, the warning and four playlists drawn in full.
- Tests: `tests/test_welcome.py` (9 tests, offscreen, no Spotify) covers what each state shows, that the
  choice goes through the same call the terminal uses, that a pool build drives the page to a set, and
  that a playlist that couldn't be read says so. Run alone it needs `QT_QPA_PLATFORM=offscreen`.
- `live/runtime.py` now holds the one session temp folder, so the window can build a pool before the
  set window exists and hand the same folder to the store, the fetch helper and the deep pass.
- `scripts/setup.sh --spotify-audio` builds the fetcher's venv (it is not built by default, since only a
  listener who turned audio on has any use for it) and `bin/disko doctor` reports it without ever
  treating it as blocking.

### Not built

- **Matching by ISRC against licensed audio.** The plan's other option (match a Spotify playlist's ISRCs
  against the listener's own files or Beatport, and report the tracks with no match) is not built. It is
  the route that stays within licensed audio, and it is worth doing if the librespot path ever has to go.
  `TrackInfo.isrc` is read and kept, so the matching itself is the only missing piece.
- **Gate 0 was never measured.** No sign-in, no fetch, no real playlist and no preview-coverage count has
  been run against Spotify: the librespot API calls in `live/workers/spotify_fetch.py` were checked
  against the package's own source and its tests, not against the service. The first real run is still
  owed, per milestone from 4 on, with what was observed written here.
- **`librespot`'s credentials do not exist yet.** `spotify_fetch.py --login` (or `--login` through the
  fetcher's venv) has to be run once before a Spotify track can be played. The app says so; the Web API
  sign-in and this one are separate.
