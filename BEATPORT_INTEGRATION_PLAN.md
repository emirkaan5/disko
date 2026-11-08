# Beatport integration plan

Status:
- Milestones 1, 2 (synthetic audio only) and 4 are implemented.
- Milestone 3: the provider-neutral part is implemented, and so is a Beatport browsing adapter built against a local fake server. It needs only the listener's own client ID and a sign-in. Its permissions (browse, metadata, Kev) are built in; at the listener's request (2026-11-08) the capability matrix is optional and only restricts them.
- Beatport playback is not built: the stream interface is undocumented, and Gate 0 decides whether disko may decode it.

Repository baseline: 47306415d1b94a9597e5fe75113fd94f1c4e7578.

## Implementation status

No Beatport credential or audio is in the repository. The only Beatport endpoints called are the documented v4 OAuth, playlist, chart and catalog-track ones. The tests call them on the fake server, never on Beatport.

### Milestone 1: provider-neutral contracts
- `live/sources/`:
  - `base.py`: provider-qualified `TrackId`, browsing types (`TrackInfo`, `Page`, `PlaylistRef`), `Availability`, `Operation`, and bounded `AudioHandle`s. Handles expire, and they close when their source disconnects; a disconnected source refuses all further reads.
  - `policy.py`: a fail-closed `Policy` built from a capability matrix. It grants nothing to any provider but local files until the matrix names a written agreement.
  - `local.py`: the playlist folder as `LocalSource`.
  - `fake.py`: a synthetic `FakeSource` with configurable permissions, missing metadata and failure modes.
  - `beatport.py`: reports the gate status and refuses to connect.
- `live/sources/capabilities/beatport.json` was the Gate 0 capability matrix. The listener removed it on 2026-11-08; Beatport's permissions now come from `beatport.default_policy()`, and the file, if added back, restricts them.
- Track identity:
  - A track's identity is its provider plus its key.
  - A local track keeps its bare file id as `uid`, because its analysis files are named after it. Any other provider's `uid` is the full id.
  - A library refuses duplicate ids.
- Unknown features:
  - A feature the source doesn't supply is `None`, and each known value carries its `provenance`.
  - `MIX_MINIMUM` (tempo and key) gates ranking, and `Library.unrankable` says why a track is left out.
- Ranking:
  - `Library.rank` scores each pair on the signals both tracks know and renormalizes the weights over those.
  - Kev's track descriptions, the mix rules and the deck and browser UI say "unknown" instead of inventing values.
  - The house set ranks exactly as before; `tests/fixtures/rank_house.json` was captured from commit 7f2106d.
- Deny-before-use:
  - Decoding goes through the track's source (`open_track_audio`).
  - `load_deck` requires DECODE, ANALYZE and MIX.
  - allin1 (`DeepAnalyzer`) requires DECODE, ANALYZE and both kinds of disk retention, and reads local files only.
  - Kev is told only about tracks whose policy allows SEND_TO_MODEL.

### Milestone 2: playback (synthetic only)
- `python -m live --fake N` runs the whole app on the synthetic catalog.
- `tests/test_engine.py` checks the engine on synthetic PCM where every sample is tagged with its origin: handoff, seek, cancel, pause, end of track, snapshots and volume, plus two tracks joined by one transition.

### Milestone 3: the provider-neutral part
- `live/sources/pool.py`:
  - `candidate_pool` builds a bounded pool from a source's playlist. Paging is capped, so a source that misreports its totals can't keep it paging.
  - Unplayable tracks are left out with their reason, and duplicates are dropped.
  - `track_from_info` copies only the metadata the source supplied.
- Failures during a set:
  - A pick whose audio is unavailable, not permitted or unreadable goes on `KevDJ.broken` and is never offered again. Kev picks again at once, and when nothing is left the set ends rather than retrying.
  - Timeouts are retried.
  - `AuthExpired` and `Session.disconnect(source)` stop whatever came from that source and say why; nothing already decoded is assumed to stay authorized.
- Not built, because each needs the Beatport contract (Gate 0): the connect/disconnect and playlist UI, the just-in-time analysis path (Branch A or B), and incremental streaming buffers.

