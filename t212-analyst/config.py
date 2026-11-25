"""Centralized configuration loader for t212-analyst."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    t212_api_key: str
    t212_api_secret: str
    t212_env: str

    monitor_interval_minutes: int
    market_hours_only: bool
    market_timezone: str
    market_open_hour: int
    market_open_minute: int
    market_close_hour: int
    market_close_minute: int

    price_move_threshold_pct: float
    daily_summary_only: bool
    scheduled_briefing_times: tuple[str, ...]
    daily_summary_hour: int
    daily_summary_minute: int

    llm_api_key: str
    llm_base_url: str
    llm_model: str

    discord_webhook_url: str
    telegram_bot_token: str
    telegram_chat_id: str

    db_path: Path
    watchlist_path: Path
    log_level: str

    account_currency: str = "GBP"
    cash_buffer_pct: float = 5.0
    max_single_buy_pct: float = 50.0
    min_trade_amount: float = 5.0

    @property
    def t212_base_url(self) -> str:
        if self.t212_env == "live":
            return "https://live.trading212.com/api/v0"
        return "https://demo.trading212.com/api/v0"


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from .env and environment variables."""
    load_dotenv(dotenv_path=env_file or (BASE_DIR / ".env"), override=False)

    t212_env = os.getenv("T212_ENV", "demo").strip().lower()
    if t212_env not in {"demo", "live"}:
        raise ValueError(f"T212_ENV must be 'demo' or 'live', got {t212_env!r}")

    db_raw = Path(os.getenv("DB_PATH", "data/t212_analyst.db"))
    db_path = db_raw if db_raw.is_absolute() else BASE_DIR / db_raw

    wl_raw = Path(os.getenv("WATCHLIST_PATH", "watchlist.json"))
    watchlist_path = wl_raw if wl_raw.is_absolute() else BASE_DIR / wl_raw

    return Settings(
        t212_api_key=os.getenv("T212_API_KEY", "").strip(),
        t212_api_secret=os.getenv("T212_API_SECRET", "").strip(),
        t212_env=t212_env,
        monitor_interval_minutes=int(os.getenv("MONITOR_INTERVAL_MINUTES", "15")),
        market_hours_only=_parse_bool(os.getenv("MARKET_HOURS_ONLY"), default=True),
        market_timezone=os.getenv("MARKET_TIMEZONE", "America/New_York").strip(),
        market_open_hour=int(os.getenv("MARKET_OPEN_HOUR", "9")),
        market_open_minute=int(os.getenv("MARKET_OPEN_MINUTE", "30")),
        market_close_hour=int(os.getenv("MARKET_CLOSE_HOUR", "16")),
        market_close_minute=int(os.getenv("MARKET_CLOSE_MINUTE", "0")),
        price_move_threshold_pct=float(os.getenv("PRICE_MOVE_THRESHOLD_PCT", "2.0")),
        daily_summary_only=_parse_bool(os.getenv("DAILY_SUMMARY_ONLY"), default=False),
        scheduled_briefing_times=tuple(
            t.strip()
            for t in os.getenv("SCHEDULED_BRIEFING_TIMES", "10:00,15:30").split(",")
            if t.strip()
        ),
        daily_summary_hour=int(os.getenv("DAILY_SUMMARY_HOUR", "16")),
        daily_summary_minute=int(os.getenv("DAILY_SUMMARY_MINUTE", "15")),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip(),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini").strip(),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        db_path=db_path,
        watchlist_path=watchlist_path,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        account_currency=os.getenv("ACCOUNT_CURRENCY", "GBP").strip().upper() or "GBP",
        cash_buffer_pct=float(os.getenv("CASH_BUFFER_PCT", "5.0")),
        max_single_buy_pct=float(os.getenv("MAX_SINGLE_BUY_PCT", "50.0")),
        min_trade_amount=float(os.getenv("MIN_TRADE_AMOUNT", "5.0")),
    )


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
