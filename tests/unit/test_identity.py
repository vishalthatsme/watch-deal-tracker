from watch_tracker.services.identity import canonicalize_url, make_listing_uid


def test_canonicalize_reddit_url_removes_tracking() -> None:
    url = canonicalize_url(
        "reddit",
        "https://old.reddit.com/r/Watchexchange/comments/abc/test/?utm_source=x#noise",
    )
    assert url == "https://www.reddit.com/r/Watchexchange/comments/abc/test"


def test_source_id_is_preferred_for_stable_uid() -> None:
    assert make_listing_uid("reddit", "t3_abc", "https://example.invalid") == "reddit:t3_abc"


def test_url_hash_uid_is_deterministic() -> None:
    first = make_listing_uid("licensed", None, "https://example.invalid/item")
    second = make_listing_uid("licensed", None, "https://example.invalid/item")
    assert first == second
    assert first.startswith("licensed:urlsha256:")