### Milestone 3: the Beatport adapter (browsing; built against a fake server)
- `live/sources/beatport/` implements the documented v4 flow. The order of checks below makes it work once the listener has their own client ID, with no code changes; nothing in it has touched Beatport yet.
  - `auth.py`:
    - Sign-in uses OAuth2 authorization code with PKCE, through a loopback redirect (`http://127.0.0.1:8765/callback` by default; `DISKO_BEATPORT_REDIRECT` overrides it).
    - The callback checks `state`. Tokens are refreshed ahead of expiry and once more on a 401, and revocation goes in a form body, never a URL.
    - The client ID and secret are read from `DISKO_BEATPORT_CLIENT_ID` and `DISKO_BEATPORT_CLIENT_SECRET`, in the environment or a git-ignored `.env` at the repository root (see `.env.example`).
      - If neither is set, they come from the Keychain (`set-client`).
      - disko warns when the `.env` holding a secret is readable by others.
    - Tokens always live in the macOS Keychain (service `disko.beatport`). Values are written through `security -i`'s stdin, so they never show on a command line.
    - The docs page's own client ID is refused; it's stored as a SHA-256 hash.
  - `api.py`:
    - Every request carries a bearer token.
    - 404 becomes `NotFound` (gone or territory-restricted), 403 becomes `Forbidden`, and 401 after a refresh becomes `AuthExpired`.
    - 429 and 5xx are retried twice, waiting at most 10 s for a Retry-After, then raise `SourceTimeout`.
  - `source.py`:
    - `BeatportSource` covers the account's playlists (`my:ID`), curated playlists (`catalog:ID`) and charts (`chart:ID`), and maps Beatport pages onto any offset and limit.
    - `track_info` copies only the catalog's bpm, Camelot key, genre and sub-genre, length, and `is_available_for_streaming`. Its field names come from public v4 clients, because the docs have no response schemas, so it is the one place to correct after the first real call.
    - `open_audio` is refused by the policy before any request; even if the policy allowed it, there's no stream code.
  - `__init__.py`: the policy is `default_policy()` (BROWSE, METADATA, SEND_TO_MODEL) unless `capabilities/beatport.json` exists, in which case that file replaces it and fails closed if malformed. `connect`, `login` and `status` check, in order:
    1. The policy allows BROWSE. Nothing touches the Keychain or the network before this passes.
    2. A client ID is set.
    3. The account is signed in.
  - `python -m live.sources.beatport`: `set-client`, `login [--no-browser]`, `status`, `playlists`, `tracks LIST [--limit]`, `track ID [--raw]`, `logout`, `forget-client`.
- `tests/fake_beatport.py` is a local stand-in with:
  - the documented OAuth endpoints, with PKCE and client-secret checks;
  - paginated playlists and charts, and catalog tracks;
  - territory 404s, and tracks that are unstreamable or missing their bpm or key;
  - switchable 429/5xx answers, expired tokens and revocation.
- `tests/test_beatport.py` (36 tests) covers:
  - the gate: a restrictive policy stops everything before the Keychain is touched;
  - `.env` parsing and precedence;
  - borrowed client IDs;
  - sign-in with and without a secret, and forged callbacks;
  - refresh, both early and on a 401;
  - revocation and logout;
  - paging across page boundaries, and the skip reasons;
  - ids that are rejected before any request;
  - retries;
  - no audio;
  - the command line;
  - tokens never appearing in a URL.
  
  The real Keychain is tested with `DISKO_TEST_KEYCHAIN=1`.
- Still open:
  - Check `track_info` against a real response.
  - Pick Beatport playlists in the app. Without playback a Beatport track can't be loaded on a deck, so there's nothing to DJ yet.
  - Playback itself (Milestone 2, Branch A or B), once the terms and the stream interface are known.

### Milestone 4: session controller and Hermes
- `live/session.py`: the set's state machine, taken out of `live/ui/window.py`.
  - It has no Qt dependency and an explicit owner thread.
  - Other threads go through `Session.call()`, which queues the command for the owner's next `tick()`.
  - The window is now a view over it, and `python -m live --headless` runs it with no window.
- `live/control.py`: local, authenticated control.
  - It speaks HTTP+JSON over a Unix socket in `~/.disko`. The folder is 0700, and the socket and token file are 0600.
  - Every request needs a bearer token, which is regenerated at each start. Stale sockets are replaced, a second instance is refused, and requests are handled one at a time.
  - TCP is off unless `--control-tcp HOST:PORT` is given, and needs the token too.
  - Commands: `GET /status`, `GET /tracks`, `POST /start`, `/pause`, `/resume`, `/stop`, `/energy`, `/pick-again`.
  - Status reports only the set's own state, including the sources with their permitted operations and the playback device.
