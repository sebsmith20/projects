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


def _is_symbol_on_cooldown(
    sym: str,
    recent_briefings: list[dict[str, str]] | None,
    ref_now: datetime,
    cooldown_minutes: int,
) -> bool:
    if not recent_briefings or cooldown_minutes <= 0:
        return False
    ref_utc = ref_now.astimezone(ZoneInfo("UTC")) if ref_now.tzinfo else ref_now.replace(tzinfo=ZoneInfo("UTC"))
    prefix = f"{sym} "
    for ev in recent_briefings:
        reason = str(ev.get("trigger_reason") or "")
        if prefix not in reason and not reason.startswith(prefix):
            continue
        ts_raw = str(ev.get("timestamp") or "")
        try:
            ev_dt = datetime.fromisoformat(ts_raw)
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=ZoneInfo("UTC"))
            age_min = (ref_utc - ev_dt.astimezone(ZoneInfo("UTC"))).total_seconds() / 60.0
            if 0.0 <= age_min < cooldown_minutes:
                return True
        except ValueError:
            continue
    return False


def evaluate_trigger(
    market_snapshots: list[dict[str, Any]],
    last_briefing_date: str | None,
    settings: Settings,
    now: datetime | None = None,
    todays_triggers: list[str] | None = None,
    recent_briefings: list[dict[str, str]] | None = None,
) -> str | None:
    """Return a trigger reason string if an LLM analysis should run, else None."""
    reasons: list[str] = []
    tz = ZoneInfo(settings.market_timezone)
    local_now = now.astimezone(tz) if now else datetime.now(tz)
    today_str = local_now.strftime("%Y-%m-%d")

    if not settings.daily_summary_only:
        for snap in market_snapshots:
            sym = snap["yahoo_symbol"]
            if _is_symbol_on_cooldown(
                sym, recent_briefings, local_now, settings.event_trigger_cooldown_minutes
            ):
                continue
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


def _extract_gbpusd_rate(macro_context: dict[str, Any] | None) -> float | None:
    if not macro_context:
        return None
    for ind in macro_context.get("indicators") or []:
        if ind.get("symbol") == "GBPUSD=X":
            try:
                val = float(ind.get("value", 0) or 0)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
    return None


