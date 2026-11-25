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


import math
import statistics


def _compute_sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas[:period]]
    losses = [max(-d, 0.0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_technicals(closes: list[float], volumes: list[float]) -> dict[str, Any]:
    if not closes:
        return {}
    price = closes[-1]
    sma20 = _compute_sma(closes, 20)
    sma50 = _compute_sma(closes, 50)
    sma200 = _compute_sma(closes, 200)
    rsi14 = _compute_rsi(closes, 14)

    high_52w = max(closes[-252:])
    low_52w = min(closes[-252:])
    pct_from_52w_high = ((price - high_52w) / high_52w) * 100.0 if high_52w else None

    ret_5d = ((price - closes[-6]) / closes[-6]) * 100.0 if len(closes) >= 6 and closes[-6] else None
    ret_20d = ((price - closes[-21]) / closes[-21]) * 100.0 if len(closes) >= 21 and closes[-21] else None

    vol_ratio = None
    if len(volumes) >= 21:
        avg_vol20 = sum(volumes[-21:-1]) / 20.0
        if avg_vol20 > 0:
            vol_ratio = volumes[-1] / avg_vol20

    vol_20d_ann = None
    if len(closes) >= 22:
        daily_rets = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(len(closes) - 20, len(closes))
            if closes[i - 1] > 0
        ]
        if len(daily_rets) >= 2:
            vol_20d_ann = statistics.stdev(daily_rets) * math.sqrt(252) * 100.0

    return {
        "rsi_14": round(rsi14, 1) if rsi14 is not None else None,
        "sma_20": round(sma20, 2) if sma20 is not None else None,
        "sma_50": round(sma50, 2) if sma50 is not None else None,
        "sma_200": round(sma200, 2) if sma200 is not None else None,
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "pct_from_52w_high": round(pct_from_52w_high, 2) if pct_from_52w_high is not None else None,
        "return_5d_pct": round(ret_5d, 2) if ret_5d is not None else None,
        "return_20d_pct": round(ret_20d, 2) if ret_20d is not None else None,
        "volume_ratio_20d": round(vol_ratio, 2) if vol_ratio is not None else None,
        "volatility_20d_pct": round(vol_20d_ann, 1) if vol_20d_ann is not None else None,
    }


def _fetch_chart_http(
    yahoo_symbol: str, range_str: str = "1y"
) -> tuple[float | None, float | None, float | None, float | None, dict[str, Any], str | None]:
    """Pure-Python Yahoo Finance v8 chart JSON fetch (returns price, prev_close, chg%, vol, technicals, currency)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    try:
        resp = requests.get(
            url,
            params={"range": range_str, "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0 (compatible; t212-analyst/1.0)"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Yahoo chart HTTP %s for %s", resp.status_code, yahoo_symbol)
            return None, None, None, None, {}, None
        data = resp.json()
        result = (data.get("chart", {}).get("result") or [None])[0]
        if not result:
            return None, None, None, None, {}, None

        currency = (result.get("meta") or {}).get("currency")
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        raw_closes = quote.get("close") or []
        raw_volumes = quote.get("volume") or []

        closes = [float(c) for c in raw_closes if c is not None]
        volumes = [float(v) for v in raw_volumes if v is not None]

        price = closes[-1] if len(closes) >= 1 else None
        prev_close = closes[-2] if len(closes) >= 2 else None
        change_pct = (
            ((price - prev_close) / prev_close) * 100.0
            if price is not None and prev_close
            else None
        )
        volume = volumes[-1] if volumes else None
        technicals = _compute_technicals(closes, volumes) if len(closes) >= 5 else {}
        return price, prev_close, change_pct, volume, technicals, currency
    except Exception as exc:
        logger.warning("Yahoo chart HTTP fetch failed for %s: %s", yahoo_symbol, exc)
        return None, None, None, None, {}, None


MACRO_BENCHMARKS = {
    "^VIX": "VIX Volatility Index",
    "^TNX": "US 10Y Treasury Yield (%)",
    "DX-Y.NYB": "US Dollar Index (DXY)",
    "GBPUSD=X": "GBP/USD FX Rate",
    "GC=F": "Gold Futures",
    "CL=F": "WTI Crude Oil",
}


def fetch_macro_context() -> dict[str, Any]:
    """Fetch external macro indicators (VIX, 10Y Yield, DXY, GBP/USD, Gold, Oil)."""
    indicators: list[dict[str, Any]] = []
    for sym, label in MACRO_BENCHMARKS.items():
        price, _prev, chg, _vol, _tech, _ccy = _fetch_chart_http(sym, range_str="5d")
        if price is not None:
            indicators.append(
                {
                    "symbol": sym,
                    "name": label,
                    "value": round(price, 3),
                    "change_pct": round(chg, 2) if chg is not None else None,
                }
            )
    return {
        "indicators": indicators,
        "macro_headlines": [n["title"] for n in fetch_rss_news("SPY", limit=3)],
    }


def fetch_symbol_snapshot(
    yahoo_symbol: str,
    t212_ticker: str | None = None,
    move_threshold_pct: float = 2.0,
    name: str | None = None,
    sector: str | None = None,
) -> dict[str, Any]:
    """Pull current price, daily % change, technicals (RSI/SMA/vol), and recent news."""
    price, prev_close, change_pct, volume, technicals, currency = _fetch_chart_http(
        yahoo_symbol, range_str="1y"
    )

    if price is None and yf is not None:
        try:
            ticker = yf.Ticker(yahoo_symbol)
            hist = ticker.history(period="1y", interval="1d")
            if not hist.empty:
                closes = [float(x) for x in hist["Close"].dropna().tolist()]
                volumes = (
                    [float(x) for x in hist["Volume"].dropna().tolist()]
                    if "Volume" in hist
                    else []
                )
                if len(closes) >= 1:
                    price = closes[-1]
                if len(closes) >= 2:
                    prev_close = closes[-2]
                    if prev_close:
                        change_pct = ((price - prev_close) / prev_close) * 100.0
                volume = volumes[-1] if volumes else None
                technicals = _compute_technicals(closes, volumes)
        except Exception as exc:
            logger.warning("yfinance fetch error for %s: %s", yahoo_symbol, exc)

    news_items = fetch_rss_news(yahoo_symbol, limit=5)

    catalysts = [
        n["title"]
        for n in news_items
        if any(kw in n["title"].lower() for kw in CATALYST_KEYWORDS)
    ]

    return {
        "yahoo_symbol": yahoo_symbol,
        "t212_ticker": t212_ticker,
        "name": name or yahoo_symbol,
        "sector": sector or "Other",
        "currency": currency,
        "price": round(price, 4) if price is not None else None,
        "prev_close": round(prev_close, 4) if prev_close is not None else None,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "volume": volume,
        "move_threshold_pct": move_threshold_pct,
        "technicals": technicals,
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
            "name": item.get("name") or ys,
            "sector": item.get("sector") or "Other",
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
                "name": ys,
                "sector": "Held Position",
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
                name=target.get("name"),
                sector=target.get("sector"),
            )
        )
    return snapshots
