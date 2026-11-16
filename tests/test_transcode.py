import os
import shutil
import subprocess

import pytest

from claudeframe import transcode
from claudeframe.transcode import VideoInfo

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.mark.parametrize("info,ok", [
    (VideoInfo("h264", 1920, 1080, "yuv420p", 10.0), True),
    (VideoInfo("h264", 1080, 1920, "yuvj420p", 10.0), True),   # portrait phone clip
    (VideoInfo("h264", 3840, 2160, "yuv420p", 10.0), False),   # above 1080p
    (VideoInfo("h264", 1920, 1080, "yuv420p10le", 10.0), False),
    (VideoInfo("mjpeg", 640, 480, "yuvj422p", 10.0), False),
    (VideoInfo("hevc", 1920, 1080, "yuv420p", 10.0), False),
])
def test_hw_decodable(info, ok):
    assert transcode.hw_decodable(info) is ok


def _make_clip(path, codec_args, size="640x480"):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", *codec_args, str(path)], check=True)


@needs_ffmpeg
def test_mjpeg_is_converted_beside_original_and_original_backed_up(tmp_path):
    lib, backup = tmp_path / "Frame", tmp_path / "Deleted"
    src = lib / "2009-Q1" / "DSCN0052.AVI"
    src.parent.mkdir(parents=True)
    _make_clip(src, ["-c:v", "mjpeg", "-c:a", "pcm_u8"])
    os.utime(src, (1_000_000_000, 1_000_000_000))
    info = transcode.probe(str(src))
    assert info.codec == "mjpeg" and not transcode.hw_decodable(info)

    new = transcode.convert(str(src), info, str(lib), str(backup))

    assert new == str(lib / "2009-Q1" / "DSCN0052.mp4")
    assert transcode.hw_decodable(transcode.probe(new))
    assert os.stat(new).st_mtime == 1_000_000_000
    assert not src.exists()
    assert (backup / "2009-Q1" / "DSCN0052.AVI").is_file()
    assert sorted(os.listdir(src.parent)) == ["DSCN0052.mp4"]  # temp file cleaned up


@needs_ffmpeg
def test_oversized_h264_with_same_name_is_downscaled_in_place(tmp_path):
    lib, backup = tmp_path / "Frame", tmp_path / "Deleted"
    src = lib / "trip" / "big.mp4"
    src.parent.mkdir(parents=True)
    _make_clip(src, ["-c:v", "libx264", "-preset", "ultrafast"], size="2560x1440")

    new = transcode.convert(str(src), transcode.probe(str(src)), str(lib), str(backup))

    assert new == str(src)
    out = transcode.probe(new)
    assert (out.width, out.height) == (1920, 1080)
    assert transcode.probe(str(backup / "trip" / "big.mp4")).width == 2560


@needs_ffmpeg
def test_failed_conversion_leaves_original_untouched(tmp_path, monkeypatch):
    lib, backup = tmp_path / "Frame", tmp_path / "Deleted"
    src = lib / "a" / "clip.avi"
    src.parent.mkdir(parents=True)
    _make_clip(src, ["-c:v", "mjpeg"])
    info = transcode.probe(str(src))
    monkeypatch.setattr(transcode, "hw_decodable", lambda info: False)  # reject the output

    with pytest.raises(RuntimeError):
        transcode.convert(str(src), info, str(lib), str(backup))

    assert src.is_file()
    assert os.listdir(src.parent) == ["clip.avi"]
    assert not backup.exists()


def test_existing_destination_is_never_overwritten(tmp_path):
    src = tmp_path / "clip.avi"
    src.write_bytes(b"x")
    (tmp_path / "clip.mp4").write_bytes(b"other")
    with pytest.raises(FileExistsError):
        transcode.convert(str(src), VideoInfo("mjpeg", 640, 480, "yuvj422p", 1.0), str(tmp_path), str(tmp_path / "b"))
    assert (tmp_path / "clip.mp4").read_bytes() == b"other"


@needs_ffmpeg
def test_rotated_phone_clip_keeps_portrait_resolution(tmp_path):
    lib, backup = tmp_path / "Frame", tmp_path / "Deleted"
    src = lib / "2019" / "phone.mp4"
    src.parent.mkdir(parents=True)
    # Stored landscape 2560x1440 with a 90-degree display rotation, like phone video.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=2560x1440:rate=30",
                    "-t", "1", "-c:v", "libx264", "-preset", "ultrafast", str(tmp_path / "flat.mp4")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(tmp_path / "flat.mp4"),
                    "-c", "copy", str(src)], check=True)

    new = transcode.convert(str(src), transcode.probe(str(src)), str(lib), str(backup))

    out = transcode.probe(new)
    assert (out.width, out.height) == (1080, 1920)
