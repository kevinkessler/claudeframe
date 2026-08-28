from claudeframe.history import DisplayHistory


def test_history_allows_exactly_ten_back_operations():
    history = DisplayHistory(max_entries=11)
    for value in range(11):
        history.commit_new(value)

    visited = []
    while history.back_candidate() is not None:
        visited.append(history.back_candidate())
        history.commit_back()

    assert len(visited) == 10
    assert history.current() == 0
    assert history.back_candidate() is None


def test_forward_traverses_existing_history_before_new_entry():
    history = DisplayHistory(max_entries=11)
    for value in ("a", "b", "c"):
        history.commit_new(value)
    history.commit_back()
    history.commit_back()

    assert history.forward_candidate() == "b"
    history.commit_forward()
    assert history.current() == "b"
    assert history.forward_candidate() == "c"


def test_oldest_entry_is_trimmed_only_after_newest_append():
    history = DisplayHistory(max_entries=11)
    for value in range(11):
        history.commit_new(value)
    history.commit_new(11)

    assert history.items() == list(range(1, 12))
    assert history.cursor == 10


def test_removing_current_entry_preserves_forward_navigation():
    history = DisplayHistory(max_entries=11)
    for value in ("banned", "next"):
        history.commit_new(value)
    history.commit_back()

    history.remove_where(lambda value: value == "banned")

    assert history.current() is None
    assert history.forward_candidate() == "next"
    history.commit_forward()
    assert history.current() == "next"


def test_duplicate_paths_remain_distinct_display_events():
    history = DisplayHistory(max_entries=11)
    history.commit_new("same.jpg")
    history.commit_new("same.jpg")

    assert history.items() == ["same.jpg", "same.jpg"]
    history.commit_back()
    assert history.current() == "same.jpg"
    assert history.cursor == 0
