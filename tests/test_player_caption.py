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


def test_filter_restart_does_not_consume_next_caption():
    cfg = SimpleNamespace(display_width=1920, display_height=1080, matte_blur_sigma=20)
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
