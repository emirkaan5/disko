---
name: disko-dj
description: Run and steer a disko AI DJ set on the user's Mac (start, status, energy, pick again, pause, stop)
version: 0.1.0
license: AGPL-3.0
platforms: [macos]
metadata:
  hermes:
    tags: [Music, DJ, Local]
    requires_tools: [disko_status, disko_start]
---
# disko DJ

disko is a desktop AI DJ. Its model, Kev, picks every next track and plans the mix into it;
the music plays out loud on the Mac running disko. These tools steer it; they don't play audio
anywhere else.

## When to Use
The user asks to start, check on, steer (more or less energy, a different next track), pause or
stop a DJ set, or asks what is playing.

## Quick Reference
- `disko_status`: state, now playing, what's queued next, energy, sources and their permissions, playback location
- `disko_find_tracks(query)`: playable tracks, to choose an opener
- `disko_start(track?)`: start the set (Kev opens on the calmest track if none is given)
- `disko_set_energy("down" | "hold" | "up")`, `disko_pick_again()`
- `disko_pause()`, `disko_resume()`, `disko_stop()`

## Procedure
1. Call `disko_status` first. If it says disko isn't running, tell the user to start it on the Mac:
   `.venv-live/bin/python -m live --playlist ~/Music/SomeFolder --control` (window) or
   `... -m live --headless --playlist ~/Music/SomeFolder` (no window).
2. Before `disko_start`, confirm with the user. Tell them which library and sources it will use
   (`sources`, with what disko is permitted to do with each) and that it plays on that Mac
   (`playback`).
3. After any change, call `disko_status` and report only what it says. Kev's reasons are not
   exposed here; don't invent explanations for its picks.

## Pitfalls
- Only report state from tool results. A `notice` in the status is disko's own message; quote it.
- An `error` from a tool means nothing changed. Don't retry a refused command in a loop.
- If Hermes runs in a container, `localhost` is the container: disko must be started with
  `--control-tcp HOST:PORT`, and DISKO_CONTROL_URL and DISKO_CONTROL_TOKEN set for Hermes.

## Verification
`disko_status` shows `state` = `playing`, `queued` or `mixing` while a set runs, and `stopped`
after `disko_stop`.
