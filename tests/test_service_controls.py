from __future__ import annotations
import threading
import time
from queue import Queue

from claudeframe.config import Config
from claudeframe.history import DisplayHistory
from claudeframe.indexer import KIND_IMAGE, MediaItem
from claudeframe.service import CONTROL_NEXT, Service


def _item(name: str) -> MediaItem:
    return MediaItem(
        path=f"/fake/{name}.jpg",
        kind=KIND_IMAGE,
        folder="fake",
        display_name=name,
        description=None,
        mtime=0.0,
    )


class FakePlayer:
    def __init__(self, rendered=True):
        self.rendered = list(rendered) if isinstance(rendered, (list, tuple)) else rendered
        self.shown = []
        self.overlay_calls = 0
        self.restart_calls = 0
        self._generation = 0

    def show(self, item, loop=False, caption=""):
        self.shown.append(item)
        self._generation += 1
        return self._generation

    def wait_rendered(self, timeout, generation=None, stop_event=None):
        if isinstance(self.rendered, list):
            return self.rendered.pop(0)
        return self.rendered

    def restart(self):
        self.restart_calls += 1

    def is_alive(self):
        return True

    def show_mail_icon(self, duration=2.0):
        self.overlay_calls += 1


class FakeScheduler:
    def __init__(self, items=()):
        self.items = list(items)
        self.calls = 0

    def next(self):
        self.calls += 1
        return self.items.pop(0) if self.items else None

    def invalidate(self):
        pass


class FakeIndexer:
    def __init__(self):
        self.banned = []

    def ban(self, path):
        self.banned.append(path)


class FakeButtons:
    def __init__(self):
        self.started = threading.Event()

    def start(self):
        self.started.set()
        return True


class FakeNotifier:
    def __init__(self):
        self.jobs = []

    def submit(self, job):
        self.jobs.append(job)
        return True


def _service(*, scheduler=(), rendered=True, slide_seconds=0.1) -> Service:
    svc = object.__new__(Service)
    svc.config = Config(slide_seconds=slide_seconds, render_timeout=0.1)
    svc.player = FakePlayer(rendered=rendered)
    svc.scheduler = FakeScheduler(scheduler)
    svc.indexer = FakeIndexer()
    svc.history = DisplayHistory(max_entries=11)
    svc.notifier = FakeNotifier()
    svc._notifier_available = True
    svc._buttons_started = False
    svc._dwell_deadline = None
    svc.buttons = None
    svc._current = None
    svc._current_ts = 0.0
    svc._paused = threading.Event()
    svc._advance = threading.Event()
    svc._controls = Queue(maxsize=32)
    svc._rescan = threading.Event()
    svc._stop = threading.Event()
    svc._lock = threading.RLock()
    return svc


def _commit(svc: Service, *items: MediaItem) -> None:
    for item in items:
        svc.history.commit_new(item)
    svc._current = items[-1]


def test_right_after_back_uses_forward_history_before_scheduler():
    a, b, c = _item("a"), _item("b"), _item("c")
    svc = _service(scheduler=[_item("new")])
    _commit(svc, a, b, c)
    svc.history.commit_back()
    svc._current = b

    shown = svc._show_navigation(CONTROL_NEXT)

    assert shown is c
    assert svc.history.current() is c
    assert svc.scheduler.calls == 0


def test_automatic_advance_uses_forward_history_before_new_selection():
    a, b, c = _item("a"), _item("b"), _item("c")
    svc = _service(scheduler=[_item("new")], slide_seconds=0.0)
    _commit(svc, a, b, c)
    svc.history.commit_back()
    svc._current = b

    control = svc._dwell(b)
    shown = svc._show_navigation(control)

    assert control == CONTROL_NEXT
    assert shown is c
    assert svc.scheduler.calls == 0


def test_automatic_advance_at_newest_selects_and_commits_new_item():
    a, b = _item("a"), _item("b")
    svc = _service(scheduler=[b], slide_seconds=0.0)
    _commit(svc, a)

    control = svc._dwell(a)
    shown = svc._show_navigation(control)

    assert shown is b
    assert svc.history.items() == [a, b]
    assert svc.scheduler.calls == 1


