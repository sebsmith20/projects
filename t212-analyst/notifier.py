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
) -> str:
    lines = [
        f"=== T212 Market Briefing ({env.upper()}) ===",
        f"Trigger: {trigger_reason}",
        "",
        summary_markdown.strip(),
    ]
    if proposals:
        lines.append("")
        lines.append("Pending Trade Proposals (tap or reply to approve):")
        for p in proposals:
            pid = p["id"]
            action = p["action"]
            qty = p["quantity"]
            sym = p.get("yahoo_symbol") or p["t212_ticker"]
            otype = p["order_type"]
            lines.append(
                f"  - #{pid}: {action} {qty} {sym} ({otype}) -> /approve {pid} | /reject {pid}"
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
) -> None:
    message = format_notification_message(
        trigger_reason=trigger_reason,
        summary_markdown=summary_markdown,
        proposal_ids=proposal_ids,
        env=settings.t212_env,
        proposals=proposals,
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
