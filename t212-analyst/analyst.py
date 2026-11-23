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
    todays_triggers: list[str] | None = None,
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

    if local_now.weekday() < 5:
        if todays_triggers is not None and settings.scheduled_briefing_times:
            # Check latest passed slot first so a late start doesn't fire an old morning slot
            for slot in sorted(settings.scheduled_briefing_times, reverse=True):
                try:
                    sh, sm = (int(x) for x in slot.split(":", 1))
                except ValueError:
                    continue
                if (local_now.hour, local_now.minute) >= (sh, sm):
                    slot_tag = f"[slot {slot}]"
                    if not any(slot_tag in t for t in todays_triggers):
                        reasons.append(f"Scheduled briefing {slot_tag} ({today_str})")
                    break
        else:
            past_daily_time = (local_now.hour, local_now.minute) >= (
                settings.daily_summary_hour,
                settings.daily_summary_minute,
            )
            if past_daily_time and last_briefing_date != today_str:
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
    held_tickers = {p.get("ticker") for p in positions if p.get("ticker")}

    def _priority_key(m: dict[str, Any]) -> tuple[int, int, float]:
        is_held = 1 if m.get("t212_ticker") in held_tickers else 0
        has_cat = 1 if m.get("catalysts") else 0
        chg = abs(float(m.get("change_pct") or 0.0))
        return (is_held, has_cat, chg)

    ranked = sorted(market_snapshots, key=_priority_key, reverse=True)
    detailed_snaps = ranked[:15]
    compact_snaps = ranked[15:]

    detailed_market = []
    for m in detailed_snaps:
        t = m.get("technicals") or {}
        detailed_market.append(
            {
                "sym": m["yahoo_symbol"],
                "t212": m.get("t212_ticker"),
                "px": m.get("price"),
                "chg%": m.get("change_pct"),
                "rsi14": t.get("rsi_14"),
                "sma50": t.get("sma_50"),
                "sma200": t.get("sma_200"),
                "52wHi%": t.get("pct_from_52w_high"),
                "volX": t.get("volume_ratio_20d"),
                "cat": m.get("catalysts", [])[:2],
                "news": [n["title"][:90] for n in m.get("news", [])[:2]],
            }
        )

    other_watchlist = {
        m["yahoo_symbol"]: f"{m.get('change_pct') or 0:+.1f}%,RSI:{(m.get('technicals') or {}).get('rsi_14')}"
        for m in compact_snaps
    }

    context = {
        "trigger": trigger_reason,
        "macro": macro_context or {},
        "cash": {
            "free": cash_data.get("free"),
            "invested": cash_data.get("invested"),
            "ppl": cash_data.get("ppl"),
            "total": cash_data.get("total"),
        },
        "positions": [
            {
                "ticker": p.get("ticker"),
                "qty": p.get("quantity"),
                "avgPx": p.get("averagePrice"),
                "curPx": p.get("currentPrice"),
                "ppl": p.get("ppl"),
            }
            for p in positions
        ],
        "focus_tickers": detailed_market,
        "other_watchlist": other_watchlist,
    }

    return (
        "You are a disciplined portfolio risk analyst assisting a human trader on Trading 212.\n"
        "Synthesize the macro regime (VIX, 10Y yield, DXY, GBP/USD, Gold, Oil), "
        "technical indicators (RSI-14, SMA-50/200 trend, 52w high distance, volume ratio), "
        "news catalysts, and portfolio cash/P&L below.\n"
        "Return ONLY valid JSON with this exact schema:\n"
        "{\n"
        '  "portfolio_pnl_impact": "concise 1-2 sentence summary including macro backdrop",\n'
        '  "key_news_catalysts": ["top 1-5 highest-impact catalysts only, ranked by importance"],\n'
        '  "risk_flags": ["top 1-5 most important risks only (macro/technical/concentration), ranked by severity"],\n'
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
        "- For HOLD ideas, set quantity to 0.\n"
        "- Return at most 5 items in key_news_catalysts and at most 5 items in risk_flags, dynamically ranked by importance.\n\n"
        f"DATA:\n{json.dumps(context, separators=(',', ':'))}"
    )


def _format_briefing_markdown(data: dict[str, Any]) -> str:
    lines = [
        "### Portfolio P&L Impact",
        data.get("portfolio_pnl_impact", "N/A"),
        "",
        "### Key News Catalysts (Top 5)",
    ]
    catalysts = (data.get("key_news_catalysts") or ["None flagged"])[:5]
    for c in catalysts:
        lines.append(f"- {c}")

    lines.extend(["", "### Top Risk Flags (Top 5)"])
    risks = (data.get("risk_flags") or ["None flagged"])[:5]
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
    """Deterministic briefing used when LLM_API_KEY is not configured or LLM is unavailable."""
    free_cash = float(cash_data.get("free", 0.0) or 0.0)
    ppl = float(cash_data.get("ppl", 0.0) or 0.0)
    held_tickers = {p.get("ticker") for p in positions if p.get("ticker")}

    movers = sorted(
        [m for m in market_snapshots if m.get("change_pct") is not None],
        key=lambda x: abs(float(x["change_pct"])),
        reverse=True,
    )
    top_str = ", ".join(f"{m['yahoo_symbol']} ({m['change_pct']:+.2f}%)" for m in movers[:5])

    high_signal_words = (
        "fda", "earnings", "guidance", "acquisition", "merger", "award",
        "contract", "downgrade", "upgrade", "sec ", "investigation", "bankruptcy",
    )
    scored_catalysts: list[tuple[float, str]] = []
    for m in market_snapshots:
        is_held = 100.0 if m.get("t212_ticker") in held_tickers else 0.0
        move_score = abs(float(m.get("change_pct") or 0.0))
        for title in m.get("catalysts", [])[:2]:
            kw_bonus = 10.0 if any(w in title.lower() for w in high_signal_words) else 0.0
            scored_catalysts.append((is_held + move_score + kw_bonus, f"{m['yahoo_symbol']}: {title}"))

    scored_catalysts.sort(key=lambda x: x[0], reverse=True)
    catalysts = [text for _score, text in scored_catalysts[:5]]

    risk_flags: list[str] = []
    if free_cash < 100:
        risk_flags.append(f"Low free cash buffer ({free_cash:.2f}).")
    if not positions:
        risk_flags.append("No open positions currently held in Trading 212 account.")
    for m in movers[:3]:
        chg = float(m.get("change_pct") or 0.0)
        tech = m.get("technicals") or {}
        rsi = tech.get("rsi_14")
        if abs(chg) >= 5.0:
            risk_flags.append(f"High volatility on {m['yahoo_symbol']} ({chg:+.2f}% today).")
        elif rsi is not None and (rsi >= 70 or rsi <= 30):
            state = "overbought" if rsi >= 70 else "oversold"
            risk_flags.append(f"{m['yahoo_symbol']} RSI-14 at {rsi:.1f} ({state}).")

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
                        f"Fallback mode. Trigger: {trigger_reason}. "
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
        "risk_flags": (risk_flags or ["LLM_API_KEY not set — running deterministic summary mode."])[:5],
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
    cleaned_flags = [
        f for f in raw.get("risk_flags", []) if "LLM_API_KEY not set" not in f
    ]
    raw["risk_flags"] = [f"LLM temporarily unavailable ({last_error[:120]}); fallback summary used."] + cleaned_flags
    return _format_briefing_markdown(raw), raw
