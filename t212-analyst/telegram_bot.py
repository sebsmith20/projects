"""Telegram Human-in-the-Loop Bot for approving/rejecting trade proposals from your phone.

Security: ONLY responds to messages from the configured TELEGRAM_CHAT_ID.
Uses pure-Python `requests` long-polling (no extra dependencies or native DLLs).
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

import requests

from config import configure_logging, load_settings
from executor import approve_and_execute
from notifier import load_watchlist_name_map
from storage import Storage
from t212_client import Trading212Client

logger = logging.getLogger(__name__)
_SHUTDOWN = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _SHUTDOWN
    logger.info("Received signal %s; stopping Telegram bot.", signum)
    _SHUTDOWN = True


class TelegramBot:
    def __init__(
        self,
        bot_token: str,
        allowed_chat_id: str,
        storage: Storage,
        client: Trading212Client,
        env: str,
        watchlist_path: Path | None = None,
    ) -> None:
        if not bot_token or not allowed_chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env.")
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.allowed_chat_id = str(allowed_chat_id).strip()
        self.storage = storage
        self.client = client
        self.env = env
        self.watchlist_path = watchlist_path
        self._offset: int | None = None

    def _display_name(self, prop: dict[str, Any], names: dict[str, str]) -> str:
        ys = str(prop.get("yahoo_symbol") or "")
        t212 = str(prop.get("t212_ticker") or "")
        return names.get(ys) or names.get(t212) or ys or t212

    def send_text(self, text: str) -> None:
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.allowed_chat_id, "text": text[:4000]},
                timeout=15,
            )
            if not resp.ok:
                logger.warning("Telegram sendMessage failed (%s): %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Telegram sendMessage error: %s", exc)

    def _handle_command(self, text: str) -> str:
        parts = text.strip().split()
        if not parts:
            return "Send /help for available commands."

        cmd = parts[0].lower().split("@")[0]

        if cmd in {"/start", "/help"}:
            return (
                f"T212 Human-in-the-Loop Bot ({self.env.upper()} mode)\n\n"
                "Commands:\n"
                "/list - Show PENDING trade proposals\n"
                "/approve <id> - Approve & submit a PENDING proposal\n"
                "/reject <id> - Reject a PENDING proposal\n"
                "/status - Show recent proposals summary\n"
                "/help - Show this help message"
            )

        names = load_watchlist_name_map(self.watchlist_path)

        if cmd == "/list":
            pending = self.storage.list_proposals(status="PENDING", limit=15)
            if not pending:
                return "No PENDING trade proposals."
            lines = [f"PENDING Proposals ({self.env.upper()}):"]
            for p in pending:
                num = p.get("daily_num") or p["id"]
                limit_str = f" @ {p['limit_price']}" if p["limit_price"] is not None else ""
                dname = self._display_name(p, names)
                lines.append(
                    f"\n#{num} | {p['action']} {p['quantity']} {dname} "
                    f"({p['order_type']}{limit_str})\n"
                    f"Reason: {p['reasoning']}\n"
                    f"-> /approve {num}  or  /reject {num}"
                )
            return "\n".join(lines)

        if cmd == "/status":
            recent = self.storage.list_proposals(limit=10)
            if not recent:
                return "No trade proposals recorded yet."
            lines = [f"Recent Proposals ({self.env.upper()}):"]
            for p in recent:
                num = p.get("daily_num") or p["id"]
                oid = f" (order {p['t212_order_id']})" if p.get("t212_order_id") else ""
                dname = self._display_name(p, names)
                lines.append(
                    f"#{num} [{p['status']}] {p['action']} {p['quantity']} {dname}{oid}"
                )
            return "\n".join(lines)

        if cmd == "/reject":
            if len(parts) != 2 or not parts[1].isdigit():
                return "Usage: /reject <proposal_id>"
            num = int(parts[1])
            pid = self.storage.resolve_proposal_id(num)
            if pid is not None and self.storage.reject_proposal(pid):
                return f"Proposal #{num} marked REJECTED."
            return f"Could not reject #{num} (not found or not PENDING)."

        if cmd == "/approve":
            if len(parts) != 2 or not parts[1].isdigit():
                return "Usage: /approve <proposal_id>"
            num = int(parts[1])
            pid = self.storage.resolve_proposal_id(num)
            prop = self.storage.get_proposal(pid) if pid is not None else None
            if not prop or pid is None:
                return f"Proposal #{num} not found."
            if prop["status"] != "PENDING":
                return f"Refused: Proposal #{num} is '{prop['status']}' (must be PENDING)."

            ok = approve_and_execute(
                proposal_id=pid,
                storage=self.storage,
                client=self.client,
                env=self.env,
                skip_confirm=True,  # Explicit /approve <id> from authorized chat IS the confirmation
            )
            updated = self.storage.get_proposal(pid) or prop
            if ok:
                return (
                    f"EXECUTED #{num} ({self.env.upper()}):\n"
                    f"{updated['action']} {updated['quantity']} {updated['t212_ticker']}\n"
                    f"Trading 212 Order ID: {updated.get('t212_order_id')}"
                )
            return (
                f"FAILED #{num} (status={updated['status']}):\n"
                f"{updated.get('execution_response_json') or 'Check server logs.'}"
            )

        return "Unknown command. Send /help for available commands."

    def poll_once(self, timeout: int = 30) -> None:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if self._offset is not None:
            params["offset"] = self._offset

        resp = requests.get(
            f"{self.base_url}/getUpdates",
            params=params,
            timeout=timeout + 10,
        )
        if not resp.ok:
            logger.warning("getUpdates HTTP %s: %s", resp.status_code, resp.text[:200])
            time.sleep(5)
            return

        data = resp.json()
        for update in data.get("result", []):
            self._offset = int(update["update_id"]) + 1
            msg = update.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", "")).strip()
            text = str(msg.get("text", "")).strip()

            if not chat_id or not text:
                continue

            if chat_id != self.allowed_chat_id:
                logger.warning("Ignored unauthorized Telegram message from chat_id=%s", chat_id)
                continue

            logger.info("Authorized Telegram command: %s", text)
            reply = self._handle_command(text)
            self.send_text(reply)

    def run_forever(self) -> None:
        logger.info(
            "Telegram bot started (env=%s, authorized_chat_id=%s)",
            self.env,
            self.allowed_chat_id,
        )
        self.send_text(f"T212 Telegram Bot online ({self.env.upper()} mode). Send /help for commands.")
        while not _SHUTDOWN:
            try:
                self.poll_once(timeout=30)
            except Exception:
                logger.exception("Error in Telegram polling loop; retrying in 5s.")
                time.sleep(5)


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    storage = Storage(settings.db_path)
    client = Trading212Client(
        api_key=settings.t212_api_key,
        api_secret=settings.t212_api_secret,
        base_url=settings.t212_base_url,
    )
    bot = TelegramBot(
        bot_token=settings.telegram_bot_token,
        allowed_chat_id=settings.telegram_chat_id,
        storage=storage,
        client=client,
        env=settings.t212_env,
        watchlist_path=settings.watchlist_path,
    )
    bot.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
