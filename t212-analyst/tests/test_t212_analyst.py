"""Unit tests for t212-analyst core safety, rate-limiting, and workflow logic."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

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


def test_dynamic_buy_sizing_splits_cash_pot_and_converts_currency(tmp_path: Path) -> None:
    from analyst import _apply_dynamic_buy_sizing

    settings = _make_settings(
        tmp_path,
        account_currency="GBP",
        cash_buffer_pct=5.0,
        max_single_buy_pct=50.0,
        min_trade_amount=5.0,
    )
    # 1000 GBP free -> 950 GBP deployable after 5% buffer
    cash_data = {"free": 1000.0}
    macro_context = {"indicators": [{"symbol": "GBPUSD=X", "value": 1.25}]}
    market_snapshots = [
        {"yahoo_symbol": "AAPL", "t212_ticker": "AAPL_US_EQ", "price": 250.0, "currency": "USD"},
        {"yahoo_symbol": "AZN.L", "t212_ticker": "AZN_L_EQ", "price": 10000.0, "currency": "GBp"},
    ]
    raw = {
        "trade_ideas": [
            {
                "t212_ticker": "AAPL_US_EQ",
                "yahoo_symbol": "AAPL",
                "action": "BUY",
                "allocation_pct": 40.0,
                "quantity": 1.0,
                "order_type": "MARKET",
                "limit_price": None,
                "reasoning": "Strong momentum",
            },
            {
                "t212_ticker": "AZN_L_EQ",
                "yahoo_symbol": "AZN.L",
                "action": "BUY",
                "allocation_pct": 40.0,
                "quantity": 1.0,
                "order_type": "LIMIT",
                "limit_price": 9500.0,
                "reasoning": "Pullback support",
            },
        ]
    }
    sized = _apply_dynamic_buy_sizing(
        raw=raw,
        cash_data=cash_data,
        market_snapshots=market_snapshots,
        settings=settings,
        macro_context=macro_context,
    )
    aapl, azn = sized["trade_ideas"]
    # AAPL: 40% of 950 GBP = 380 GBP. $250 / 1.25 = 200 GBP/share -> 1.9 shares
    assert aapl["quantity"] == pytest.approx(1.9, rel=1e-4)
    assert aapl["estimated_cost"] == pytest.approx(380.0, rel=1e-2)
    # AZN: 40% of 950 GBP = 380 GBP. 9500 GBp limit = 95 GBP/share -> 4.0 shares
    assert azn["quantity"] == pytest.approx(4.0, rel=1e-4)
    assert azn["estimated_cost"] == pytest.approx(380.0, rel=1e-2)


def test_dynamic_buy_sizing_normalizes_over_100_and_reserves_pending(tmp_path: Path) -> None:
    from analyst import _apply_dynamic_buy_sizing

    settings = _make_settings(
        tmp_path,
        account_currency="USD",
        cash_buffer_pct=0.0,
        max_single_buy_pct=60.0,
        min_trade_amount=5.0,
    )
    # 1000 USD free, but 200 USD already reserved by a PENDING BUY -> 800 USD deployable
    cash_data = {"free": 1000.0}
    market_snapshots = [
        {"yahoo_symbol": "NVDA", "t212_ticker": "NVDA_US_EQ", "price": 100.0, "currency": "USD"},
        {"yahoo_symbol": "MSFT", "t212_ticker": "MSFT_US_EQ", "price": 400.0, "currency": "USD"},
        {"yahoo_symbol": "GOOGL", "t212_ticker": "GOOGL_US_EQ", "price": 200.0, "currency": "USD"},
    ]
    todays_proposals = [
        {
            "t212_ticker": "NVDA_US_EQ",
            "yahoo_symbol": "NVDA",
            "action": "BUY",
            "quantity": 2.0,
            "order_type": "MARKET",
            "limit_price": None,
            "status": "PENDING",
        }
    ]
    # Two BUYs with no allocation_pct -> equal 50/50 split of 800 USD = 400 USD each
    raw = {
        "trade_ideas": [
            {
                "t212_ticker": "MSFT_US_EQ",
                "yahoo_symbol": "MSFT",
                "action": "BUY",
                "quantity": 1.0,
                "order_type": "MARKET",
                "limit_price": None,
                "reasoning": "Cloud growth",
            },
            {
                "t212_ticker": "GOOGL_US_EQ",
                "yahoo_symbol": "GOOGL",
                "action": "BUY",
                "quantity": 1.0,
                "order_type": "MARKET",
                "limit_price": None,
                "reasoning": "AI search",
            },
        ]
    }
    sized = _apply_dynamic_buy_sizing(
        raw=raw,
        cash_data=cash_data,
        market_snapshots=market_snapshots,
        settings=settings,
        todays_proposals=todays_proposals,
    )
    msft, googl = sized["trade_ideas"]
    assert msft["quantity"] == pytest.approx(1.0, rel=1e-4)   # 400 / 400
    assert googl["quantity"] == pytest.approx(2.0, rel=1e-4)  # 400 / 200


def test_sector_diversified_focus_prevents_single_sector_crowding() -> None:
    from analyst import _select_sector_diversified_focus

    # 6 high-volatility Tech stocks with big moves + 1 each from Healthcare, Financials, Defense
    snaps = [
        {"yahoo_symbol": f"TECH{i}", "t212_ticker": f"TECH{i}_US_EQ", "sector": "Technology & AI", "change_pct": 8.0 - i * 0.1, "catalysts": ["news"]}
        for i in range(6)
    ] + [
        {"yahoo_symbol": "LLY", "t212_ticker": "LLY_US_EQ", "sector": "Healthcare", "change_pct": 0.4, "catalysts": []},
        {"yahoo_symbol": "JPM", "t212_ticker": "JPM_US_EQ", "sector": "Financials", "change_pct": 0.3, "catalysts": []},
        {"yahoo_symbol": "LMT", "t212_ticker": "LMT_US_EQ", "sector": "Defense & Space", "change_pct": 0.2, "catalysts": []},
    ]

    selected, compact = _select_sector_diversified_focus(snaps, held_tickers=set(), limit=5)
    selected_sectors = {m["sector"] for m in selected}
    assert selected_sectors == {"Technology & AI", "Healthcare", "Financials", "Defense & Space"}
    assert len(selected) == 5
    assert len(compact) == 4


def test_five_year_compounder_pullback_prioritized_and_returns_computed() -> None:
    from analyst import _select_sector_diversified_focus
    from market_data import _compute_technicals

    # 800 trading days (~3.2y) rising from 100 to 250 (+150%)
    closes = [100.0 + (150.0 * i / 799.0) for i in range(800)]
    tech = _compute_technicals(closes, [1_000_000.0] * 800)
    assert tech["return_5y_pct"] == pytest.approx(150.0, rel=1e-2)
    assert tech["return_1y_pct"] is not None and tech["return_1y_pct"] > 0

    snaps = [
        {
            "yahoo_symbol": "WEAK",
            "t212_ticker": "WEAK_US_EQ",
            "sector": "Healthcare",
            "price": 50.0,
            "change_pct": 1.5,
            "catalysts": [],
            "technicals": {"return_5y_pct": -10.0, "return_20d_pct": 1.0, "rsi_14": 60.0},
        },
        {
            "yahoo_symbol": "LLY",
            "t212_ticker": "LLY_US_EQ",
            "sector": "Healthcare",
            "price": 800.0,
            "change_pct": -0.5,
            "catalysts": [],
            "technicals": {"return_5y_pct": 220.0, "return_20d_pct": -3.0, "rsi_14": 42.0, "sma_50": 830.0},
        },
    ]
    selected, _ = _select_sector_diversified_focus(snaps, held_tickers=set(), limit=1)
    assert selected[0]["yahoo_symbol"] == "LLY"