def test_failed_render_does_not_change_current_or_history():
    a, b = _item("a"), _item("b")
    svc = _service(scheduler=[b], rendered=[False, True])
    _commit(svc, a)

    assert svc._show_navigation(CONTROL_NEXT) is None
    assert svc._current is a
    assert svc.history.items() == [a]
    assert svc.history.cursor == 0
    assert svc.player.restart_calls == 1
    assert svc.player.shown == [b, a]  # timed-out candidate, then restored commit


def test_timed_out_load_is_cancelled_before_a_later_request_commits():
    a, b, c = _item("a"), _item("b"), _item("c")
    svc = _service(scheduler=[b, c], rendered=[False, True, True])
    _commit(svc, a)

    assert svc._show_navigation(CONTROL_NEXT) is None
    assert svc.player.restart_calls == 1
    assert svc._current is a

    # A late acknowledgement belongs to the pre-restart client and cannot
    # satisfy this new request; the next request uses its own token.
    assert svc._show_navigation(CONTROL_NEXT) is c
    assert svc._current is c
    assert svc.history.items() == [a, c]


def test_buttons_start_only_after_first_successful_render_commit():
    first = _item("first")
    svc = _service(scheduler=[first], rendered=False)
    svc.buttons = FakeButtons()

    assert svc._show_navigation(CONTROL_NEXT) is None
    assert svc.buttons.started.wait(0.02) is False

    svc.player.rendered = True
    svc.scheduler.items.append(first)
    assert svc._show_navigation(CONTROL_NEXT) is first
    assert svc.buttons.started.wait(0.2) is True


def test_flag_snapshots_committed_picture_before_later_navigation():
    a, b = _item("a"), _item("b")
    svc = _service()
    _commit(svc, a)

    svc._accept_flag()
    svc._current = b

    assert len(svc.notifier.jobs) == 1
    assert svc.notifier.jobs[0].rendered_path == a.path
    assert svc.notifier.jobs[0].timestamp.utcoffset() is not None
    deadline = time.monotonic() + 0.2
    while svc.player.overlay_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert svc.player.overlay_calls == 1


def test_repeated_flag_controls_create_separate_jobs_for_same_picture():
    a = _item("a")
    svc = _service(slide_seconds=0.0)
    _commit(svc, a)
    svc.request_flag()
    svc.request_flag()

    assert svc._dwell(a) == CONTROL_NEXT
    assert [job.rendered_path for job in svc.notifier.jobs] == [a.path, a.path]


def test_ignored_back_does_not_restart_or_end_current_dwell():
    a = _item("a")
    svc = _service(slide_seconds=0.15)
    _commit(svc, a)
    svc.request_prev()

    started = time.monotonic()
    control = svc._dwell(a)
    elapsed = time.monotonic() - started

    assert control == CONTROL_NEXT
    assert elapsed >= 0.12


def test_failed_manual_navigation_preserves_existing_dwell_deadline():
    a, b = _item("a"), _item("b")
    svc = _service(scheduler=[b], rendered=[False, True], slide_seconds=0.15)
    _commit(svc, a)
    svc.request_next()

    assert svc._dwell(a) == CONTROL_NEXT
    original_deadline = svc._dwell_deadline
    time.sleep(0.07)
    assert svc._show_navigation(CONTROL_NEXT) is None

    started = time.monotonic()
    assert svc._dwell(a, deadline=original_deadline) == CONTROL_NEXT
    assert time.monotonic() - started < 0.11


def test_successful_manual_navigation_starts_a_fresh_dwell():
    a, b = _item("a"), _item("b")
    svc = _service(scheduler=[b], slide_seconds=0.12)
    _commit(svc, a)
    svc.request_next()

    started = time.monotonic()
    control = svc._dwell(a)
    assert time.monotonic() - started < 0.08
    assert svc._show_navigation(control) is b

    started = time.monotonic()
    assert svc._dwell(b) == CONTROL_NEXT
    assert time.monotonic() - started >= 0.09


def test_ban_removes_all_matching_paths_from_committed_history():
    a, banned, c = _item("a"), _item("banned"), _item("c")
    duplicate = _item("banned")
    svc = _service(scheduler=[c], slide_seconds=0.0)
    _commit(svc, a, banned, duplicate)

    svc.ban_current()
    control = svc._dwell(duplicate)
    assert control == CONTROL_NEXT
    assert svc.indexer.banned == [duplicate.path]
    assert [item.path for item in svc.history.items()] == [a.path]

    assert svc._show_navigation(control) is c
    assert svc.history.back_candidate() is a
