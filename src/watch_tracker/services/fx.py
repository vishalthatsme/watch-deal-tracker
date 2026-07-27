from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from watch_tracker.config import Settings
from watch_tracker.database.models import ExchangeRate

LOGGER = logging.getLogger(__name__)
MAX_FALLBACK_AGE = timedelta(days=7)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class CurrencyConverter:
    def __init__(self, settings: Settings, session: Session) -> None:
        self.settings = settings
        self.session = session
        self._memo: dict[tuple[str, date], Decimal | None] = {}

    def rate_to_usd(self, currency: str | None, at: datetime | None = None) -> Decimal | None:
        if not currency:
            return None
        currency = currency.upper()
        if currency == "USD":
            return Decimal("1")
        at = _as_utc(at or datetime.now(UTC))
        memo_key = (currency, at.date())
        if memo_key in self._memo:
            return self._memo[memo_key]
        retrieved_at = datetime.now(UTC)
        retrieval_freshness = retrieved_at - timedelta(hours=self.settings.currency.cache_hours)
        effective_cutoff = at - MAX_FALLBACK_AGE
        cached = self.session.scalar(
            select(ExchangeRate)
            .where(
                ExchangeRate.base_currency == currency,
                ExchangeRate.quote_currency == "USD",
                ExchangeRate.effective_at_utc >= effective_cutoff,
                ExchangeRate.effective_at_utc <= at,
                ExchangeRate.retrieved_at_utc >= retrieval_freshness,
            )
            .order_by(ExchangeRate.effective_at_utc.desc())
            .limit(1)
        )
        if cached:
            self._memo[memo_key] = cached.rate
            return cached.rate

        resource = at.date().isoformat() if at.date() < datetime.now(UTC).date() else "latest"
        try:
            response = httpx.get(
                f"{self.settings.currency.provider_url.rstrip('/')}/{resource}",
                params={"base": currency, "symbols": "USD"},
                timeout=self.settings.network.timeout_seconds,
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
            rate = Decimal(str(payload["rates"]["USD"]))
            if rate <= 0:
                raise ValueError("Currency provider returned a non-positive rate")
            response_date = date.fromisoformat(str(payload["date"]))
            effective_at = datetime.combine(response_date, time.min, tzinfo=UTC)
            if effective_at > at:
                raise ValueError(
                    "Currency provider returned a rate effective after the requested time"
                )
        except Exception:
            fallback_cutoff = at - MAX_FALLBACK_AGE
            fallback = self.session.scalar(
                select(ExchangeRate)
                .where(
                    ExchangeRate.base_currency == currency,
                    ExchangeRate.quote_currency == "USD",
                    ExchangeRate.effective_at_utc >= fallback_cutoff,
                    ExchangeRate.effective_at_utc <= at,
                )
                .order_by(ExchangeRate.effective_at_utc.desc())
                .limit(1)
            )
            if fallback:
                LOGGER.warning(
                    "currency provider unavailable; using bounded past rate",
                    extra={
                        "currency": currency,
                        "requested_at_utc": at.isoformat(),
                        "fallback_effective_at_utc": _as_utc(fallback.effective_at_utc).isoformat(),
                        "maximum_fallback_age_days": MAX_FALLBACK_AGE.days,
                    },
                    exc_info=True,
                )
                self._memo[memo_key] = fallback.rate
                return fallback.rate
            LOGGER.warning(
                "currency conversion unavailable and no bounded past rate exists",
                extra={
                    "currency": currency,
                    "requested_at_utc": at.isoformat(),
                    "maximum_fallback_age_days": MAX_FALLBACK_AGE.days,
                },
                exc_info=True,
            )
            self._memo[memo_key] = None
            return None

        existing = self.session.scalar(
            select(ExchangeRate)
            .where(
                ExchangeRate.base_currency == currency,
                ExchangeRate.quote_currency == "USD",
                ExchangeRate.effective_at_utc == effective_at,
            )
            .limit(1)
        )
        if existing:
            existing.retrieved_at_utc = retrieved_at
            self._memo[memo_key] = existing.rate
            return existing.rate
        self.session.add(
            ExchangeRate(
                base_currency=currency,
                quote_currency="USD",
                rate=rate,
                effective_at_utc=effective_at,
                retrieved_at_utc=retrieved_at,
                provider=self.settings.currency.provider_url,
            )
        )
        self._memo[memo_key] = rate
        return rate

    def convert(
        self,
        amount: Decimal | None,
        currency: str | None,
        at: datetime | None = None,
    ) -> Decimal | None:
        if amount is None:
            return None
        rate = self.rate_to_usd(currency, at)
        return (amount * rate).quantize(Decimal("0.01")) if rate is not None else None
