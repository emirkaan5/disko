# Spotify integration plan

Status (2026-11-11): Milestones 1 and 3 built and tested; the librespot audio path (Gate 0 steps 1-3,
Milestones 2 and 5) declined; Milestones 4 and 6 waiting on a decision about where playable audio comes
from. See "Implementation status" at the end. Nothing here has been run against a real Spotify account yet.

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

(to be filled in)

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

### Declined: the librespot audio path

Gate 0 steps 1-3, Milestone 2 (the in-app audio opt-in) and Milestone 5 (full-track fetch) are not
built. Fetching Spotify's encrypted audio files and decrypting them outside Spotify's own players gets
around Spotify's copy protection, not just its terms of service. A listener's acknowledgement doesn't
change that, so this code won't be written for disko. `SpotifySource._open_audio` refuses even when a
capability matrix grants `DECODE`.

### Open: where a Spotify playlist's audio comes from

Without Milestone 5, a Spotify playlist can be browsed but not played. Milestones 4 (previews and the
pool) and 6 (picking a Spotify playlist in the window) are only worth building once that is decided.
Options that stay within licensed audio:

- Match by ISRC (now in `TrackInfo`) against sources disko can already play: the listener's own files,
  or Beatport with a streaming plan. Build the set from the matches and report the tracks with no match.
- Use Spotify as the set's source of tracks only, and run the set on a local playlist or Beatport list
  that the matches build.
