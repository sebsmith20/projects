"""External market data and news fetcher (yfinance + RSS) to avoid overloading T212."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import feedparser
import requests

try:
    import yfinance as yf  # type: ignore[import-untyped]
except (ImportError, OSError) as exc:
    yf = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning(
        "yfinance unavailable (%s); using pure-Python Yahoo Finance HTTP + RSS fallback.",
        exc,
    )

logger = logging.getLogger(__name__)

CATALYST_KEYWORDS = (
    "earnings",
    "guidance",
    "revenue",
    "acquisition",
    "merger",
    "buyout",
    "fda",
    "sec ",
    "investigation",
    "downgrade",
    "upgrade",
    "layoff",
    "dividend",
    "stock split",
    "bankruptcy",
    "beat",
    "miss",
)


def t212_to_yahoo_symbol(t212_ticker: str) -> str:
    """Best-effort mapping from Trading 212 ticker (e.g. AAPL_US_EQ) to Yahoo symbol."""
    parts = t212_ticker.split("_")
    if not parts:
        return t212_ticker
    base = parts[0]
    if len(parts) >= 2:
        suffix = parts[1].upper()
        if suffix in {"L", "LN"}:
            return f"{base}.L"
        if suffix in {"DE", "GY"}:
            return f"{base}.DE"
        if suffix in {"PA", "FP"}:
            return f"{base}.PA"
    return base


def load_watchlist(watchlist_path: Path) -> list[dict[str, Any]]:
    if not watchlist_path.exists():
        logger.warning("Watchlist not found at %s; using empty watchlist.", watchlist_path)
        return []
    data = json.loads(watchlist_path.read_text(encoding="utf-8"))
    return list(data.get("tickers", []))


def fetch_rss_news(yahoo_symbol: str, limit: int = 5) -> list[dict[str, str]]:
    """Fetch recent headlines from Yahoo Finance RSS feed."""
    url = (
        f"https://feeds.finance.yahoo.com/rss/2.0/headline"
        f"?s={yahoo_symbol}&region=US&lang=en-US"
    )
    try:
        feed = feedparser.parse(url)
        items: list[dict[str, str]] = []
        for entry in feed.entries[:limit]:
            items.append(
                {
                    "title": str(entry.get("title", "")).strip(),
                    "published": str(entry.get("published", "")).strip(),
                    "link": str(entry.get("link", "")).strip(),
                }
            )
        return items
    except Exception as exc:
        logger.debug("RSS fetch failed for %s: %s", yahoo_symbol, exc)
        return []


def _fetch_chart_http(yahoo_symbol: str) -> tuple[float | None, float | None, float | None, float | None]:
    """Pure-Python fallback using Yahoo Finance v8 chart JSON endpoint (no pandas/DLLs)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    try:
        resp = requests.get(
            url,
            params={"range": "5d", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0 (compatible; t212-analyst/1.0)"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Yahoo chart HTTP %s for %s", resp.status_code, yahoo_symbol)
            return None, None, None, None
        data = resp.json()
        result = (data.get("chart", {}).get("result") or [None])[0]
        if not result:
            return None, None, None, None

        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = [c for c in (quote.get("close") or []) if c is not None]
        volumes = [v for v in (quote.get("volume") or []) if v is not None]

        price = float(closes[-1]) if len(closes) >= 1 else None
        prev_close = float(closes[-2]) if len(closes) >= 2 else None
        change_pct = (
            ((price - prev_close) / prev_close) * 100.0
            if price is not None and prev_close
            else None
        )
        volume = float(volumes[-1]) if volumes else None
        return price, prev_close, change_pct, volume
    except Exception as exc:
        logger.warning("Yahoo chart HTTP fallback failed for %s: %s", yahoo_symbol, exc)
        return None, None, None, None


def fetch_symbol_snapshot(
    yahoo_symbol: str,
    t212_ticker: str | None = None,
    move_threshold_pct: float = 2.0,
) -> dict[str, Any]:
    """Pull current price, daily % change, volume, and recent news via yfinance or HTTP/RSS."""
    price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    volume: float | None = None
    news_items: list[dict[str, str]] = []

    if yf is not None:
        try:
            ticker = yf.Ticker(yahoo_symbol)
            hist = ticker.history(period="5d", interval="1d")
            if not hist.empty:
                closes = hist["Close"].dropna()
                if len(closes) >= 1:
                    price = float(closes.iloc[-1])
                if len(closes) >= 2:
                    prev_close = float(closes.iloc[-2])
                    if prev_close:
                        change_pct = ((price - prev_close) / prev_close) * 100.0
                if "Volume" in hist and not hist["Volume"].dropna().empty:
                    volume = float(hist["Volume"].dropna().iloc[-1])

            raw_news = getattr(ticker, "news", None) or []
            for item in raw_news[:5]:
                content = item.get("content", item) if isinstance(item, dict) else {}
                title = str(content.get("title", "")).strip()
                if title:
                    news_items.append(
                        {
                            "title": title,
                            "published": str(content.get("pubDate", "")),
                            "link": str(
                                (content.get("canonicalUrl") or {}).get("url", "")
                                if isinstance(content.get("canonicalUrl"), dict)
                                else content.get("link", "")
                            ),
                        }
                    )
        except Exception as exc:
            logger.warning("yfinance fetch error for %s: %s", yahoo_symbol, exc)

    if price is None:
        price, prev_close, change_pct, volume = _fetch_chart_http(yahoo_symbol)

    if not news_items:
        news_items = fetch_rss_news(yahoo_symbol, limit=5)

    catalysts = [
        n["title"]
        for n in news_items
        if any(kw in n["title"].lower() for kw in CATALYST_KEYWORDS)
    ]

    return {
        "yahoo_symbol": yahoo_symbol,
        "t212_ticker": t212_ticker,
        "price": round(price, 4) if price is not None else None,
        "prev_close": round(prev_close, 4) if prev_close is not None else None,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "volume": volume,
        "move_threshold_pct": move_threshold_pct,
        "news": news_items,
        "catalysts": catalysts,
    }


def collect_market_universe(
    positions: list[dict[str, Any]],
    watchlist: list[dict[str, Any]],
    default_threshold_pct: float = 2.0,
) -> list[dict[str, Any]]:
    """Build deduplicated universe of held tickers + watchlist tickers and fetch data."""
    targets: dict[str, dict[str, Any]] = {}

    for item in watchlist:
        t212_t = item.get("t212_ticker")
        ys = item.get("yahoo_symbol") or (t212_to_yahoo_symbol(t212_t) if t212_t else None)
        if not ys:
            continue
        targets[ys] = {
            "yahoo_symbol": ys,
            "t212_ticker": t212_t,
            "move_threshold_pct": float(item.get("move_threshold_pct", default_threshold_pct)),
        }

    for pos in positions:
        t212_t = pos.get("ticker", "")
        if not t212_t:
            continue
        # Prefer explicit mapping from watchlist if present
        existing = next((v for v in targets.values() if v.get("t212_ticker") == t212_t), None)
        if existing:
            continue
        ys = t212_to_yahoo_symbol(t212_t)
        targets.setdefault(
            ys,
            {
                "yahoo_symbol": ys,
                "t212_ticker": t212_t,
                "move_threshold_pct": default_threshold_pct,
            },
        )

    snapshots: list[dict[str, Any]] = []
    for target in targets.values():
        snapshots.append(
            fetch_symbol_snapshot(
                yahoo_symbol=target["yahoo_symbol"],
                t212_ticker=target.get("t212_ticker"),
                move_threshold_pct=target["move_threshold_pct"],
            )
        )
    return snapshots
