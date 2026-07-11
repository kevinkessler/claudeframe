# claudeframe — Maintainer Notes for LLMs

Operational knowledge for maintaining this app, accumulated while building and
installing it. Facts here were true as of 2026-07; verify against current code
before acting on file/line specifics. See also `docs/notes-2026-04-22.md` for
the original session notes and `README.md` for the user-facing overview.

## What this is

`claudeframe` is a random photo+video slideshow for a Raspberry Pi digital
picture frame. It replaced [picframe](https://github.com/helgeerbe/picframe)
because picframe has no video support. Two processes:

- **mpv** — fullscreen renderer, controlled over JSON IPC on a Unix socket
  (`/tmp/claudeframe-mpv.sock`).
- **claudeframe** — Python 3 daemon (`python3 -m claudeframe`): indexes media,
  schedules slides, drives mpv, serves a Flask web UI on port 8080.

No external Python deps beyond distro packages: `flask`, `yaml`, `pyinotify`,
`PIL`, plus `mpv` and `exiftool` binaries.

## Hardware

### Current production frame

- Raspberry Pi 3 Model B (1 GB RAM), Raspbian Buster, user `pi`
- HDMI display at 1680×1050
- Fake-KMS (`dtoverlay=vc4-fkms-v3d`); lightdm owns DRM master
- **SSH: `pi@192.168.132.89` — always use the IP.** mDNS/DNS for
  `pictureframe.lan` / `pictureframe.local` is unreliable on this network.

### Pi 4 rebuild design notes (researched 2026-07)

- Raspberry Pi 4 + NV156QUM 15.6" 4K (3840×2160) panel
- LCD controller: VS-RT2795T4K-V1 (HDMI/mini-DP in, 30-pin eDP out, needs
  **12 V DC ≥ 2 A** — do not feed it 15/20 V)
- Single-cable power plan: 65 W USB-C PD charger → PD trigger cable fixed at
  12 V (verify charger offers the optional 12 V rung) → barrel Y-splitter →
  controller board + 12 V→5 V buck for the Pi. Recommended buck: Pololu
  D36V50F5 (5 V/5.5 A). Pi 4 browns out below ~4.63 V, so keep the 5 V run
  short (<15 cm) and thick (18 AWG+); avoid MP1584/LM2596 modules.
- When rebuilding: `display_width`/`display_height` in config must change to
  3840×2160, and the VC4 2048-texture-limit workaround (see mpv section) may
  behave differently on the Pi 4's GPU — retest large images.

### Pi 4 frame installed 2026-07-11

- SSH: `kevin@192.168.40.99`
- Debian 13 (trixie), arm64; desktop/Xwayland display `:0` currently reports
  4096×2160.
- App: `/home/kevin/claudeframe`; user service enabled as
  `claudeframe.service`; web UI at `http://192.168.40.99:8080/`.
- NAS is mounted at `/home/kevin/Pictures`. The slideshow root is
  `/home/kevin/Frame`, containing the 120 selection symlinks copied from the
  production frame with targets rewritten from `/home/pi/Pictures/...` to
  `/home/kevin/Pictures/...`. Do not confuse it with the real NAS directory
  `/home/kevin/Pictures/Frame`.
- Live config uses `display_width: 4096`, `display_height: 2160`,
  `mpv_hwdec: auto-safe`, and `mpv_gpu_api: opengl`.

## Storage layout (critical context)

Media is **not local**. `pic_dir: /home/pi/Frame` contains symlinks into a
CIFS share `//fileserver.lan/Photos` (NAS at 192.168.132.148), automounted at
`/home/pi/Pictures` via `/etc/fstab` (`x-systemd.automount`, credentials in
`/home/pi/.freenascredentials`).

Consequences:

- A slow/degraded NAS is the #1 cause of slideshow failures (see failure
  modes below).
- `pyinotify` watching `/home/pi/Frame` only sees **local** filesystem events.
  Changes made on the NAS from another machine do not trigger a rescan; use
  the web UI "Rescan" button or wait for a restart.

## Deployment & operations

All from the dev machine (`/home/kevin/Documents/AI/PictureFrame`):