- `python -m live.ctl` is the command-line client. `python -m live --control` turns control on for the window.
- `integrations/hermes/disko/` is the Hermes plugin:
  - `plugin.yaml` and `__init__.py`, standard library only.
  - Eight tools: `disko_status`, `disko_find_tracks`, `disko_start`, `disko_set_energy`, `disko_pick_again`, `disko_pause`, `disko_resume`, `disko_stop`.
  - A bundled `disko-dj` skill.
  - To install, copy the folder to `~/.hermes/plugins/disko` and run `hermes plugins enable disko`.
  - For Hermes in a container, set `DISKO_CONTROL_URL` and `DISKO_CONTROL_TOKEN`.

### Verification
- `.venv-live/bin/python -m unittest discover tests` runs 99 tests (one skipped unless DISKO_TEST_KEYCHAIN=1):
  - ids, policies, deny-before-use, failures, paging
  - ranking parity and unknowns
  - the engine on synthetic PCM
  - the session with a stub Kev (the full autopilot loop, commands, thread ownership, unreadable picks, expiry, disconnect)
  - the control socket (file permissions, tokens, restart, TCP, serialized requests)
  - the candidate pool
  - the Hermes plugin against a running session
- Also checked by hand:
  - House-set and playlist snapshots, unchanged after the window refactor.
  - A Kev-driven snapshot with the real Kev-4B model.
  - The offscreen window driven end to end over the socket.
  - `--headless --fake` queried with `live.ctl`.
- Not exercised: a real Hermes install, and anything involving Beatport.

## Goal and scope

A listener signs in to an authorized Beatport account, chooses a playlist or bounded collection, and starts a continuous disko set. Kev continues to choose tracks and transitions; the user can steer energy. Music plays on the machine running disko. A Hermes plugin is an optional later controller, not a prerequisite for this integration.

The first release targets personal listening and online playback. It does not promise recording, exported mixes, unrestricted offline storage, public performance, multiple accounts, or general catalog-wide bulk analysis.

## Gate 0: authorization and technical feasibility

Do not access protected streams, build on undocumented endpoints, reuse another application's credentials, circumvent protection, or run Beatport integration tests without the required authorization. Beatport's public terms require approved API access and written permission for specified subscription/content-distribution uses; a consumer subscription or open-source/non-revenue status is not an integration grant. Partner documentation and agreement were not available for review.

Ask Beatport for written answers covering:
- Open-source AGPL-3.0 client distribution; permitted SDK and dependency licenses, credential handling, review and user onboarding; agreement and fees.
- Catalog and playlist access, track availability, supported territories, reporting, and required subscription tier.
- Playback interface: simultaneous decks, seek/sample range access, output routing, access to decoded PCM, timing accuracy, and whether processing outside the SDK is supported. A normal player SDK is not evidence of PCM access.
- Permission for whole-track and just-in-time analysis, inference with pretrained Essentia, allin1 and Demucs models, autonomous Kev decisions, and transfer of metadata/derived features to Kev or Hermes. No training on Beatport data is proposed.
- Whether processed audio, waveforms, beat grids, per-bar stem metrics, embeddings, metadata and temporary or prerendered transition buffers may be stored; approved locations, encryption, limits, expiry, deletion, crashes and revocation. RAM and disk rules may differ.
- If offline operation, recording, export, mixed-source playback, screenshots/cover art or public performance are permitted; assume not supported until explicitly authorized.

Decision: record the written authorization and SDK contract in a capability matrix. If playback can be externally controlled but PCM processing/mixing isn't available, stop and assess an authorized player-native approach; do not claim that disko's existing engine can mix it. If required processing is not authorized, retain local/demo playback and do not ship Beatport audio integration. Public statements about AI-assisted mixing and existing partners are precedents, not grants to disko.

## Current architecture and the actual gap

