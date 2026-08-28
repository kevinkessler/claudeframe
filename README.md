# claudeframe

A random slideshow of pictures and short videos for a Raspberry Pi digital frame.

Replacement for [picframe](https://github.com/helgeerbe/picframe) that adds video support.

## Target hardware

- Raspberry Pi 3 Model B (1 GB RAM), Raspbian Buster (original frame)
- Raspberry Pi 4, Debian 13 (button-equipped frame)
- HDMI display (tested at 1680×1050 and 4096×2160)
- Fake-KMS (`dtoverlay=vc4-fkms-v3d`) enabled for DRM/KMS output
- Source tree of photos/videos under `/home/pi/Frame/` (typically symlinks into a CIFS-mounted NAS)

## Architecture

Two processes, one controller:

- **`mpv`** — fullscreen renderer via `--vo=gpu` inside the existing X session (lightdm owns DRM master, so direct `--vo=drm` is denied). Displays images and videos; hardware H.264 decode via V4L2 M2M. Controlled by JSON IPC over a Unix socket.
- **`claudeframe`** — Python 3 daemon that indexes media, picks the next slide, drives mpv, serves a small Flask web UI on `:8080`.

For each slide:

- **Image**: shown for 20 s
- **Video ≥ 20 s**: played for exactly 20 s, then cut
- **Video < 20 s**: looped until 20 s have elapsed, then cut
- Caption (filename · folder name · IPTC/XMP/EXIF description) overlaid via mpv OSD
- Brief fade-to-black between slides

## Install on the Pi

```bash
sudo apt-get install mpv libimage-exiftool-perl \
    python3-flask python3-yaml python3-pyinotify python3-pil \
    python3-gpiozero python3-requests
```

Deploy from dev machine:

```bash
make deploy          # original frame: rsync to /home/pi/claudeframe
make enable start    # install + start original user systemd unit
make tail            # follow original-frame logs

make deploy-frame2 restart-frame2  # Pi 4 frame at kevin@192.168.40.99
make logs-frame2                    # inspect Pi 4 service logs
```

## Configuration

`config/claudeframe.yaml` on the Pi. See `config/claudeframe.example.yaml` for the full set.

Inherits defaults where sensible from the existing `~/picframe_data/config/configuration.yaml`.

## Physical controls (Pi 4 frame)

BCM GPIO22 goes to Previous, GPIO27 flags the displayed picture for review, and
GPIO17 goes to Next. All switches use pull-ups and a shared ground. Physical
buttons default off so the original frame remains unchanged; set
`buttons_enabled: true` only in the Pi 4 frame's live `claudeframe.yaml`. The
private Home Assistant endpoint is loaded only from
`PICTURE_FRAME_HA_WEBHOOK_URL` in the frame's systemd environment file.

## Web UI

Open the frame's port 8080 on your LAN. No auth. Pause/resume, next/prev, ban
current, rescan. Next/Previous use the same committed display history as the
physical buttons.

## Rollback

The old `picframe.service` is preserved until `make disable-old-picframe` is run. To go back:

```bash
systemctl --user stop claudeframe.service
systemctl --user disable claudeframe.service
systemctl --user enable picframe.service
systemctl --user start picframe.service
```