```bash
make deploy          # rsync code+config+unit to pi@192.168.132.89:~/claudeframe
make restart         # restart the systemd user unit
make status / logs / tail
make test            # pytest locally (tests/)
```

- Service is a **systemd user unit** (`claudeframe.service`, `WantedBy=default.target`),
  not a system unit. Use `systemctl --user` / `journalctl --user` on the Pi.
- The unit sets `DISPLAY=:0`, `XAUTHORITY=/home/pi/.Xauthority`,
  `MemoryMax=450M`, `Restart=on-failure`.
- Config lives at `/home/pi/claudeframe/config/claudeframe.yaml` on the Pi
  (deployed from `config/`; see `config/claudeframe.example.yaml` for all keys
  and `claudeframe/config.py` for defaults).
- **Cold start takes ~72 s** before the web UI appears: ~7 s directory walk of
  ~5,500 files + ~65 s exiftool metadata refresh. A restart after an mpv/GPU
  problem can take ~2 min. Don't assume a restart failed just because the UI
  is slow to appear.
- Old `picframe.service` is still installed (disabled) as a rollback path.

### HDMI blackout schedule

Cron on the Pi (pi user, timezone America/New_York) blanks the panel at night
via `vcgencmd display_power 0|1` (01:00 off, 06:00 on). The service keeps
running during blackout — slides advance to a dark screen. This is on the Pi's
crontab, not in this repo.

## Architecture map

| File | Role |
|---|---|
| `service.py` | Main loop: `Service.run()`, `_dwell()` (per-slide wait), `_show_next()` (advance + mpv recovery), signal handling |
| `player.py` | `MpvClient` (JSON IPC, reader thread) and `Player` (mpv lifecycle, EOF/stall detection, matte/scale filters) |
| `indexer.py` | Walks `pic_dir`, sqlite cache (`index.sqlite`, keyed by mtime), ban list filtering |
| `metadata.py` | EXIF/IPTC/XMP description via exiftool |
| `scheduler.py` | Shuffle order, reshuffle after N passes |
| `watcher.py` | pyinotify recursive watch, debounced 2 s |
| `webui.py` | Flask app; **imported lazily inside `Service.run()`** so core modules import without flask (needed for tests) |
| `config.py` | Dataclass config; unknown YAML keys are silently ignored |

Slide rules: images show for `slide_seconds` (20 s); videos ≥ 20 s are cut at
20 s; videos < 20 s loop until 20 s elapses. Timing is wall-clock, which
matters because playback is slower than real-time (below).

## mpv quirks (hard-won — do not re-learn these)

All discovered on the Pi 3B with `--keep-open=always`, `--vo=gpu`,
`hwdec=v4l2m2m-copy`, rendering into the lightdm X session (direct `--vo=drm`
is denied because lightdm owns DRM master).

1. **`--keep-open=always` suppresses `end-file`.** At natural EOF mpv pauses
   on the last frame and flips the `eof-reached` property instead. EOF
   detection must observe `property-change eof-reached=true`, not `end-file`.
2. **Pause state persists across `loadfile ... replace`.** After an EOF pause,
   the next file loads frozen at frame 0. Fix: `set_property pause false` from
   the `playback-restart` handler — sending it *before* `loadfile` doesn't
   stick because mpv re-pauses during load.
3. **`eof-reached` flickers true during loadfile transitions.** Gate EOF on a
   `_playing` flag set by `playback-restart` and cleared by `show()`; only the
   first `eof-reached=true` after playback actually started counts.
4. **Never call `MpvClient.command()` from an event handler.** Handlers run on
   the IPC reader thread; a synchronous command blocks waiting for a reply
   that only the reader thread can deliver → 15 s `TimeoutError`. Use
   `command_async()` (fire-and-forget) from event context.
5. **Video plays slower than real-time on the Pi 3B** (~17 s wall for an 11 s
   file with v4l2m2m-copy). The loop-short-videos rule tracks wall-clock, so
   an 11 s file may not loop even though 11 < 20.
6. **VC4 GPU has a 2048-px texture limit.** Images larger than that fail to
   render unless scaled down first — that's what `display_width`/`display_height`
   and the scale filter are for. Also drives the blurred auto-matte for images
   that don't fill the screen (`matte_blur_sigma`).