- `live/playlist.py:162-177,181-246` scans files, identifies tracks by relative path, expects fast analysis, and stores CSV and JSON under the music folder.
- `live/library.py:63-93,273-321` requires `Track.path` and numeric features; ranking precomputes a fixed feature matrix and assumes valid Camelot keys. `live/dj.py:89-120` also assumes those features are present for descriptive cards.
- `live/indexer.py:35-63,91-147` passes filesystem paths to separate fast and deep worker processes. `live/workers/deep.py:61-77` writes temporary stems and persistent structural JSON.
- `live/audio.py:23-46,103-118,121-148` decodes a full file to PCM, retains an LRU cache, generates a waveform, measures beat period and prepares stretched decks. `live/mixes.py` builds pre-rendered transitions from deck sample arrays.
- `live/engine.py:31-48,75-131` plays full deck arrays and scheduled transition buffers. Its callback must not perform network requests or block waiting for analysis. `live/ui/window.py:324-345,366-397` coordinates opening, analysis and Kev planning in UI-driven flows.
- This is a static analysis of commit 4730641, not a verified Beatport implementation. `PLAN.md` describes intentions; the implemented local playlist persistence is CSV/JSON.

## Milestone 1: extract provider-neutral contracts, with local parity

Introduce small interfaces, adapting the existing local implementation first. Suggested paths, not existing files:

- `live/sources/base.py`: stable provider-qualified TrackId; catalog/playlist browsing; explicit availability, capabilities and permitted operations. Separate identity and metadata from audio access. Make unknown fields explicit rather than inventing BPM, key, energy, vocals or other analysis.
- `live/sources/local.py`: adapter for the current folder scan and local tracks.
- `live/sources/fake.py`: deterministic, synthetic test source with configurable capability and failure modes. No Beatport music, endpoints, account or branding required.
- `live/sources/policy.py`: fail-closed capability/retention decisions based on the actual agreement, not assumptions. Denial must happen before decoding, analysis, writing, sending, or export.

Refactor `Track` so the local `path` is not the universal identity or implicit right to decode. Introduce an explicit analysis representation with provenance, unknown values, versioning and retention policy. Do not persist provider data in the current `<music-folder>/disko/` cache by default.

Update `Library.rank()` and Kev's cards to cope with absent feature values and changing candidate sets. Use only known features and normalize weights over available signals; mark unknowns as unknown and avoid pretending absent values are zero. Define explicit minimum metadata and a safe fallback for an undersized library. Preserve local-source ranking behavior with regression fixtures.

Acceptance: existing local folder indexing, playback, selection and transitions continue to work; fake catalog IDs cannot collide with local IDs; missing data never becomes invented data; unsupported operations fail before data access.

## Milestone 2: choose a playback architecture from the actual contract

Branch A — authorized PCM access, local processing and necessary retention:
- Implement a provider-owned playback/acquisition handle with bounded lifetimes, explicit close/expiry, cancellation and no credential leakage. If full-track materialization is explicitly permitted, a bounded in-memory/prepared-deck bridge may reuse the existing `Deck` and renderer. Do not assume permission to write decoded files or stems to disk.
- Only adapt fast/deep workers for approved processing and storage. The current workers require file paths and the deep worker writes temporary stem WAVs; replace or isolate these byproducts if the agreement does not permit them. Ensure normal shutdown, worker failure and crash recovery follow the agreed cleanup rules; don't promise secure deletion.
- Implement limited just-in-time acquisition/analysis of the opener and a small next-track candidate window, rather than indexing an entire streaming catalog. Confirm provider request and play-count rules. A metadata-only shortlist requires the missing-feature ranking work above.

Branch B — playback controlled by the provider SDK, but PCM is not exposed or disko DSP is prohibited:
- Determine whether the approved SDK itself offers two decks, timing, beatmatching, effects, mixing and sufficient control. If yes, build a separate playback adapter and restrict features to those it actually supports; do not route protected streams through `live/audio.py` or existing PCM rendering.
- If it exposes only single-track playback, it cannot deliver disko's existing beatmatched transition engine. Treat that as a product change/go/no-go decision, not a transparent fallback.

If incremental streaming is required and authorized, design a bounded, seekable buffer and stateful DSP separately; this is more work than replacing `decode()`. Keep network, decoding, model inference and transition generation off `Engine._callback`. Define underflow behavior, sample-position semantics, seek-generation cancellation, expiry and no-network behavior. Preserve handoff and heard-position accounting. Never imply seamless playback until measured.

Acceptance: demonstrate two consecutive authorized tracks and one transition under the actual SDK contract, with no callback network/analysis work, no prohibited persistence, and defined behavior on failure. Without partner access, use only synthetic sources for this milestone and label results accordingly.

## Milestone 3: Beatport adapter and session UX

