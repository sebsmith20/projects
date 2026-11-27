"""Notification dispatcher for console, Discord, and Telegram."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Sequence

import requests

from config import Settings

logger = logging.getLogger(__name__)


def load_watchlist_name_map(watchlist_path: Path | None) -> dict[str, str]:
    if not watchlist_path or not watchlist_path.exists():
        return {}
    try:
        data = json.loads(watchlist_path.read_text(encoding="utf-8"))
        name_map: dict[str, str] = {}
        for item in data.get("tickers", []):
            ys = str(item.get("yahoo_symbol") or "").strip()
            t212 = str(item.get("t212_ticker") or "").strip()
            raw_name = str(item.get("name") or "").strip()
            label = f"{raw_name} ({ys})" if raw_name and ys and raw_name != ys else (raw_name or ys or t212)
            if ys:
                name_map[ys] = label
            if t212:
                name_map[t212] = label
        return name_map
    except Exception:
        return {}


def format_notification_message(
    trigger_reason: str,
    summary_markdown: str,
    proposal_ids: Sequence[int],
    env: str,
    proposals: Sequence[dict[str, object]] | None = None,
    all_pending_proposals: Sequence[dict[str, object]] | None = None,
    name_map: dict[str, str] | None = None,
) -> str:
    names = name_map or {}
    lines = [
        f"=== T212 Market Briefing ({env.upper()}) ===",
        f"Trigger: {trigger_reason}",
        "",
        summary_markdown.strip(),
    ]
    new_ids = {int(p["id"]) for p in (proposals or []) if p.get("id") is not None} | set(proposal_ids)
    combined = list(all_pending_proposals) if all_pending_proposals is not None else list(proposals or [])

    if combined:
        buy_props = [p for p in combined if str(p.get("action", "")).upper() == "BUY"]
        other_props = [p for p in combined if str(p.get("action", "")).upper() != "BUY"]

        if buy_props:
            lines.append("")
            lines.append("All Today's Pending BUY Suggestions (tap or reply to approve):")
            for p in buy_props:
                pid = p.get("daily_num") or p["id"]
                qty = p["quantity"]
                ys = str(p.get("yahoo_symbol") or "")
                t212 = str(p.get("t212_ticker") or "")
                sym = ys or t212
                display_name = names.get(ys) or names.get(t212) or sym
                otype = p["order_type"]
                lprice = p.get("limit_price")
                limit_str = f" @ {lprice}" if otype == "LIMIT" and lprice is not None else ""
                new_tag = " [NEW]" if p.get("id") in new_ids and len(combined) > len(new_ids) else ""
                lines.append(
                    f"  - #{pid}: BUY {qty} {display_name} ({otype}{limit_str}){new_tag} -> /approve {pid} | /reject {pid}"
                )

        if other_props:
            lines.append("")
            lines.append("Pending SELL Suggestions:")
            for p in other_props:
                pid = p.get("daily_num") or p["id"]
                action = p["action"]
                qty = p["quantity"]
                ys = str(p.get("yahoo_symbol") or "")
                t212 = str(p.get("t212_ticker") or "")
                sym = ys or t212
                display_name = names.get(ys) or names.get(t212) or sym
                otype = p["order_type"]
                lprice = p.get("limit_price")
                limit_str = f" @ {lprice}" if otype == "LIMIT" and lprice is not None else ""
                new_tag = " [NEW]" if p.get("id") in new_ids and len(combined) > len(new_ids) else ""
                lines.append(
                    f"  - #{pid}: {action} {qty} {display_name} ({otype}{limit_str}){new_tag} -> /approve {pid} | /reject {pid}"
                )
    elif proposal_ids:
        lines.append("")
        lines.append("Pending Trade Proposals (require explicit human approval):")
        for pid in proposal_ids:
            lines.append(f"  - #{pid}: /approve {pid}  |  /reject {pid}  |  CLI: python executor.py approve {pid}")
    return "\n".join(lines)


def send_notifications(
    settings: Settings,
    trigger_reason: str,
    summary_markdown: str,
    proposal_ids: Sequence[int],
    proposals: Sequence[dict[str, object]] | None = None,
    all_pending_proposals: Sequence[dict[str, object]] | None = None,
    name_map: dict[str, str] | None = None,
) -> None:
    resolved_names = name_map if name_map is not None else load_watchlist_name_map(settings.watchlist_path)
    message = format_notification_message(
        trigger_reason=trigger_reason,
        summary_markdown=summary_markdown,
        proposal_ids=proposal_ids,
        env=settings.t212_env,
        proposals=proposals,
        all_pending_proposals=all_pending_proposals,
        name_map=resolved_names,
    )

    # 1. Always print to console
    print("\n" + message + "\n")

    # 2. Optional Discord webhook
    if settings.discord_webhook_url:
        try:
            resp = requests.post(
                settings.discord_webhook_url,
                json={"content": message[:1950]},
                timeout=10,
            )
            if not resp.ok:
                logger.warning("Discord webhook returned %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Failed to send Discord notification: %s", exc)

    # 3. Optional Telegram bot webhook
    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
            resp = requests.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": message[:4000],
                },
                timeout=10,
            )
            if not resp.ok:
                logger.warning("Telegram API returned %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Failed to send Telegram notification: %s", exc)