7. **Pi 4 at 4K must force OpenGL on the current Debian 13/mpv stack.** mpv's
   automatic GPU selection chose Vulkan, then repeatedly logged
   `VK_ERROR_OUT_OF_HOST_MEMORY` while recreating the swapchain. The scheduler
   continued advancing but the panel remained stuck on its first image. Set
   `mpv_gpu_api: opengl` in the Pi's live config. The default remains blank so
   existing installations retain mpv's automatic selection.
8. **Disable mpv's OSC for unattended display.** `Player.start()` passes
   `--no-osc`; disabling default key bindings alone does not suppress the
   playback controls that appear when the pointer is near the bottom edge.
9. **Arm captions after changing the video filter.** The synchronous `vf set`
   in `Player.show()` can emit `playback-restart` for the outgoing file. If the
   next caption is stashed before that command, the outgoing event consumes it
   and captions lag one slide behind. Set `_pending_caption` only after `vf set`
   completes and immediately before `loadfile`. Regression coverage is in
   `tests/test_player_caption.py`.

## Known failure modes and their defenses

### mpv crashes → hot loop → Pi freeze (fixed 2026-05-24, commit f9ce9b0)

mpv died, became a zombie, and its IPC socket vanished. `_show_next()` had no
recovery path and spun on `BrokenPipeError` with no backoff, which froze the
1 GB Pi hard enough to need a power cycle. Now: `_show_next()` checks
`is_alive()` after show failures and restarts mpv; 5 s backoff if the restart
fails, 1 s backoff for transient errors with mpv alive.

**Rule: the Pi 3B cannot tolerate hot loops.** Any new error path needs
backoff.

### Slow NAS stalls videos → slideshow hangs (fixed 2026-06-13, commit 053aaf0)

When the CIFS share degrades (seen: ~1.2 MB/s, dmesg `CIFS: VFS: ...has not
responded in 180 seconds`), images mostly squeak through but a video that
never opens or stalls mid-stream never reaches EOF, and `_dwell()` used to
wait forever — the frame froze for hours. mpv is *alive* in this mode, so the
crash recovery above doesn't trigger.

Now: `_dwell()` polls `player.time_pos()` and skips any video making no
progress for `video_stall_timeout` (default 10 s). Covers failed-open
(time-pos stays None) and mid-stream stalls (frozen position). Tests:
`tests/test_video_watchdog.py`.

Manual unstick if it ever recurs: `curl -X POST http://192.168.132.89:8080/next`.

## Web UI (port 8080, LAN only, no auth)

Endpoints: `GET /` (control page), `GET /state.json`, `GET /current.jpg`,
`POST /pause|/resume|/next|/prev|/ban|/rescan`.

- **Ban** appends the current path to `banned.txt`; filtered at read time, no
  rescan needed.
- **Rescan** sets a flag checked once per dwell iteration, so it runs between
  slides. Incremental (mtime-cached) — cheap.
- `POST /next` is the universal "unstick it" lever.

## Testing

- `make test` runs `tests/test_video_watchdog.py` locally (no Pi, no mpv, no
  flask needed — that's why the webui import is lazy).
- Videos are rare in the library (~20 of ~5,500 files), so you cannot test
  video behavior by mashing `/next`. Use `tools/test_video_dwell.py` on the Pi
  against a known video path; **stop the service first** to free the mpv IPC
  socket.

## Conventions & gotchas for agents

- Deploy is rsync of the working tree — there is no build step and no
  packaging; the Pi runs whatever `make deploy` pushed, which may differ from
  git HEAD. Commit before/after deploying to keep them in sync.
- `Config.load()` silently drops unknown YAML keys — a typo in the config file
  produces defaults, not an error.
- The example config in `config/` is deployed to the Pi, but the live config
  `claudeframe.yaml` on the Pi is *not* in this repo. Check it on the Pi
  before assuming defaults.
- Legacy picframe artifacts still exist on the Pi (`~/picframe_data/`,
  disabled `picframe.service`, caption font is loaded from
  `~/picframe_data/data/fonts/`). Don't delete them; the font path is live.
