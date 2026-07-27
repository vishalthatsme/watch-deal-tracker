from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from watch_tracker.config import BrandSettings

_TRACKING_PARAMETERS = {
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def canonicalize_url(source: str, url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    hostname = (parts.hostname or "").lower()
    if hostname in {"old.reddit.com", "new.reddit.com", "reddit.com"}:
        hostname = "www.reddit.com"
    port = f":{parts.port}" if parts.port else ""
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() not in _TRACKING_PARAMETERS
        ]
    )
    fragment = parts.fragment if source == "reddit" and parts.fragment.startswith("watch-") else ""
    path = re.sub(r"/{2,}", "/", parts.path)
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, f"{hostname}{port}", path, query, fragment))


def make_listing_uid(
    source: str,
    source_listing_id: str | None,
    canonical_url: str,
) -> str:
    if source_listing_id:
        return f"{source}:{source_listing_id}"
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    return f"{source}:urlsha256:{digest}"


def content_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def identify_brands(text: str, brands: list[BrandSettings]) -> list[str]:
    normalized = text.casefold()
    matches: list[str] = []
    for brand in brands:
        if any(_alias_present(normalized, alias.casefold()) for alias in brand.aliases):
            matches.append(brand.canonical)
    return matches


def _alias_present(text: str, alias: str) -> bool:
    if len(alias) <= 4 and alias.isalnum():
        return bool(re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", text))
    return alias in text


def stable_offer_suffix(brand: str, reference_number: str | None = None) -> str:
    value = "-".join(part for part in (brand, reference_number) if part)
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:72] or hashlib.sha256(value.encode()).hexdigest()[:16]
