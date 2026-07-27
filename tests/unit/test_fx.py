from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from watch_tracker.database.models import Base, ExchangeRate
from watch_tracker.database.session import create_sqlite_engine, make_session_factory
from watch_tracker.services.fx import CurrencyConverter


class _ProviderResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _add_rate(
    session,
    *,
    effective_at: datetime,
    rate: str,
    currency: str = "EUR",
    retrieved_at: datetime | None = None,
) -> None:
    session.add(
        ExchangeRate(
            base_currency=currency,
            quote_currency="USD",
            rate=Decimal(rate),
            effective_at_utc=effective_at,
            retrieved_at_utc=retrieved_at or datetime.now(UTC),
            provider="fixture",
        )
    )
    session.flush()


def test_cache_lookup_never_uses_a_future_rate(settings, monkeypatch) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    requested_at = datetime(2026, 1, 15, 12, tzinfo=UTC)

    def unexpected_request(*args, **kwargs):
        raise AssertionError("a valid past cache rate should avoid a provider request")

    monkeypatch.setattr("watch_tracker.services.fx.httpx.get", unexpected_request)
    with factory() as session:
        _add_rate(
            session,
            effective_at=requested_at - timedelta(hours=2),
            rate="1.10",
        )
        _add_rate(
            session,
            effective_at=requested_at + timedelta(hours=1),
            rate="9.99",
        )

        result = CurrencyConverter(settings, session).rate_to_usd("eur", requested_at)

    assert result == Decimal("1.10000000")


def test_backfill_uses_dated_endpoint_and_response_effective_date(settings, monkeypatch) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    requested_at = datetime(2026, 1, 15, 18, tzinfo=UTC)
    request: dict[str, Any] = {}

    def provider_get(url, *, params, timeout, follow_redirects):
        request.update(
            url=url,
            params=params,
            timeout=timeout,
            follow_redirects=follow_redirects,
        )
        return _ProviderResponse({"date": "2026-01-14", "rates": {"USD": "1.2345"}})

    monkeypatch.setattr("watch_tracker.services.fx.httpx.get", provider_get)
    with factory() as session:
        result = CurrencyConverter(settings, session).rate_to_usd("EUR", requested_at)
        session.flush()
        stored = session.scalar(select(ExchangeRate))

    assert result == Decimal("1.2345")
    assert request["url"].endswith("/2026-01-15")
    assert request["params"] == {"base": "EUR", "symbols": "USD"}
    assert request["follow_redirects"] is True
    assert stored is not None
    assert stored.effective_at_utc.replace(tzinfo=UTC) == datetime(2026, 1, 14, tzinfo=UTC)
    assert stored.retrieved_at_utc is not None


def test_provider_response_reuses_an_exact_stored_rate(settings, monkeypatch) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    requested_at = datetime(2026, 1, 15, 18, tzinfo=UTC)
    effective_at = datetime(2026, 1, 14, tzinfo=UTC)

    monkeypatch.setattr(
        "watch_tracker.services.fx.httpx.get",
        lambda *args, **kwargs: _ProviderResponse({"date": "2026-01-14", "rates": {"USD": "1.30"}}),
    )
    with factory() as session:
        stale_retrieval = datetime.now(UTC) - timedelta(days=2)
        _add_rate(
            session,
            effective_at=effective_at,
            rate="1.20",
            retrieved_at=stale_retrieval,
        )
        result = CurrencyConverter(settings, session).rate_to_usd("EUR", requested_at)
        session.flush()
        count = session.scalar(select(func.count(ExchangeRate.id)))
        stored = session.scalar(select(ExchangeRate))

    assert result == Decimal("1.20000000")
    assert count == 1
    assert stored is not None
    assert stored.retrieved_at_utc.replace(tzinfo=UTC) > stale_retrieval


def test_provider_failure_uses_only_a_bounded_past_rate(settings, monkeypatch, caplog) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    requested_at = datetime(2026, 1, 15, 18, tzinfo=UTC)

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic provider outage")

    monkeypatch.setattr("watch_tracker.services.fx.httpx.get", unavailable)
    with factory() as session:
        _add_rate(
            session,
            effective_at=requested_at - timedelta(days=8),
            rate="8.00",
            retrieved_at=datetime.now(UTC) - timedelta(days=2),
        )
        _add_rate(
            session,
            effective_at=requested_at - timedelta(days=6),
            rate="1.15",
            retrieved_at=datetime.now(UTC) - timedelta(days=2),
        )
        _add_rate(
            session,
            effective_at=requested_at + timedelta(hours=1),
            rate="9.99",
            retrieved_at=datetime.now(UTC) - timedelta(days=2),
        )
        with caplog.at_level(logging.WARNING):
            result = CurrencyConverter(settings, session).rate_to_usd("EUR", requested_at)

    assert result == Decimal("1.15000000")
    assert "using bounded past rate" in caplog.text


def test_provider_failure_rejects_an_expired_fallback(settings, monkeypatch) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    requested_at = datetime(2026, 1, 15, 18, tzinfo=UTC)

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic provider outage")

    monkeypatch.setattr("watch_tracker.services.fx.httpx.get", unavailable)
    with factory() as session:
        _add_rate(
            session,
            effective_at=requested_at - timedelta(days=8),
            rate="8.00",
            retrieved_at=datetime.now(UTC) - timedelta(days=2),
        )
        result = CurrencyConverter(settings, session).rate_to_usd("EUR", requested_at)

    assert result is None


def test_weekend_rate_is_memoized_by_requested_date(settings, monkeypatch) -> None:
    engine = create_sqlite_engine(settings.paths.database)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    requested_at = datetime(2026, 1, 17, 18, tzinfo=UTC)
    requests = 0

    def provider_get(*args, **kwargs):
        nonlocal requests
        requests += 1
        return _ProviderResponse({"date": "2026-01-16", "rates": {"USD": "1.25"}})

    monkeypatch.setattr("watch_tracker.services.fx.httpx.get", provider_get)
    with factory() as session:
        converter = CurrencyConverter(settings, session)
        first = converter.rate_to_usd("EUR", requested_at)
        second = converter.rate_to_usd("EUR", requested_at + timedelta(hours=1))

    assert first == Decimal("1.25")
    assert second == first
    assert requests == 1
