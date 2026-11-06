# Beatport integration plan

Status: proposal, not implemented. Repository baseline: 47306415d1b94a9597e5fe75113fd94f1c4e7578.

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
