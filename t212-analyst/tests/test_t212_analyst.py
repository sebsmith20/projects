"""Unit tests for t212-analyst core safety, rate-limiting, and workflow logic."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from analyst import evaluate_trigger, generate_briefing  # noqa: E402
from config import Settings, load_settings  # noqa: E402
from executor import approve_and_execute  # noqa: E402
from storage import Storage  # noqa: E402
from t212_client import OrderUncertainError, Trading212Client  # noqa: E402


def _make_settings(tmp_path: Path, **overrides: object) -> Settings:
    base = {
        "t212_api_key": "test_key",
        "t212_api_secret": "test_secret",
        "t212_env": "demo",
        "monitor_interval_minutes": 15,
        "market_hours_only": True,
        "market_timezone": "America/New_York",
        "market_open_hour": 9,
        "market_open_minute": 30,
        "market_close_hour": 16,
        "market_close_minute": 0,
        "price_move_threshold_pct": 2.0,
        "daily_summary_only": False,
        "scheduled_briefing_times": ("10:00", "15:30"),
        "daily_summary_hour": 16,
        "daily_summary_minute": 15,
        "llm_api_key": "",
        "llm_base_url": "https://api.openai.com/v1",
        "llm_model": "gpt-4o-mini",
        "discord_webhook_url": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "db_path": tmp_path / "test.db",
        "watchlist_path": tmp_path / "watchlist.json",
        "log_level": "INFO",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_default_env_is_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T212_ENV", raising=False)
    settings = load_settings(env_file=tmp_path / "nonexistent.env")
    assert settings.t212_env == "demo"
    assert settings.t212_base_url == "https://demo.trading212.com/api/v0"


def test_threshold_gating_skips_quiet_market(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    snaps = [
        {"yahoo_symbol": "AAPL", "change_pct": 0.4, "move_threshold_pct": 2.0, "catalysts": []},
        {"yahoo_symbol": "MSFT", "change_pct": -1.1, "move_threshold_pct": 2.0, "catalysts": []},
    ]
    morning = datetime(2026, 11, 17, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    assert evaluate_trigger(snaps, last_briefing_date="2026-11-16", settings=settings, now=morning) is None


def test_threshold_gating_triggers_on_move_and_daily_summary(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    snaps = [
        {"yahoo_symbol": "NVDA", "change_pct": 3.5, "move_threshold_pct": 2.0, "catalysts": []},
    ]
    morning = datetime(2026, 11, 17, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    reason = evaluate_trigger(snaps, last_briefing_date="2026-11-16", settings=settings, now=morning)
    assert reason is not None and "NVDA moved +3.50%" in reason

    quiet_snaps = [
        {"yahoo_symbol": "AAPL", "change_pct": 0.2, "move_threshold_pct": 2.0, "catalysts": []},
    ]
    eod = datetime(2026, 11, 17, 16, 20, tzinfo=ZoneInfo("America/New_York"))
    daily_reason = evaluate_trigger(
        quiet_snaps, last_briefing_date="2026-11-16", settings=settings, now=eod
    )
    assert daily_reason is not None and "Daily summary" in daily_reason


def test_fallback_briefing_without_llm_key(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, llm_api_key="")
    md, raw = generate_briefing(
        settings=settings,
        positions=[{"ticker": "AAPL_US_EQ", "quantity": 2, "averagePrice": 200, "currentPrice": 210, "ppl": 20}],
        cash_data={"free": 500.0, "ppl": 20.0},
        market_snapshots=[{"yahoo_symbol": "AAPL", "t212_ticker": "AAPL_US_EQ", "change_pct": 2.5, "catalysts": []}],
        trigger_reason="AAPL moved +2.50%",
    )
    assert "Portfolio P&L Impact" in md
    assert raw["trade_ideas"][0]["action"] == "HOLD"


def test_sell_order_uses_negative_quantity_and_idempotency(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    _, proposal_ids = storage.save_briefing_and_proposals(
        trigger_reason="Test trigger",
        summary_markdown="Summary",
        raw_payload={},
        ideas=[
            {
                "t212_ticker": "AAPL_US_EQ",
                "yahoo_symbol": "AAPL",
                "action": "SELL",
                "quantity": 2.5,
                "order_type": "MARKET",
                "limit_price": None,
                "reasoning": "Take profits",
            }
        ],
    )
    pid = proposal_ids[0]

    mock_session = MagicMock()
    get_resp = MagicMock(ok=True, status_code=200)
    get_resp.json.return_value = []
    post_resp = MagicMock(ok=True, status_code=200, text='{"id": "ord_123"}')
    post_resp.json.return_value = {"id": "ord_123"}
    mock_session.get.return_value = get_resp
    mock_session.post.return_value = post_resp

    client = Trading212Client("k", "s", session=mock_session)
    with patch("t212_client.time.sleep"):
        ok = approve_and_execute(pid, storage, client, env="demo", skip_confirm=True)
        assert ok is True

        # Second attempt MUST be blocked by idempotency state
        ok_second = approve_and_execute(pid, storage, client, env="demo", skip_confirm=True)
        assert ok_second is False

    assert mock_session.post.call_count == 1
    sent_payload = mock_session.post.call_args.kwargs["json"]
    assert sent_payload == {"ticker": "AAPL_US_EQ", "quantity": -2.5}


def test_timeout_marks_failed_and_never_retries(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    _, proposal_ids = storage.save_briefing_and_proposals(
        trigger_reason="Test timeout",
        summary_markdown="Summary",
        raw_payload={},
        ideas=[
            {
                "t212_ticker": "MSFT_US_EQ",
                "yahoo_symbol": "MSFT",
                "action": "BUY",
                "quantity": 1.0,
                "order_type": "LIMIT",
                "limit_price": 400.0,
                "reasoning": "Dip buy",
            }
        ],
    )
    pid = proposal_ids[0]

    mock_session = MagicMock()
    get_resp = MagicMock(ok=True, status_code=200)
    get_resp.json.return_value = []
    mock_session.get.return_value = get_resp
    mock_session.post.side_effect = requests.Timeout("Read timed out")

    client = Trading212Client("k", "s", session=mock_session)
    with patch("t212_client.time.sleep"):
        ok = approve_and_execute(pid, storage, client, env="demo", skip_confirm=True)
        assert ok is False

    assert mock_session.post.call_count == 1
    prop = storage.get_proposal(pid)
    assert prop is not None and prop["status"] == "FAILED"


def test_client_order_uncertain_error_on_timeout() -> None:
    mock_session = MagicMock()
    mock_session.post.side_effect = requests.Timeout("timed out")
    client = Trading212Client("k", "s", session=mock_session)
    with pytest.raises(OrderUncertainError):
        client.place_market_order("AAPL_US_EQ", 1.0)
