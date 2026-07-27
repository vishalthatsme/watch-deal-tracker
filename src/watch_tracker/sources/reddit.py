from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from watch_tracker.config import Settings
from watch_tracker.domain import Confidence, ListingCandidate, ListingStatus
from watch_tracker.services.identity import (
    identify_brands,
    stable_offer_suffix,
)
from watch_tracker.sources.base import RefreshOutcome, SourceAccessError, SourceAdapter

LOGGER = logging.getLogger(__name__)
PARSER_VERSION = "reddit-oauth-1.1"

_LABELED_PRICE = re.compile(
    r"(?i)(?:price|asking|sale\s*value|sv)\s*(?:is|:|-)?\s*"
    r"(?:(USD|CAD|EUR|GBP)\s*)?([$€£])?\s*([\d,]+(?:\.\d{1,2})?)"
    r"(?:\s*(USD|CAD|EUR|GBP)\b)?"
)
_GENERIC_PRICE = re.compile(
    r"(?i)(?:(USD|CAD|EUR|GBP)\s*)?([$€£])\s*([\d,]+(?:\.\d{1,2})?)"
    r"(?:\s*(USD|CAD|EUR|GBP)\b)?"
)
_SOLD_PRICE = re.compile(
    r"(?i)\bsold\s+(?:to\s+\S+\s+)?for\s+"
    r"(?:(USD|CAD|EUR|GBP)\s*)?([$€£])?\s*([\d,]+(?:\.\d{1,2})?)"
    r"(?:\s*(USD|CAD|EUR|GBP)\b)?"
)
_REFERENCE = re.compile(
    r"(?i)\b(?:ref(?:erence)?(?:\s*(?:number|no\.?))?)\s*[:#-]?\s*"
    r"([A-Z0-9][A-Z0-9./-]{2,})"
)
_SIZE = re.compile(r"(?i)\b(\d{2}(?:\.\d)?)\s*mm\b")
_WATCH_ONLY = re.compile(r"(?i)\bwatch\s+only\b")


def _currency(code: str | None, symbol: str | None) -> str | None:
    if code:
        return code.upper()
    # A bare dollar symbol is globally ambiguous; do not silently score it as USD.
    return {"€": "EUR", "£": "GBP"}.get(symbol or "")


def _money(match: re.Match[str] | None) -> tuple[Decimal | None, str | None]:
    if not match:
        return None, None
    try:
        amount = Decimal(match.group(3).replace(",", ""))
    except (InvalidOperation, IndexError):
        return None, None
    trailing_code = match.group(4) if match.lastindex and match.lastindex >= 4 else None
    return amount, _currency(match.group(1) or trailing_code, match.group(2))


def _canonical_post_url(permalink: str) -> str:
    parts = urlsplit(permalink)
    path = parts.path if parts.netloc else permalink
    return urlunsplit(("https", "www.reddit.com", path.rstrip("/"), "", ""))


def _status(post: dict[str, Any]) -> tuple[ListingStatus, str]:
    title = str(post.get("title") or "")
    body = str(post.get("selftext") or "")
    flair = str(post.get("link_flair_text") or "")
    combined = " ".join((title, flair)).casefold()
    if post.get("removed_by_category") or body.strip().casefold() in {"[removed]", "[deleted]"}:
        return ListingStatus.REMOVED, "Reddit reports the submission as removed or deleted"
    if re.search(r"(?:^|[\[\s])sold(?:\]|$|\s)", combined):
        return ListingStatus.SOLD, "SOLD marker in seller-controlled title or flair"
    if re.search(r"(?:^|[\[\s])pending(?:\]|$|\s)", combined):
        return ListingStatus.PENDING, "PENDING marker in title or flair"
    if re.search(r"(?:^|[\[\s])reserved(?:\]|$|\s)", combined):
        return ListingStatus.RESERVED, "RESERVED marker in title or flair"
    return ListingStatus.ACTIVE, "Submission is visible without a terminal status marker"


def _target_sale_post(post: dict[str, Any]) -> bool:
    title = str(post.get("title") or "")
    flair = str(post.get("link_flair_text") or "")
    combined = f"{title} {flair}".casefold()
    if "[wtb" in combined or "[meta" in combined:
        return False
    return "[wts" in combined or re.search(r"\bwts\b", flair, re.IGNORECASE) is not None


def _mentions_brand(text: str, brand: str, aliases: list[str]) -> bool:
    normalized = text.casefold()
    return brand.casefold() in normalized or any(
        alias.casefold() in normalized for alias in aliases
    )


