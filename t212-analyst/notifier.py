"""Notification dispatcher for console, Discord, and Telegram."""

from __future__ import annotations

import logging
from typing import Sequence

import requests

from config import Settings

logger = logging.getLogger(__name__)


def format_notification_message(
    trigger_reason: str,
    summary_markdown: str,
    proposal_ids: Sequence[int],
    env: str,
    proposals: Sequence[dict[str, object]] | None = None,
    all_pending_proposals: Sequence[dict[str, object]] | None = None,
) -> str:
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
                sym = p.get("yahoo_symbol") or p["t212_ticker"]
                otype = p["order_type"]
                lprice = p.get("limit_price")
                limit_str = f" @ {lprice}" if otype == "LIMIT" and lprice is not None else ""
                new_tag = " [NEW]" if p.get("id") in new_ids and len(combined) > len(new_ids) else ""
                lines.append(
                    f"  - #{pid}: BUY {qty} {sym} ({otype}{limit_str}){new_tag} -> /approve {pid} | /reject {pid}"
                )

        if other_props:
            lines.append("")
            lines.append("Pending SELL Suggestions:")
            for p in other_props:
                pid = p.get("daily_num") or p["id"]
                action = p["action"]
                qty = p["quantity"]
                sym = p.get("yahoo_symbol") or p["t212_ticker"]
                otype = p["order_type"]
                lprice = p.get("limit_price")
                limit_str = f" @ {lprice}" if otype == "LIMIT" and lprice is not None else ""
                new_tag = " [NEW]" if p.get("id") in new_ids and len(combined) > len(new_ids) else ""
                lines.append(
                    f"  - #{pid}: {action} {qty} {sym} ({otype}{limit_str}){new_tag} -> /approve {pid} | /reject {pid}"
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
) -> None:
    message = format_notification_message(
        trigger_reason=trigger_reason,
        summary_markdown=summary_markdown,
        proposal_ids=proposal_ids,
        env=settings.t212_env,
        proposals=proposals,
        all_pending_proposals=all_pending_proposals,
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
