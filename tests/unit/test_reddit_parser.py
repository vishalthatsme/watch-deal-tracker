from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from watch_tracker.domain import ListingStatus
from watch_tracker.sources.base import SourceAccessError
from watch_tracker.sources.reddit import RedditOAuthSource, parse_post

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _post(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())["data"]


def test_parse_active_single_watch(settings) -> None:
    candidates = parse_post(_post("reddit_active.json"), settings)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_listing_id == "t3_fix001#item:jaeger-lecoultre"
    assert candidate.brand == "Jaeger-LeCoultre"
    assert candidate.reference_number == "Q4018420"
    assert candidate.asking_price_original == Decimal("6250")
    assert candidate.currency == "USD"
    assert candidate.current_status == ListingStatus.ACTIVE
    assert candidate.box_included is True
    assert candidate.papers_included is True


def test_sold_marker_does_not_promote_asking_price(settings) -> None:
    post = _post("reddit_active.json")
    post["title"] = f"{post['title']} [SOLD]"
    candidates = parse_post(post, settings)
    assert candidates[0].current_status == ListingStatus.SOLD
    assert candidates[0].sold_price_original is None


def test_explicit_sold_for_is_captured_separately(settings) -> None:
    post = _post("reddit_active.json")
    post["title"] = f"{post['title']} [SOLD]"
    post["selftext"] += " Sold for $6,000 USD."
    candidate = parse_post(post, settings)[0]
    assert candidate.asking_price_original == Decimal("6250")
    assert candidate.sold_price_original == Decimal("6000")
    assert candidate.sold_price_currency == "USD"


def test_bare_dollar_symbol_is_not_silently_assumed_to_be_usd(settings) -> None:
    post = _post("reddit_active.json")
    post["selftext"] = post["selftext"].replace("$6,250 USD", "$6,250")
    candidate = parse_post(post, settings)[0]
    assert candidate.asking_price_original == Decimal("6250")
    assert candidate.currency is None
    assert "currency" in candidate.missing_information


def test_multiwatch_post_produces_stable_offer_ids(settings) -> None:
    post = _post("reddit_multiwatch.json")
    first = parse_post(post, settings)
    second = parse_post(post, settings)
    assert len(first) == 2
    assert {candidate.brand for candidate in first} == {"Breguet", "Patek Philippe"}
    assert [candidate.source_listing_id for candidate in first] == [
        candidate.source_listing_id for candidate in second
    ]
    assert len({candidate.source_listing_id for candidate in first}) == 2
    by_brand = {candidate.brand: candidate for candidate in first}
    assert by_brand["Breguet"].reference_number == "5177BB"
    assert by_brand["Breguet"].asking_price_original == Decimal("14500")
    assert by_brand["Patek Philippe"].reference_number == "6119G"
    assert by_brand["Patek Philippe"].asking_price_original == Decimal("25000")


def test_same_brand_multiple_priced_paragraphs_are_rejected(settings) -> None:
    post = _post("reddit_active.json")
    post["selftext"] = (
        "Jaeger-LeCoultre ref Q4018420. Price: $6,250 USD.\n\n"
        "Jaeger-LeCoultre ref Q4148420. Price: $8,500 USD."
    )

    with pytest.raises(ValueError, match="multiple priced paragraphs"):
        parse_post(post, settings)


@pytest.mark.parametrize(
    ("description", "box", "papers"),
    [
        ("Price: $6,250 USD. No box or papers.", False, False),
        ("Price: $6,250 USD. Watch only.", False, False),
        ("Price: $6,250 USD. Box included, papers missing.", True, False),
    ],
)
def test_box_and_papers_negation_is_respected(
    settings, description: str, box: bool, papers: bool
) -> None:
    post = _post("reddit_active.json")
    post["selftext"] = description
    candidate = parse_post(post, settings)[0]
    assert candidate.box_included is box
    assert candidate.papers_included is papers


def test_offer_identity_survives_reference_and_second_brand_edits(settings) -> None:
    post = _post("reddit_active.json")
    initial = parse_post(post, settings)[0]
    post["title"] = post["title"].replace("ref Q4018420", "")
    post["selftext"] += "\nBreguet Classique ref 5177BB. Price: $14,500 USD."
    edited = parse_post(post, settings)
    jlc = next(candidate for candidate in edited if candidate.brand == "Jaeger-LeCoultre")
    assert jlc.source_listing_id == initial.source_listing_id


def test_removed_post_is_not_sold(settings) -> None:
    post = _post("reddit_active.json")
    post["removed_by_category"] = "moderator"
    post["selftext"] = "[removed]"
    candidate = parse_post(post, settings)[0]
    assert candidate.current_status == ListingStatus.REMOVED
    assert candidate.sold_price_original is None