def _price_spans(text: str) -> list[tuple[int, int]]:
    """Return unique money spans without double-counting labeled prices."""

    spans = [match.span() for match in _LABELED_PRICE.finditer(text)]
    for match in _GENERIC_PRICE.finditer(text):
        if not any(start <= match.start() and match.end() <= end for start, end in spans):
            spans.append(match.span())
    return spans


def _segment_for_brand(text: str, brand: str, aliases: list[str]) -> str:
    paragraphs = re.split(r"\n\s*\n", text)
    matching = [
        paragraph
        for paragraph in paragraphs
        if _mentions_brand(paragraph, brand, aliases) and _price_spans(paragraph)
    ]
    if len(matching) > 1:
        raise ValueError(
            f"Ambiguous {brand} post: multiple priced paragraphs may describe different watches"
        )
    if matching:
        segment = matching[0]
        mention_count = max(
            (segment.casefold().count(alias.casefold()) for alias in {brand, *aliases} if alias),
            default=0,
        )
        if len(_price_spans(segment)) > 1 and mention_count > 1:
            raise ValueError(
                f"Ambiguous {brand} post: one paragraph may contain multiple priced watches"
            )
        return segment
    for paragraph in paragraphs:
        if _mentions_brand(paragraph, brand, aliases):
            return paragraph
    return text


def _included_status(text: str, noun: str) -> bool | None:
    normalized = " ".join(text.casefold().split())
    if _WATCH_ONLY.search(normalized):
        return False
    if re.search(
        r"\b(?:no|without)\s+(?:box\s*(?:and|or|/)\s*papers?"
        r"|papers?\s*(?:and|or|/)\s*(?:original\s+)?box)\b",
        normalized,
    ):
        return False
    noun_pattern = r"papers?" if noun == "papers" else r"(?:original\s+)?box"
    negative_patterns = (
        rf"\b(?:no|without)\s+(?:original\s+)?{noun_pattern}\b",
        rf"\b{noun_pattern}\s+(?:is\s+)?(?:not\s+included|unavailable|missing)\b",
        rf"\bdoes\s+not\s+include\s+(?:an?\s+)?{noun_pattern}\b",
    )
    if any(re.search(pattern, normalized) for pattern in negative_patterns):
        return False
    if "full set" in normalized:
        return True
    if re.search(rf"\b{noun_pattern}\b", normalized):
        return True
    return None


def parse_post(post: dict[str, Any], settings: Settings) -> list[ListingCandidate]:
    if not _target_sale_post(post):
        return []
    title = str(post.get("title") or "")
    body = str(post.get("selftext") or "")
    full_text = f"{title}\n{body}"
    matched_brands = identify_brands(full_text, settings.target_brands)
    if not matched_brands:
        return []

    posted = datetime.fromtimestamp(float(post["created_utc"]), tz=UTC)
    source_ad_id = str(post.get("name") or f"t3_{post['id']}")
    base_url = _canonical_post_url(str(post.get("permalink") or ""))
    status, status_evidence = _status(post)
    sold_match = _SOLD_PRICE.search(full_text) if status == ListingStatus.SOLD else None
    sold_price, sold_currency = _money(sold_match)
    candidates: list[ListingCandidate] = []

    for brand_name in matched_brands:
        brand_config = next(
            brand for brand in settings.target_brands if brand.canonical == brand_name
        )
        segment_source = body if identify_brands(body, [brand_config]) else full_text
        segment = _segment_for_brand(segment_source, brand_name, brand_config.aliases)
        reference_match = _REFERENCE.search(segment)
        reference = reference_match.group(1).upper().rstrip(".,;:") if reference_match else None
        # The item key intentionally excludes mutable reference/price data and
        # is always present, so single-to-multi edits retain the same identity.
        suffix = stable_offer_suffix(brand_name)
        source_listing_id = f"{source_ad_id}#item:{suffix}"
        canonical_url = f"{base_url}#watch-{suffix}"
        price_match = _LABELED_PRICE.search(segment) or _GENERIC_PRICE.search(segment)
        asking_price, currency = _money(price_match)
        size_match = _SIZE.search(segment)
        size = Decimal(size_match.group(1)) if size_match else None
        box = _included_status(segment, "box")
        papers = _included_status(segment, "papers")
        missing = []
        if asking_price is None:
            missing.append("asking price")
        elif currency is None:
            missing.append("currency")
        if reference is None:
            missing.append("reference number")
        if box is None:
            missing.append("box status")
        if papers is None:
            missing.append("papers status")
        questions = [f"Please confirm the {item}." for item in missing]
        candidates.append(
            ListingCandidate(
                source="reddit",
                source_listing_id=source_listing_id,
                canonical_url=canonical_url,
                title=title,
                original_posted_at_utc=posted,
                date_evidence=f"Reddit created_utc={post['created_utc']}",
                date_confidence=Confidence.HIGH,
                current_status=status,
                status_evidence=status_evidence,
                brand=brand_name,
                model=None,
                reference_number=reference,
                case_size_mm=size,
                condition_notes=None,
                box_included=box,
                papers_included=papers,
                seller_name=post.get("author"),
                seller_type="private",
                seller_reputation_evidence=post.get("author_flair_text"),
                asking_price_original=asking_price,
                currency=currency,
                estimated_all_in_original=asking_price,
                description_summary=" ".join(segment.split())[:700],
                risk_flags=[],
                missing_information=missing,
                questions_to_ask_seller=questions,
                sold_price_original=sold_price,
                sold_price_currency=sold_currency,
                sold_price_evidence=(sold_match.group(0) if sold_match else None),
                sold_price_confidence=Confidence.HIGH if sold_match else None,
                raw_payload={
                    "source_ad_id": source_ad_id,
                    "edited": post.get("edited"),
                    "num_comments": post.get("num_comments"),
                    "full_snapshot": True,
                    "author_deleted": post.get("author") is None,
                    "parser_version": PARSER_VERSION,
                },
            )
        )
    return candidates