def _build_snapshot_lookup(market_snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for m in market_snapshots:
        if m.get("t212_ticker"):
            lookup[str(m["t212_ticker"])] = m
        if m.get("yahoo_symbol"):
            lookup[str(m["yahoo_symbol"])] = m
    return lookup


def _infer_quote_currency(
    snap: dict[str, Any] | None,
    t212_ticker: str | None,
    yahoo_symbol: str | None,
) -> str | None:
    if snap and snap.get("currency"):
        return str(snap["currency"])
    t212 = str(t212_ticker or "")
    ys = str(yahoo_symbol or "")
    if t212.endswith("_US_EQ"):
        return "USD"
    if t212.endswith("_L_EQ") or ys.endswith(".L"):
        return "GBP"
    if ys and "." not in ys:
        return "USD"
    return None


def _convert_to_account_currency(
    price: float,
    quote_currency: str | None,
    account_currency: str,
    gbpusd_rate: float | None,
) -> float:
    if quote_currency == "GBp":
        price_src = price / 100.0
        src_ccy = "GBP"
    else:
        price_src = price
        src_ccy = (quote_currency or account_currency).upper()

    acct = account_currency.upper()
    if src_ccy == acct or not gbpusd_rate:
        return price_src
    if src_ccy == "USD" and acct == "GBP":
        return price_src / gbpusd_rate
    if src_ccy == "GBP" and acct == "USD":
        return price_src * gbpusd_rate
    return price_src


def _estimate_pending_buy_reserved(
    todays_proposals: list[dict[str, Any]] | None,
    snap_lookup: dict[str, dict[str, Any]],
    account_currency: str,
    gbpusd_rate: float | None,
) -> float:
    reserved = 0.0
    for prop in todays_proposals or []:
        if str(prop.get("status", "")).upper() != "PENDING":
            continue
        if str(prop.get("action", "")).upper() != "BUY":
            continue
        try:
            qty = float(prop.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        t212 = prop.get("t212_ticker")
        ys = prop.get("yahoo_symbol")
        snap = snap_lookup.get(str(t212 or "")) or snap_lookup.get(str(ys or ""))
        otype = str(prop.get("order_type", "MARKET")).upper()
        lprice = prop.get("limit_price")
        quote_price: float | None = None
        if otype == "LIMIT" and lprice is not None:
            try:
                lp = float(lprice)
                if lp > 0:
                    quote_price = lp
            except (TypeError, ValueError):
                pass
        if quote_price is None and snap and snap.get("price") is not None:
            try:
                sp = float(snap["price"])
                if sp > 0:
                    quote_price = sp
            except (TypeError, ValueError):
                pass
        if quote_price is None:
            continue
        quote_ccy = _infer_quote_currency(snap, t212, ys)
        price_acct = _convert_to_account_currency(quote_price, quote_ccy, account_currency, gbpusd_rate)
        reserved += qty * price_acct
    return reserved


def _apply_dynamic_buy_sizing(
    raw: dict[str, Any],
    cash_data: dict[str, Any],
    market_snapshots: list[dict[str, Any]],
    settings: Settings,
    macro_context: dict[str, Any] | None = None,
    todays_proposals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministically size BUY trade ideas from available free cash and split across ideas."""
    ideas = raw.get("trade_ideas")
    if not isinstance(ideas, list) or not ideas:
        return raw

    buy_ideas = [i for i in ideas if str(i.get("action", "")).upper() == "BUY"]
    if not buy_ideas:
        return raw

    snap_lookup = _build_snapshot_lookup(market_snapshots)
    gbpusd_rate = _extract_gbpusd_rate(macro_context)
    free_cash = float(cash_data.get("free", 0.0) or 0.0)
    pending_reserved = _estimate_pending_buy_reserved(
        todays_proposals, snap_lookup, settings.account_currency, gbpusd_rate
    )
    available_cash = max(0.0, free_cash - pending_reserved)
    buffer_factor = max(0.0, 1.0 - (settings.cash_buffer_pct / 100.0))
    deployable_cash = available_cash * buffer_factor

    if deployable_cash < settings.min_trade_amount:
        for idea in buy_ideas:
            idea["quantity"] = 0.0
            idea["allocation_pct"] = 0.0
        return raw

    parsed_allocs: list[float | None] = []
    for idea in buy_ideas:
        raw_alloc = idea.get("allocation_pct")
        val: float | None = None
        if raw_alloc is not None:
            try:
                f = float(raw_alloc)
                if f > 0:
                    val = f
            except (TypeError, ValueError):
                pass
        parsed_allocs.append(val)

    equal_share = 100.0 / len(buy_ideas)
    allocs = [a if a is not None else equal_share for a in parsed_allocs]

    if settings.max_single_buy_pct > 0:
        allocs = [min(a, settings.max_single_buy_pct) for a in allocs]

    total_alloc = sum(allocs)
    if total_alloc > 100.0 and total_alloc > 0:
        scale = 100.0 / total_alloc
        allocs = [a * scale for a in allocs]

    for idea, alloc_pct in zip(buy_ideas, allocs):
        target_cash = deployable_cash * (alloc_pct / 100.0)
        if target_cash < settings.min_trade_amount:
            idea["quantity"] = 0.0
            idea["allocation_pct"] = 0.0
            continue

        t212 = idea.get("t212_ticker")
        ys = idea.get("yahoo_symbol")
        snap = snap_lookup.get(str(t212 or "")) or snap_lookup.get(str(ys or ""))
        otype = str(idea.get("order_type", "MARKET")).upper()
        lprice = idea.get("limit_price")

        quote_price: float | None = None
        if otype == "LIMIT" and lprice is not None:
            try:
                lp = float(lprice)
                if lp > 0:
                    quote_price = lp
            except (TypeError, ValueError):
                pass
        if quote_price is None and snap and snap.get("price") is not None:
            try:
                sp = float(snap["price"])
                if sp > 0:
                    quote_price = sp
            except (TypeError, ValueError):
                pass

        if quote_price is None or quote_price <= 0:
            logger.warning("Cannot dynamically size BUY for %s: missing price.", t212 or ys)
            continue

        quote_ccy = _infer_quote_currency(snap, t212, ys)
        price_acct = _convert_to_account_currency(
            quote_price, quote_ccy, settings.account_currency, gbpusd_rate
        )
        if price_acct <= 0:
            continue

        qty = round(target_cash / price_acct, 4)
        if qty <= 0:
            idea["quantity"] = 0.0
            idea["allocation_pct"] = 0.0
            continue

        idea["quantity"] = qty
        idea["allocation_pct"] = round(alloc_pct, 1)
        idea["estimated_cost"] = round(qty * price_acct, 2)
        idea["account_currency"] = settings.account_currency

    return raw


def _select_sector_diversified_focus(
    market_snapshots: list[dict[str, Any]],
    held_tickers: set[str],
    limit: int = 20,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select focus tickers ensuring held positions, every sector, and top performers are represented."""
    def _rank_key(m: dict[str, Any]) -> tuple[int, float, int, float]:
        is_held = 1 if m.get("t212_ticker") in held_tickers else 0
        has_cat = 1 if m.get("catalysts") else 0
        chg = abs(float(m.get("change_pct") or 0.0))
        tech = m.get("technicals") or {}

        ret5d = float(tech.get("return_5d_pct") or 0.0)
        ret20d = float(tech.get("return_20d_pct") or 0.0)
        ret1y = float(tech.get("return_1y_pct") or 0.0)
        ret5y = float(tech.get("return_5y_pct") or 0.0)

        # Detect buying opportunity on a strong 5-year compounder (pullback/dip)
        rsi = tech.get("rsi_14")
        pct_52w = tech.get("pct_from_52w_high")
        px = m.get("price")
        sma50 = tech.get("sma_50")
        is_pullback = (
            (rsi is not None and float(rsi) <= 48.0)
            or (pct_52w is not None and float(pct_52w) <= -8.0)
            or (px is not None and sma50 is not None and float(px) < float(sma50))
        )
        compounder_dip_bonus = 50.0 if (ret5y >= 50.0 and is_pullback) else 0.0

        perf_score = (
            compounder_dip_bonus
            + max(ret20d, 0.0) * 1.5
            + max(ret5d, 0.0) * 1.2
            + max(ret1y, 0.0) * 0.25
            + max(ret5y, 0.0) * 0.15
            + (15.0 if has_cat else 0.0)
            + chg
        )
        return (is_held, round(perf_score, 4), has_cat, chg)

    ranked = sorted(market_snapshots, key=_rank_key, reverse=True)
    selected: list[dict[str, Any]] = []
    selected_syms: set[str] = set()

    # 1. Always include held positions first
    for m in ranked:
        if m.get("t212_ticker") in held_tickers and len(selected) < limit:
            selected.append(m)
            selected_syms.add(m["yahoo_symbol"])

    # 2. Group remaining snapshots by sector and pick round-robin
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for m in ranked:
        if m["yahoo_symbol"] in selected_syms:
            continue
        sec = str(m.get("sector") or "Other")
        by_sector.setdefault(sec, []).append(m)

    # Order sectors by their strongest candidate first
    sector_order = sorted(
        by_sector.keys(),
        key=lambda s: _rank_key(by_sector[s][0]),
        reverse=True,
    )

    idx = 0
    while len(selected) < limit:
        added_this_round = False
        for sec in sector_order:
            bucket = by_sector[sec]
            if idx < len(bucket) and len(selected) < limit:
                m = bucket[idx]
                selected.append(m)
                selected_syms.add(m["yahoo_symbol"])
                added_this_round = True
        if not added_this_round:
            break
        idx += 1

    compact = [m for m in ranked if m["yahoo_symbol"] not in selected_syms]
    return selected, compact


def _build_llm_prompt(
    positions: list[dict[str, Any]],
    cash_data: dict[str, Any],
    market_snapshots: list[dict[str, Any]],
    trigger_reason: str,
    macro_context: dict[str, Any] | None = None,
    todays_proposals: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
) -> str:
    held_tickers = {p.get("ticker") for p in positions if p.get("ticker")}
    detailed_snaps, compact_snaps = _select_sector_diversified_focus(
        market_snapshots, held_tickers, limit=12
    )

    detailed_market = []
    for m in detailed_snaps:
        t = m.get("technicals") or {}
        cats = [c[:75] for c in (m.get("catalysts") or [])[:1]]
        cat_set = set(m.get("catalysts") or [])
        news = [
            n["title"][:75]
            for n in (m.get("news") or [])
            if n.get("title") and n["title"] not in cat_set
        ][:1]
        entry: dict[str, Any] = {
            "sym": m["yahoo_symbol"],
            "t212": m.get("t212_ticker"),
            "sec": m.get("sector", "Other"),
            "px": m.get("price"),
            "chg%": m.get("change_pct"),
            "5d%": t.get("return_5d_pct"),
            "20d%": t.get("return_20d_pct"),
            "1y%": t.get("return_1y_pct"),
            "5y%": t.get("return_5y_pct"),
            "rsi": t.get("rsi_14"),
            "sma20": t.get("sma_20"),
            "sma50": t.get("sma_50"),
            "sma200": t.get("sma_200"),
            "52wH%": t.get("pct_from_52w_high"),
            "volX": t.get("volume_ratio_20d"),
        }
        if cats:
            entry["cat"] = cats
        if news:
            entry["news"] = news
        detailed_market.append({k: v for k, v in entry.items() if v is not None})

    other_watchlist = {}
    for m in compact_snaps:
        t = m.get("technicals") or {}
        sec_short = str(m.get("sector") or "Oth")[:5]
        parts = [f"{sec_short}:{m.get('change_pct') or 0:+.1f}%"]
        if t.get("return_20d_pct") is not None:
            parts.append(f"20d:{t['return_20d_pct']}%")
        if t.get("return_5y_pct") is not None:
            parts.append(f"5y:{t['return_5y_pct']}%")
        if t.get("rsi_14") is not None:
            parts.append(f"R:{t['rsi_14']}")
        other_watchlist[m["yahoo_symbol"]] = ",".join(parts)

    acct_ccy = settings.account_currency if settings else "GBP"
    max_single_pct = settings.max_single_buy_pct if settings else 50.0
    snap_lookup = _build_snapshot_lookup(market_snapshots)
    gbpusd_rate = _extract_gbpusd_rate(macro_context)
    free_cash = float(cash_data.get("free", 0.0) or 0.0)
    pending_reserved = _estimate_pending_buy_reserved(
        todays_proposals, snap_lookup, acct_ccy, gbpusd_rate
    )
    available_after_pending = round(max(0.0, free_cash - pending_reserved), 2)

    compact_prior = [
        {
            "id": p.get("daily_num"),
            "t212": p.get("t212_ticker"),
            "act": p.get("action"),
            "qty": p.get("quantity"),
            "st": p.get("status"),
        }
        for p in (todays_proposals or [])[-10:]
    ]

    context = {
        "trigger": trigger_reason,
        "macro": macro_context or {},
        "cash": {
            "currency": acct_ccy,
            "free": cash_data.get("free"),
            "pending_buy_reserved": round(pending_reserved, 2),
            "available_after_pending": available_after_pending,
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
        "todays_prior_proposals": compact_prior,
        "focus_tickers": detailed_market,
        "other_watchlist": other_watchlist,
    }

    return (
        "You are a disciplined portfolio risk analyst assisting a human trader on Trading 212.\n"
        "Synthesize the macro regime (VIX, 10Y yield, DXY, GBP/USD, Gold, Oil), "
        "technical indicators (RSI-14, SMA-20/50/200 trend, 52w high distance, volume ratio), "
        "short-term vs 5-year return track record, news catalysts, and portfolio cash/P&L below.\n"
        "Return ONLY valid JSON with this exact schema:\n"
        "{\n"
        '  "portfolio_pnl_impact": "concise 1-2 sentence summary including macro backdrop",\n'
        '  "key_news_catalysts": ["top 1-5 highest-impact catalysts only, ranked by importance"],\n'
        '  "risk_flags": ["top 1-5 most important risks only (macro/technical/concentration), ranked by severity"],\n'
        "  \"trade_ideas\": [\n"
        "    {\n"
        '      "t212_ticker": "<exact t212 code from DATA>",\n'
        '      "yahoo_symbol": "<exact sym from DATA>",\n'
        '      "action": "BUY" | "SELL" | "HOLD",\n'
        '      "allocation_pct": 25.0,\n'
        '      "quantity": 0.0,\n'
        '      "order_type": "MARKET" | "LIMIT",\n'
        '      "limit_price": null,\n'
        '      "reasoning": "concise rationale citing technicals, macro, and catalysts"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Rules:\n"
        "- Scan ALL focus_tickers, positions, and other_watchlist across MULTIPLE sectors (Tech/Semis, Healthcare, Financials, Energy/Nuclear, Defense, Consumer Staples, Broad ETFs/Gold) — do NOT over-concentrate BUY ideas in a single sector.\n"
        "- Prioritize the best-performing stocks in each sector when diversifying: compare short-term percentage return (`ret5d%`/`ret20d%`) against 5-year performance (`ret5y%`/`ret1y%`). Favor a short-term momentum leader when it offers a stronger near-term percentage return setup, BUT if a proven 5-year strong performer (`ret5y%`) is at an attractive buying opportunity (e.g., pullback toward SMA-20/50, RSI-14 <= 48, or meaningful discount from 52w high), buy that 5-year compounder instead.\n"
        "- Coordinate with todays_prior_proposals (from earlier today, e.g. the 10:00 ET open run): do NOT duplicate already-PENDING or EXECUTED buys for the same ticker today.\n"
        "- Wait for better opportunities: you are NOT required to spend cash every run. If a stock is extended/overbought (e.g. RSI-14 > 65 or far above SMA-20/50) or macro risk is elevated, advise holding cash (HOLD) or propose a LIMIT buy at a pullback support level (e.g. near SMA-20 or SMA-50) instead of a MARKET buy.\n"
        "- Pick every trade_idea warranted by factoring in risk (VIX, 10Y yield, volatility, cash/position sizing), news catalysts, and technical indicators. Do NOT artificially limit the number of trade ideas.\n"
        f"- For BUY ideas, Trading 212 supports fractional shares: set `allocation_pct` (between 5 and {max_single_pct:.0f}% of `available_after_pending` cash) based on conviction and risk, splitting the cash pot across your BUY ideas so the total `allocation_pct` across all BUY ideas is <= 100%. Exact fractional `quantity` will be calculated dynamically from `allocation_pct` and `available_after_pending`.\n"
        "- For SELL ideas, set `allocation_pct` to 0 and set `quantity` up to currently held shares.\n"
        "- For HOLD ideas, set `quantity` to 0 and `allocation_pct` to 0.\n"
        "- Return at most 5 items in key_news_catalysts and at most 5 items in risk_flags, dynamically ranked by importance.\n\n"
        f"DATA:\n{json.dumps(context, separators=(',', ':'))}"
    )


def _build_name_map(market_snapshots: list[dict[str, Any]]) -> dict[str, str]:
    name_map: dict[str, str] = {}
    for m in market_snapshots:
        ys = m.get("yahoo_symbol", "")
        t212 = m.get("t212_ticker")
        raw_name = m.get("name") or ys
        label = f"{raw_name} ({ys})" if raw_name and raw_name != ys else ys
        if ys:
            name_map[ys] = label
        if t212:
            name_map[t212] = label
    return name_map


def _format_briefing_markdown(
    data: dict[str, Any], name_map: dict[str, str] | None = None
) -> str:
    names = name_map or {}
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
            t212 = idea.get("t212_ticker")
            ys = idea.get("yahoo_symbol")
            raw_id = t212 or ys or "UNKNOWN"
            display_name = names.get(raw_id) or (names.get(ys, raw_id) if ys else raw_id)
            qty = idea.get("quantity", 0)
            otype = idea.get("order_type", "MARKET")
            lprice = idea.get("limit_price")
            price_str = f" @ {lprice}" if otype == "LIMIT" and lprice else ""
            est_cost = idea.get("estimated_cost")
            alloc_pct = idea.get("allocation_pct")
            ccy = idea.get("account_currency", "")
            size_str = ""
            if action == "BUY" and est_cost:
                pct_part = f", {alloc_pct:.0f}% pot" if alloc_pct else ""
                size_str = f", ~{est_cost:.2f} {ccy}{pct_part}".rstrip()
            lines.append(
                f"- **{action} {qty} {display_name}** ({otype}{price_str}{size_str}): {idea.get('reasoning', '')}"
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
    todays_proposals: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run LLM (or deterministic fallback) and return (markdown, raw_dict)."""
    name_map = _build_name_map(market_snapshots)
    if not settings.llm_api_key:
        logger.info("LLM_API_KEY not set; using deterministic fallback briefing.")
        raw = _fallback_rule_based_analysis(positions, cash_data, market_snapshots, trigger_reason)
        return _format_briefing_markdown(raw, name_map), raw

    base_url = settings.llm_base_url.rstrip("/")
    prompt = _build_llm_prompt(
        positions,
        cash_data,
        market_snapshots,
        trigger_reason,
        macro_context=macro_context,
        todays_proposals=todays_proposals,
        settings=settings,
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
                    "temperature": 0.0,
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
                raw = _apply_dynamic_buy_sizing(
                    raw=raw,
                    cash_data=cash_data,
                    market_snapshots=market_snapshots,
                    settings=settings,
                    macro_context=macro_context,
                    todays_proposals=todays_proposals,
                )
                return _format_briefing_markdown(raw, name_map), raw

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
    return _format_briefing_markdown(raw, name_map), raw