def test_exact_48_hour_boundary_is_included(settings, monkeypatch) -> None:
    post = _post("reddit_active.json")
    post["created_utc"] = 1784790000
    settings.sources.reddit.access_approved = True
    settings.sources.reddit.deletion_contract_verified = True
    settings.sources.reddit.client_id = "synthetic"
    settings.sources.reddit.client_secret = "synthetic"
    settings.sources.reddit.username = "synthetic"
    source = RedditOAuthSource(settings)
    monkeypatch.setattr(
        source,
        "_get",
        lambda path, params: {"data": {"children": [{"kind": "t3", "data": post}], "after": None}},
    )
    start = datetime.fromtimestamp(1784790000, tz=UTC)
    end = start.replace(day=start.day + 2)
    candidates = source.discover(start, end)
    assert len(candidates) == 1


def test_refresh_does_not_fetch_or_store_comment_evidence(settings, monkeypatch) -> None:
    post = _post("reddit_active.json")
    settings.sources.reddit.access_approved = True
    settings.sources.reddit.deletion_contract_verified = True
    settings.sources.reddit.client_id = "synthetic"
    settings.sources.reddit.client_secret = "synthetic"
    settings.sources.reddit.username = "synthetic"
    source = RedditOAuthSource(settings)
    requested_paths: list[str] = []

    def fake_get(path, params):
        requested_paths.append(path)
        return {"data": {"children": [{"kind": "t3", "data": post}]}}

    monkeypatch.setattr(source, "_get", fake_get)
    outcome = source.refresh(["t3_fix001"])

    assert requested_paths == ["/r/Watchexchange/api/info"]
    assert outcome.returned_source_ad_ids == {"t3_fix001"}
    assert outcome.current_source_listing_ids == {"t3_fix001#item:jaeger-lecoultre"}
    assert outcome.candidates[0].current_status == ListingStatus.ACTIVE


def test_reddit_access_requires_verified_deletion_contract(settings) -> None:
    settings.sources.reddit.access_approved = True
    settings.sources.reddit.deletion_contract_verified = False
    source = RedditOAuthSource(settings)

    with pytest.raises(SourceAccessError) as caught:
        source._validate_access()

    assert caught.value.code == "deletion_contract_unverified"


def test_request_budget_counts_token_post_and_api_get(settings) -> None:
    settings.sources.reddit.access_approved = True
    settings.sources.reddit.deletion_contract_verified = True
    settings.sources.reddit.client_id = "synthetic"
    settings.sources.reddit.client_secret = "credential-that-must-not-leak"
    settings.sources.reddit.username = "synthetic"
    settings.sources.reddit.max_requests_per_run = 2
    settings.network.minimum_request_interval_seconds = 0
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/v1/access_token":
            return httpx.Response(
                200,
                json={"access_token": "token-that-must-not-leak", "expires_in": 3600},
            )
        return httpx.Response(200, json={"data": {"children": [], "after": None}})

    source = RedditOAuthSource(settings)
    source._client = httpx.Client(transport=httpx.MockTransport(handler))
    source._get("/r/Watchexchange/new", {"limit": 100})

    with pytest.raises(RuntimeError, match=r"budget exhausted \(limit 2\)") as caught:
        source._get("/r/Watchexchange/new", {"limit": 100})

    assert requests == [
        ("POST", "/api/v1/access_token"),
        ("GET", "/r/Watchexchange/new"),
    ]
    assert "credential-that-must-not-leak" not in str(caught.value)
    assert "token-that-must-not-leak" not in str(caught.value)
    assert "/r/Watchexchange" not in str(caught.value)
    source.close()


def test_request_budget_counts_each_retry_attempt(settings) -> None:
    settings.sources.reddit.access_approved = True
    settings.sources.reddit.deletion_contract_verified = True
    settings.sources.reddit.client_id = "synthetic"
    settings.sources.reddit.client_secret = "synthetic"
    settings.sources.reddit.username = "synthetic"
    settings.sources.reddit.max_requests_per_run = 3
    settings.network.minimum_request_interval_seconds = 0
    settings.network.backoff_seconds = 0
    settings.network.max_retries = 3
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/v1/access_token":
            return httpx.Response(
                200,
                json={"access_token": "synthetic", "expires_in": 3600},
            )
        return httpx.Response(500)

    source = RedditOAuthSource(settings)
    source._client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match=r"budget exhausted \(limit 3\)"):
        source._get("/r/Watchexchange/new", {"limit": 100})

    assert requests == [
        ("POST", "/api/v1/access_token"),
        ("GET", "/r/Watchexchange/new"),
        ("GET", "/r/Watchexchange/new"),
    ]
    source.close()
