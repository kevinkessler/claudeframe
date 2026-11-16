from types import SimpleNamespace

from claudeframe.player import Player


class FakeClient:
    def __init__(self, player):
        self.player = player
        self.async_commands = []
        self.loaded = []

    def set_property(self, *args):
        pass

    def command(self, *args):
        if args[:2] == ("vf", "set"):
            # mpv may restart playback of the current file when its filter is
            # replaced. This event must not consume the next file's caption.
            self.player._on_event({"event": "playback-restart"})

    def command_async(self, *args):
        self.async_commands.append(args)

    def loadfile(self, path, mode):
        self.loaded.append((path, mode))

    def close(self):
        pass


def test_filter_restart_does_not_consume_next_caption():
    cfg = SimpleNamespace(display_width=1920, display_height=1080, matte_blur_sigma=20, mpv_vo="gpu", mpv_hwdec="no")
    player = Player(cfg)
    client = FakeClient(player)
    player._client = client
    item = SimpleNamespace(kind="image", path="/photos/next.jpg")

    player.show(item, loop=False, caption="next caption")

    assert player._pending_caption == "next caption"
    assert client.loaded == [(item.path, "replace")]

    # Another playback restart still must not consume it; only loading the
    # requested media establishes which caption belongs on screen.
    player._on_event({"event": "playback-restart"})
    assert player._pending_caption == "next caption"

    player._on_event({"event": "file-loaded"})

    assert player._pending_caption is None
    assert ("set_property", "osd-msg1", "next caption") in client.async_commands


def test_render_ack_requires_file_loaded_then_first_playback_restart():
    cfg = SimpleNamespace(display_width=1920, display_height=1080, matte_blur_sigma=20, mpv_vo="gpu", mpv_hwdec="no")
    player = Player(cfg)
    client = FakeClient(player)
    player._client = client
    item = SimpleNamespace(kind="image", path="/photos/next.jpg")

    generation = player.show(item, loop=False, caption="caption")
    assert player.wait_rendered(0.001, generation=generation) is False

    player._on_event({"event": "file-loaded"})
    assert player.wait_rendered(0.001, generation=generation) is False

    player._on_event({"event": "playback-restart"})
    assert player.wait_rendered(0.001, generation=generation) is True


def test_restart_cancels_timed_out_generation_before_late_event_and_next_load():
    cfg = SimpleNamespace(display_width=1920, display_height=1080, matte_blur_sigma=20, mpv_vo="gpu", mpv_hwdec="no")
    player = Player(cfg)
    client = FakeClient(player)
    player._client = client
    first = SimpleNamespace(kind="image", path="/photos/a.jpg")
    second = SimpleNamespace(kind="image", path="/photos/b.jpg")

    first_generation = player.show(first, loop=False)
    player._on_event({"event": "file-loaded"})
    assert player.wait_rendered(0.001, generation=first_generation) is False

    # Exercise restart's state reset without spawning a real mpv process.
    old_client_generation = player._client_generation
    player.start = lambda: None
    player.restart()
    player._client = FakeClient(player)
    second_generation = player.show(second, loop=False)

    player._on_event(
        {"event": "playback-restart"},
        client_generation=old_client_generation,
    )  # late first-load event from the closed IPC reader
    assert player.wait_rendered(0.001, generation=second_generation) is False
    player._on_event({"event": "file-loaded"})
    player._on_event({"event": "playback-restart"})
    assert player.wait_rendered(0.001, generation=second_generation) is True


def test_stale_callback_from_replaced_client_cannot_acknowledge_current_load():
    cfg = SimpleNamespace(display_width=1920, display_height=1080, matte_blur_sigma=20, mpv_vo="gpu", mpv_hwdec="no")
    player = Player(cfg)
    old_client = FakeClient(player)
    new_client = FakeClient(player)
    player._client = new_client
    player._client_generation = 7
    item = SimpleNamespace(kind="image", path="/photos/current.jpg")

    generation = player.show(item, loop=False, caption="current")
    player._on_event(
        {"event": "file-loaded"},
        client_generation=7,
        client=old_client,
    )
    player._on_event(
        {"event": "playback-restart"},
        client_generation=7,
        client=old_client,
    )

    assert player.wait_rendered(0.001, generation=generation) is False
    assert player._pending_caption == "current"

    player._on_event(
        {"event": "file-loaded"},
        client_generation=7,
        client=new_client,
    )
    player._on_event(
        {"event": "playback-restart"},
        client_generation=7,
        client=new_client,
    )
    assert player.wait_rendered(0.001, generation=generation) is True


def test_mail_overlay_refresh_ignores_older_hide_generation():
    cfg = SimpleNamespace(display_width=1920, display_height=1080, matte_blur_sigma=20, mpv_vo="gpu", mpv_hwdec="no")
    player = Player(cfg)
    client = FakeClient(player)
    player._client = client

    player.show_mail_icon(duration=60)
    old_generation = player._mail_overlay_generation
    player.show_mail_icon(duration=60)
    new_generation = player._mail_overlay_generation
    player._hide_mail_icon(old_generation)

    removals = [cmd for cmd in client.async_commands if cmd[:3] == ("osd-overlay", 7719, "none")]
    assert removals == []

    player._hide_mail_icon(new_generation)
    removals = [cmd for cmd in client.async_commands if cmd[:3] == ("osd-overlay", 7719, "none")]
    assert len(removals) == 1


class RecordingClient(FakeClient):
    def __init__(self, player):
        super().__init__(player)
        self.props = []
        self.filters = []

    def set_property(self, name, value):
        if name in ("vo", "hwdec"):
            self.props.append((name, value))

    def command(self, *args):
        if args[:2] == ("vf", "set"):
            self.filters.append(args[2])


def test_output_switches_to_zero_copy_only_for_flagged_videos_and_back():
    cfg = SimpleNamespace(display_width=3840, display_height=2160, matte_blur_sigma=20, mpv_vo="gpu", mpv_hwdec="no")
    player = Player(cfg)
    client = RecordingClient(player)
    player._client = client
    player._output = ("gpu", "no")  # as set by start()
    image = SimpleNamespace(kind="image", path="/p/a.jpg")
    video = SimpleNamespace(kind="video", path="/p/v.mp4")

    player.show(image, loop=False)
    player.show(image, loop=False)
    assert client.props == []  # no vo reinit between pictures

    player.show(video, loop=False, zero_copy=True)
    assert client.props == [("hwdec", "v4l2m2m"), ("vo", "dmabuf-wayland")]

    player.show(image, loop=False)
    assert client.props[2:] == [("hwdec", "no"), ("vo", "gpu")]

    player.show(video, loop=False, zero_copy=False)
    assert len(client.props) == 4  # already on the GL output
    # Pictures get the matte filter; videos never get a CPU filter.
    assert [bool(f) for f in client.filters] == [True, True, False, True, False]