Only after Gate 0:
1. Implement the documented partner SDK and OAuth flow in an isolated provider module. Use provider-approved secure credential storage, token refresh and revocation; never put tokens in URLs, logs, prompts or the repository. Request minimum scopes. Make source registration and use opt-in.
2. Add connect/disconnect, account/entitlement state, playlist selection, bounded pagination and track availability in the PySide interface. Explain subscription and account requirements. Use official branding rules.
3. Build a bounded candidate pool from a selected playlist or permitted discovery query. Keep explicit unavailable/territory/rights errors and ensure an unavailable track is skipped, not endlessly retried. Do not assume genre metadata supplies energy or beats.
4. Prepare enough material ahead of the current track to cover acquisition + permitted analysis + Kev decisions + rendering. Measure actual timings; use cancellation and bounded concurrency. On delay or entitlement expiry, continue only where authorized, then pause or stop gracefully and explain what happened. Do not assume a queued stream remains authorized after logout.
5. Keep the local-folder source as the default fallback; never silently replace the user's choice of source or credentials. Close provider handles and remove only the artifacts the agreement requires on disconnect.

Acceptance: with an explicitly approved test account, a listener can sign in, choose an eligible playlist, start a set, hear at least one authorized transition, steer energy, stop, and disconnect; failure and policy events are visible. Local-only mode remains functional.

## Milestone 4: Hermes controller (optional, separate)

First extract a session controller from the UI-driven orchestration in `live/ui/window.py`; make Qt thread ownership explicit. Provide a local, authenticated IPC interface or another reviewed control boundary for start/status/pause/stop/energy/pick-again. Restrict access to the local user, do not expose network controls by default, and serialize state-changing requests. Hermes runs outside the real-time audio path and uses status/events, not fabricated explanations.

A small native Hermes plugin can call the approved disko control interface and bundle a skill explaining setup and supported commands. Starting a session through Hermes must surface account selection, track-source permissions and playback location. If Hermes runs in Docker or on a remote host, the Mac running disko needs an explicit, authenticated transport; localhost in the container is not the Mac. No remote playback or audio delivery is implied.

Acceptance: with a fake/local source first, `start`, `status`, `set energy`, `pause` and `stop` are reliable and safe after disconnect/restart. Add a Beatport end-to-end scenario only after authorization. Ship this phase separately if it does not block the core DJ product.

## Verification matrix

- Provider contract: no unauthorized raw audio, persistence, ML calls, upload, recording or export; tests assert deny-before-use and cleanup on expiry/disconnect/crash where feasible.
- Existing local source: fixtures preserve scan, IDs, fast/deep results, ranking and transition behavior; no unintended cache migration.
- Engine: synthetic PCM and `Engine(audio=False).advance()` test exact handoff, seek, cancellation, pause, ending and snapshots; no sound device required.
- Fault injection: throttling, timeout, transient outage, invalid/expired token, unavailable/region-restricted track, seek cancellation, decode error, worker failure, concurrent requests and process shutdown.
- Resource/real-time: measured memory bounds, bounded queues, preparation deadline misses, buffer underflow and UI responsiveness. Use test audio and a controllable fake clock/source; run long-session soak tests on Apple Silicon.
- Partner-approved end-to-end: only after access, using a permitted account and music. Record platform, app version, subscription tier, permitted capabilities, evidence of cleanup and actual audible results. Do not commit audio, tokens or partner-confidential SDK material.

## Suggested ordering and decisions

1. Submit the authorization/SDK questions and obtain written terms (external blocker).
2. In parallel, implement local/fake source abstractions and regression tests using independently licensed fixtures.
3. Run a small authorized feasibility spike on the actual SDK; choose Branch A, Branch B, or stop. Do not pre-commit to PCM availability or a UI architecture.
4. Build only the approved Beatport feature set, measure preparation and failure behavior, then add the Hermes controller.

Existing constraints remain: Apple Silicon/MLX and multiple Python environments; separately review Essentia model licenses and all third-party model/SDK licenses. Budget, timelines, SDK availability and operational costs are unknown until the partner responds.

## References

- Beatport general/API/streaming/AI terms: https://support.beatport.com/hc/en-us/articles/4414997837716-Terms-and-Conditions
- Beatport streaming tiers and supported integrations: https://stream.beatport.com/
- Beatport AI statement: https://support.beatport.com/hc/en-us/articles/52381231418004-Beatport-s-Stance-on-Artificial-Intelligence
- Serato's Beatport FAQ (partner-specific analysis and recording behavior): https://support.serato.com/hc/en-us/articles/360001929056-Beatport-Streaming-FAQ
- Hermes plugin guide: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
