"""Trading 212 Public API v0 client with strict per-endpoint rate limiting."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)


class Trading212Error(RuntimeError):
    """Base error for Trading 212 API failures."""


class OrderUncertainError(Trading212Error):
    """Raised when an order POST times out or drops; NEVER blindly retry."""


class EndpointRateLimiter:
    """Thread-safe minimum-interval rate limiter per endpoint bucket."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}

    def wait(self, bucket: str, min_interval_sec: float) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last_call.get(bucket)
            if last is not None:
                elapsed = now - last
                if elapsed < min_interval_sec:
                    sleep_for = min_interval_sec - elapsed
                    logger.debug("Rate limiter sleeping %.2fs for bucket=%s", sleep_for, bucket)
                    time.sleep(sleep_for)
            self._last_call[bucket] = time.monotonic()


class Trading212Client:
    """Client for Trading 212 Public API (demo or live) using HTTP Basic Auth."""

    RATE_LIMITS_SEC = {
        "positions": 1.0,       # max 1 req / 1s
        "account_cash": 5.0,    # max 1 req / 5s
        "orders_list": 5.0,     # max 1 req / 5s
        "orders_place": 1.0,    # conservative 1 req / 1s
    }

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://demo.trading212.com/api/v0",
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("T212_API_KEY and T212_API_SECRET must be set.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.auth = HTTPBasicAuth(api_key, api_secret)
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "t212-analyst/1.0",
            }
        )
        self._limiter = EndpointRateLimiter()

    def _get(self, path: str, bucket: str, max_retries: int = 3) -> Any:
        url = f"{self.base_url}{path}"
        min_interval = self.RATE_LIMITS_SEC.get(bucket, 1.0)

        for attempt in range(1, max_retries + 1):
            self._limiter.wait(bucket, min_interval)
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", min_interval * 2))
                logger.warning(
                    "429 Rate limited on GET %s (attempt %d/%d). Waiting %.1fs.",
                    path,
                    attempt,
                    max_retries,
                    retry_after,
                )
                time.sleep(retry_after)
                continue
            if not resp.ok:
                raise Trading212Error(
                    f"GET {path} failed ({resp.status_code}): {resp.text[:300]}"
                )
            return resp.json()

        raise Trading212Error(f"GET {path} exceeded {max_retries} retries due to rate limiting.")

    def get_positions(self) -> list[dict[str, Any]]:
        """Fetch all open positions (max 1 req/sec)."""
        data = self._get("/equity/positions", bucket="positions")
        return list(data) if isinstance(data, list) else []

    def get_account_cash(self) -> dict[str, Any]:
        """Fetch account cash balances (max 1 req/5s)."""
        data = self._get("/equity/account/cash", bucket="account_cash")
        return dict(data) if isinstance(data, dict) else {}

    def get_open_orders(self) -> list[dict[str, Any]]:
        """Fetch currently active/pending orders (max 1 req/5s)."""
        data = self._get("/equity/orders", bucket="orders_list")
        return list(data) if isinstance(data, list) else []

    def _post_order(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit an order POST without automatic retries to prevent duplicates."""
        url = f"{self.base_url}{path}"
        self._limiter.wait("orders_place", self.RATE_LIMITS_SEC["orders_place"])
        try:
            resp = self.session.post(url, json=payload, timeout=self.timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise OrderUncertainError(
                f"Network error/timeout submitting order to {path}: {exc}. "
                "Do NOT auto-retry; check open orders in Trading 212 before re-submitting."
            ) from exc

        if not resp.ok:
            raise Trading212Error(
                f"POST {path} rejected ({resp.status_code}): {resp.text[:400]}"
            )
        return dict(resp.json()) if resp.text else {}

    def place_market_order(self, ticker: str, signed_quantity: float) -> dict[str, Any]:
        """Place a market order. Negative quantity = SELL, positive = BUY."""
        if signed_quantity == 0:
            raise ValueError("Order quantity cannot be zero.")
        payload = {
            "ticker": ticker,
            "quantity": round(float(signed_quantity), 6),
        }
        logger.info("Submitting MARKET order: %s", payload)
        return self._post_order("/equity/orders/market", payload)

    def place_limit_order(
        self,
        ticker: str,
        signed_quantity: float,
        limit_price: float,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        """Place a limit order. Negative quantity = SELL, positive = BUY."""
        if signed_quantity == 0:
            raise ValueError("Order quantity cannot be zero.")
        if limit_price <= 0:
            raise ValueError("Limit price must be positive.")
        payload = {
            "ticker": ticker,
            "quantity": round(float(signed_quantity), 6),
            "limitPrice": round(float(limit_price), 4),
            "timeValidity": time_validity,
        }
        logger.info("Submitting LIMIT order: %s", payload)
        return self._post_order("/equity/orders/limit", payload)