class RedditOAuthSource(SourceAdapter):
    name = "reddit"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config = settings.sources.reddit
        self._client: httpx.Client | None = None
        self._token: str | None = None
        self._token_expiry = 0.0
        self._last_request_at = 0.0
        # DailyPipeline creates one adapter for an invocation. This counter
        # therefore spans token acquisition, discovery, refresh, and retries
        # performed during that run.
        self._http_requests_attempted = 0

    def _validate_access(self) -> None:
        if not self.config.enabled:
            raise SourceAccessError(self.name, "disabled", "Reddit source is disabled")
        if not self.config.access_approved:
            raise SourceAccessError(
                self.name,
                "approval_required",
                "Reddit API access must be explicitly approved before collection",
            )
        if not self.config.deletion_contract_verified:
            raise SourceAccessError(
                self.name,
                "deletion_contract_unverified",
                "Reddit collection is blocked until the approved integration's "
                "deletion semantics have been verified",
            )
        if not all((self.config.client_id, self.config.client_secret, self.config.username)):
            raise SourceAccessError(
                self.name,
                "credentials_missing",
                "Approved Reddit OAuth credentials and username are not configured",
            )

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            user_agent = f"macos:watch-deal-tracker:0.1.0 (by /u/{self.config.username})"
            self._client = httpx.Client(
                timeout=self.settings.network.timeout_seconds,
                headers={"User-Agent": user_agent},
                follow_redirects=True,
            )
        return self._client

    def _access_token(self) -> str:
        self._validate_access()
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        client = self.client
        self._reserve_http_request()
        response = client.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(self.config.client_id or "", self.config.client_secret or ""),
            data={"grant_type": "client_credentials"},
        )
        response.raise_for_status()
        payload = response.json()
        self._token = str(payload["access_token"])
        self._token_expiry = time.monotonic() + max(30, int(payload.get("expires_in", 3600)) - 60)
        return self._token

    def _wait_for_rate_limit(self) -> None:
        minimum = self.settings.network.minimum_request_interval_seconds
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < minimum:
            time.sleep(minimum - elapsed)

    def _reserve_http_request(self) -> None:
        limit = self.config.max_requests_per_run
        if self._http_requests_attempted >= limit:
            raise RuntimeError(
                f"Reddit HTTP request budget exhausted (limit {limit}); "
                "no additional request was sent"
            )
        self._http_requests_attempted += 1

    def _get(self, path: str, params: dict[str, str | int]) -> Any:
        token = self._access_token()
        response: httpx.Response | None = None
        for attempt in range(self.settings.network.max_retries + 1):
            self._wait_for_rate_limit()
            self._reserve_http_request()
            response = self.client.get(
                f"https://oauth.reddit.com{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            self._last_request_at = time.monotonic()
            if response.status_code != 429 and response.status_code < 500:
                break
            if attempt >= self.settings.network.max_retries:
                break
            retry_after = response.headers.get("retry-after")
            delay = (
                float(retry_after)
                if retry_after
                else self.settings.network.backoff_seconds * (2**attempt)
            )
            time.sleep(min(delay, 60.0))
        assert response is not None
        response.raise_for_status()
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining and float(remaining) < 2 and reset:
            time.sleep(min(float(reset), 60.0))
        return response.json()

    def discover(self, window_start: datetime, window_end: datetime) -> list[ListingCandidate]:
        self._validate_access()
        candidates: list[ListingCandidate] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        pages = 0
        while True:
            pages += 1
            if pages > 1000:
                error = RuntimeError("Reddit pagination exceeded the 1000-page safety limit")
                self.record_failure("discover_pagination", error)
                break
            params: dict[str, str | int] = {
                "limit": self.config.page_limit,
                "raw_json": 1,
            }
            if after:
                params["after"] = after
            payload = self._get(f"/r/{self.config.community}/new", params)
            listing_data = payload.get("data", {})
            children = listing_data.get("children", [])
            reached_cutoff = False
            for child in children:
                record_id = "unknown"
                try:
                    if not isinstance(child, dict):
                        raise TypeError("Reddit listing child is not an object")
                    post = child.get("data", {})
                    if not isinstance(post, dict):
                        raise TypeError("Reddit submission data is not an object")
                    record_id = str(post.get("name") or post.get("id") or "unknown")
                    if post.get("stickied"):
                        continue
                    created = datetime.fromtimestamp(
                        float(post["created_utc"]),
                        tz=UTC,
                    )
                except Exception as error:
                    LOGGER.exception(
                        "Reddit submission metadata rejected",
                        extra={"stage": "discover_metadata", "record_id": record_id},
                    )
                    self.record_failure("discover_metadata", error, record_id)
                    continue
                if created < window_start:
                    reached_cutoff = True
                    continue
                if created <= window_end:
                    if post.get("removed_by_category") or str(post.get("selftext")).casefold() in {
                        "[removed]",
                        "[deleted]",
                    }:
                        continue
                    try:
                        candidates.extend(parse_post(post, self.settings))
                    except Exception as error:
                        LOGGER.exception(
                            "Reddit submission rejected",
                            extra={"stage": "discover_parse", "record_id": record_id},
                        )
                        self.record_failure("discover_parse", error, record_id)
            next_after = listing_data.get("after")
            if reached_cutoff or not next_after:
                break
            after = str(next_after)
            if after in seen_cursors:
                error = RuntimeError(f"Reddit pagination cursor repeated: {after}")
                self.record_failure("discover_pagination", error)
                break
            seen_cursors.add(after)
        return candidates

    def refresh(self, source_ad_ids: list[str]) -> RefreshOutcome:
        self._validate_access()
        candidates: list[ListingCandidate] = []
        deleted: set[str] = set()
        returned_source_ad_ids: set[str] = set()
        current_source_listing_ids: set[str] = set()
        unique_ids = sorted({source_id.split("#", 1)[0] for source_id in source_ad_ids})
        for offset in range(0, len(unique_ids), 100):
            batch = unique_ids[offset : offset + 100]
            payload = self._get(
                f"/r/{self.config.community}/api/info",
                {"id": ",".join(batch), "raw_json": 1},
            )
            children = payload.get("data", {}).get("children", [])
            returned: set[str] = set()
            for child in children:
                ad_id = "unknown"
                try:
                    if not isinstance(child, dict):
                        raise TypeError("Reddit listing child is not an object")
                    post = child.get("data", {})
                    if not isinstance(post, dict):
                        raise TypeError("Reddit submission data is not an object")
                    source_id = post.get("name") or (f"t3_{post['id']}" if post.get("id") else None)
                    if not source_id:
                        raise ValueError("Reddit submission has no source ID")
                    ad_id = str(source_id)
                except Exception as error:
                    LOGGER.exception(
                        "Reddit refresh metadata rejected",
                        extra={"stage": "refresh_metadata", "record_id": ad_id},
                    )
                    self.record_failure("refresh_metadata", error, ad_id)
                    continue
                returned.add(ad_id)
                if post.get("removed_by_category") or str(post.get("selftext")).casefold() in {
                    "[removed]",
                    "[deleted]",
                }:
                    deleted.add(ad_id)
                    continue
                try:
                    parsed = parse_post(post, self.settings)
                except Exception as error:
                    LOGGER.exception(
                        "Reddit submission rejected",
                        extra={"stage": "refresh_parse", "record_id": ad_id},
                    )
                    self.record_failure("refresh_parse", error, ad_id)
                    continue
                returned_source_ad_ids.add(ad_id)
                current_source_listing_ids.update(
                    candidate.source_listing_id
                    for candidate in parsed
                    if candidate.source_listing_id is not None
                )
                candidates.extend(parsed)
            # Absence is not proof of deletion; it remains an unavailable check.
            LOGGER.info(
                "reddit refresh batch completed",
                extra={"requested": len(batch), "returned": len(returned)},
            )
        return RefreshOutcome(
            candidates=candidates,
            deleted_source_ad_ids=deleted,
            returned_source_ad_ids=returned_source_ad_ids,
            current_source_listing_ids=current_source_listing_ids,
        )

    def close(self) -> None:
        if self._client:
            self._client.close()
