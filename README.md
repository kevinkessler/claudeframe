# claudeframe

A random slideshow of pictures and short videos for a Raspberry Pi digital frame.

Replacement for [picframe](https://github.com/helgeerbe/picframe) that adds video support.

## Target hardware

- Raspberry Pi 3 Model B (1 GB RAM), Raspbian Buster
- HDMI display (tested at 1680×1050)
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
    python3-flask python3-yaml python3-pyinotify python3-pil
```

Deploy from dev machine:

```bash
make deploy          # rsync to /home/pi/claudeframe
make enable start    # install + start user systemd unit
make tail            # follow logs
```

## Configuration

`config/claudeframe.yaml` on the Pi. See `config/claudeframe.example.yaml` for the full set.

Inherits defaults where sensible from the existing `~/picframe_data/config/configuration.yaml`.

## Web UI

Open `http://pictureframe.lan:8080/` on your LAN. No auth. Pause/resume, next/prev, ban current, rescan.

## Rollback

The old `picframe.service` is preserved until `make disable-old-picframe` is run. To go back:

```bash
systemctl --user stop claudeframe.service
systemctl --user disable claudeframe.service
systemctl --user enable picframe.service
systemctl --user start picframe.service
```
