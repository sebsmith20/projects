"""AI Analysis & Trade Recommendation Engine with cost-saving threshold gating."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

import requests

from config import Settings

logger = logging.getLogger(__name__)


def evaluate_trigger(
    market_snapshots: list[dict[str, Any]],
    last_briefing_date: str | None,
    settings: Settings,
    now: datetime | None = None,
) -> str | None:
    """Return a trigger reason string if an LLM analysis should run, else None."""
    reasons: list[str] = []

    if not settings.daily_summary_only:
        for snap in market_snapshots:
            sym = snap["yahoo_symbol"]
            chg = snap.get("change_pct")
            thresh = float(snap.get("move_threshold_pct", settings.price_move_threshold_pct))
            if chg is not None and abs(chg) >= thresh:
                reasons.append(f"{sym} moved {chg:+.2f}% (threshold +/-{thresh:.1f}%)")
            if snap.get("catalysts"):
                reasons.append(f"{sym} news catalyst: {snap['catalysts'][0][:80]}")
            tech = snap.get("technicals") or {}
            rsi = tech.get("rsi_14")
            if rsi is not None and (rsi <= 30 or rsi >= 70):
                state = "oversold" if rsi <= 30 else "overbought"
                reasons.append(f"{sym} RSI-14={rsi:.1f} ({state})")
            vol_ratio = tech.get("volume_ratio_20d")
            if vol_ratio is not None and vol_ratio >= 2.0:
                reasons.append(f"{sym} volume spike ({vol_ratio:.1f}x 20d avg)")

    tz = ZoneInfo(settings.market_timezone)
    local_now = now.astimezone(tz) if now else datetime.now(tz)
    today_str = local_now.strftime("%Y-%m-%d")

    past_daily_time = (local_now.hour, local_now.minute) >= (
        settings.daily_summary_hour,
        settings.daily_summary_minute,
    )
    if local_now.weekday() < 5 and past_daily_time and last_briefing_date != today_str:
        reasons.append(f"Daily summary schedule ({today_str})")

    if not reasons:
        return None
    return "; ".join(reasons[:4])


def _build_llm_prompt(
    positions: list[dict[str, Any]],
    cash_data: dict[str, Any],
    market_snapshots: list[dict[str, Any]],
    trigger_reason: str,
    macro_context: dict[str, Any] | None = None,
) -> str:
    context = {
        "trigger_reason": trigger_reason,
        "macro_context": macro_context or {},
        "account_cash": {
            "free": cash_data.get("free"),
            "invested": cash_data.get("invested"),
            "ppl": cash_data.get("ppl"),
            "total": cash_data.get("total"),
        },
        "open_positions": [
            {
                "ticker": p.get("ticker"),
                "quantity": p.get("quantity"),
                "averagePrice": p.get("averagePrice"),
                "currentPrice": p.get("currentPrice"),
                "ppl": p.get("ppl"),
            }
            for p in positions
        ],
        "market_and_news": [
            {
                "yahoo_symbol": m["yahoo_symbol"],
                "t212_ticker": m.get("t212_ticker"),
                "price": m.get("price"),
                "change_pct": m.get("change_pct"),
                "technicals": m.get("technicals", {}),
                "catalysts": m.get("catalysts", []),
                "headlines": [n["title"] for n in m.get("news", [])[:4]],
            }
            for m in market_snapshots
        ],
    }

    return (
        "You are a disciplined portfolio risk analyst assisting a human trader on Trading 212.\n"
        "Synthesize the macro regime (VIX, 10Y Treasury yield, DXY, GBP/USD, Gold, Oil), "
        "technical indicators (RSI-14, SMA-20/50/200 trend, 52w high distance, 5d/20d momentum, "
        "volume ratio, 20d volatility), news catalysts, and portfolio cash/P&L below.\n"
        "Return ONLY valid JSON with this exact schema:\n"
        "{\n"
        '  "portfolio_pnl_impact": "concise 1-2 sentence summary including macro backdrop",\n'
        '  "key_news_catalysts": ["bullet 1", "bullet 2"],\n'
        '  "risk_flags": ["risk 1 (macro/technical/concentration)", "risk 2"],\n'
        '  "trade_ideas": [\n'
        "    {\n"
        '      "t212_ticker": "AAPL_US_EQ",\n'
        '      "yahoo_symbol": "AAPL",\n'
        '      "action": "BUY" | "SELL" | "HOLD",\n'
        '      "quantity": 1.0,\n'
        '      "order_type": "MARKET" | "LIMIT",\n'
        '      "limit_price": null,\n'
        '      "reasoning": "concise rationale citing technicals, macro, and catalysts"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Rules:\n"
        "- Never recommend spending more than available free cash.\n"
        "- Never recommend selling more shares than currently held.\n"
        "- Prefer HOLD unless macro + technicals + catalysts align.\n"
        "- For HOLD ideas, set quantity to 0.\n\n"
        f"DATA:\n{json.dumps(context, indent=2)}"
    )


def _format_briefing_markdown(data: dict[str, Any]) -> str:
    lines = [
        "### Portfolio P&L Impact",
        data.get("portfolio_pnl_impact", "N/A"),
        "",
        "### Key News Catalysts",
    ]
    catalysts = data.get("key_news_catalysts") or ["None flagged"]
    for c in catalysts:
        lines.append(f"- {c}")

    lines.extend(["", "### Risk Flags"])
    risks = data.get("risk_flags") or ["None flagged"]
    for r in risks:
        lines.append(f"- {r}")

    lines.extend(["", "### Trade Ideas"])
    ideas = data.get("trade_ideas") or []
    if not ideas:
        lines.append("- No actionable trades suggested.")
    else:
        for idea in ideas:
            action = idea.get("action", "HOLD")
            ticker = idea.get("t212_ticker") or idea.get("yahoo_symbol")
            qty = idea.get("quantity", 0)
            otype = idea.get("order_type", "MARKET")
            lprice = idea.get("limit_price")
            price_str = f" @ {lprice}" if otype == "LIMIT" and lprice else ""
            lines.append(
                f"- **{action} {qty} {ticker}** ({otype}{price_str}): {idea.get('reasoning', '')}"
            )
    return "\n".join(lines)


def _fallback_rule_based_analysis(
    positions: list[dict[str, Any]],
    cash_data: dict[str, Any],
    market_snapshots: list[dict[str, Any]],
    trigger_reason: str,
) -> dict[str, Any]:
    """Deterministic briefing used when LLM_API_KEY is not configured."""
    free_cash = float(cash_data.get("free", 0.0) or 0.0)
    ppl = float(cash_data.get("ppl", 0.0) or 0.0)
    movers = sorted(
        [m for m in market_snapshots if m.get("change_pct") is not None],
        key=lambda x: abs(float(x["change_pct"])),
        reverse=True,
    )
    top_str = ", ".join(f"{m['yahoo_symbol']} ({m['change_pct']:+.2f}%)" for m in movers[:3])

    catalysts = []
    for m in market_snapshots:
        for c in m.get("catalysts", [])[:2]:
            catalysts.append(f"{m['yahoo_symbol']}: {c}")

    risk_flags = []
    if free_cash < 100:
        risk_flags.append(f"Low free cash buffer ({free_cash:.2f}).")
    if not positions:
        risk_flags.append("No open positions currently held in Trading 212 account.")

    ideas: list[dict[str, Any]] = []
    for m in movers[:2]:
        if m.get("t212_ticker"):
            ideas.append(
                {
                    "t212_ticker": m["t212_ticker"],
                    "yahoo_symbol": m["yahoo_symbol"],
                    "action": "HOLD",
                    "quantity": 0,
                    "order_type": "MARKET",
                    "limit_price": None,
                    "reasoning": (
                        f"Fallback mode (no LLM_API_KEY set). Trigger: {trigger_reason}. "
                        f"Observed move: {m['change_pct']:+.2f}%."
                    ),
                }
            )

    return {
        "portfolio_pnl_impact": (
            f"Open P&L: {ppl:+.2f} | Free Cash: {free_cash:.2f} | "
            f"Positions: {len(positions)} | Top movers: {top_str or 'N/A'}"
        ),
        "key_news_catalysts": catalysts or ["No major catalyst headlines detected."],
        "risk_flags": risk_flags or ["LLM_API_KEY not set — running deterministic summary mode."],
        "trade_ideas": ideas,
    }


def generate_briefing(
    settings: Settings,
    positions: list[dict[str, Any]],
    cash_data: dict[str, Any],
    market_snapshots: list[dict[str, Any]],
    trigger_reason: str,
    macro_context: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run LLM (or deterministic fallback) and return (markdown, raw_dict)."""
    if not settings.llm_api_key:
        logger.info("LLM_API_KEY not set; using deterministic fallback briefing.")
        raw = _fallback_rule_based_analysis(positions, cash_data, market_snapshots, trigger_reason)
        return _format_briefing_markdown(raw), raw

    base_url = settings.llm_base_url.rstrip("/")
    prompt = _build_llm_prompt(
        positions, cash_data, market_snapshots, trigger_reason, macro_context=macro_context
    )

    last_error = ""
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.llm_model,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "You are a concise, risk-aware equity analyst."},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=60,
            )
            if resp.ok:
                data = resp.json()
                content = data["choices"][0]["message"]["content"] or "{}"
                raw = json.loads(content)
                return _format_briefing_markdown(raw), raw

            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                wait_sec = attempt * 3
                logger.warning(
                    "LLM transient error (%s) on attempt %d/3; retrying in %ds.",
                    resp.status_code,
                    attempt,
                    wait_sec,
                )
                time.sleep(wait_sec)
                continue
            break
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < 3:
                time.sleep(attempt * 3)
                continue
            break

    logger.error("LLM unavailable after retries (%s); using fallback briefing.", last_error)
    raw = _fallback_rule_based_analysis(positions, cash_data, market_snapshots, trigger_reason)
    raw["risk_flags"] = [f"LLM temporarily unavailable ({last_error[:120]}); fallback summary used."] + raw.get(
        "risk_flags", []
    )
    return _format_briefing_markdown(raw), raw
