"""Continuous Market & Portfolio Monitor (read-only + proposal generation)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from analyst import evaluate_trigger, generate_briefing
from config import Settings, configure_logging, load_settings
from market_data import collect_market_universe, fetch_macro_context, load_watchlist
from notifier import send_notifications
from storage import Storage
from t212_client import Trading212Client

logger = logging.getLogger(__name__)
_SHUTDOWN = False


def _handle_signal(signum: int, _frame: object) -> None:
    global _SHUTDOWN
    logger.info("Received signal %s; shutting down cleanly after current cycle.", signum)
    _SHUTDOWN = True


def is_within_market_hours(settings: Settings, now: datetime | None = None) -> bool:
    if not settings.market_hours_only:
        return True
    tz = ZoneInfo(settings.market_timezone)
    local_now = now.astimezone(tz) if now else datetime.now(tz)
    if local_now.weekday() >= 5:
        return False
    current_hm = (local_now.hour, local_now.minute)
    open_hm = (settings.market_open_hour, settings.market_open_minute)
    close_hm = (settings.market_close_hour, settings.market_close_minute)
    return open_hm <= current_hm <= close_hm


def run_cycle(
    settings: Settings,
    client: Trading212Client,
    storage: Storage,
    force_analysis: bool = False,
) -> None:
    logger.info("Starting monitor cycle (env=%s)", settings.t212_env)

    positions = client.get_positions()
    cash_data = client.get_account_cash()
    snap_id = storage.save_portfolio_snapshot(settings.t212_env, cash_data, positions)
    logger.info(
        "Saved portfolio snapshot #%d: %d open positions, free cash=%.2f",
        snap_id,
        len(positions),
        float(cash_data.get("free", 0.0) or 0.0),
    )

    watchlist = load_watchlist(settings.watchlist_path)
    market_snapshots = collect_market_universe(
        positions=positions,
        watchlist=watchlist,
        default_threshold_pct=settings.price_move_threshold_pct,
    )
    storage.save_market_snapshots(market_snapshots)
    logger.info("Saved %d external market/news snapshots.", len(market_snapshots))

    last_date = storage.get_last_briefing_date()
    today_utc_prefix = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
    todays_events = storage.get_todays_briefing_events(today_utc_prefix)
    todays_triggers = [e["trigger_reason"] for e in todays_events]
    trigger_reason = (
        "Manual force-analysis run"
        if force_analysis
        else evaluate_trigger(
            market_snapshots,
            last_date,
            settings,
            todays_triggers=todays_triggers,
            recent_briefings=todays_events,
        )
    )

    if not trigger_reason:
        logger.info("No analysis threshold met; skipping LLM call to save tokens.")
        return

    logger.info("Analysis triggered: %s", trigger_reason)
    macro_context = fetch_macro_context()
    todays_proposals = storage.get_todays_proposals(today_utc_prefix)
    summary_md, raw_payload = generate_briefing(
        settings=settings,
        positions=positions,
        cash_data=cash_data,
        market_snapshots=market_snapshots,
        trigger_reason=trigger_reason,
        macro_context=macro_context,
        todays_proposals=todays_proposals,
    )
    briefing_id, proposal_ids = storage.save_briefing_and_proposals(
        trigger_reason=trigger_reason,
        summary_markdown=summary_md,
        raw_payload=raw_payload,
        ideas=raw_payload.get("trade_ideas", []),
    )
    logger.info(
        "Saved briefing #%d with %d actionable trade proposal(s): %s",
        briefing_id,
        len(proposal_ids),
        proposal_ids,
    )
    created_proposals = [
        p for pid in proposal_ids if (p := storage.get_proposal(pid)) is not None
    ]
    todays_pending = storage.get_todays_pending_proposals(today_utc_prefix)
    send_notifications(
        settings=settings,
        trigger_reason=trigger_reason,
        summary_markdown=summary_md,
        proposal_ids=proposal_ids,
        proposals=created_proposals,
        all_pending_proposals=todays_pending,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Trading 212 Continuous Portfolio & Market Monitor")
    parser.add_argument("--once", action="store_true", help="Run a single monitor cycle and exit.")
    parser.add_argument(
        "--force-analysis",
        action="store_true",
        help="Force an analysis briefing even if price/news thresholds are not met.",
    )
    parser.add_argument(
        "--ignore-market-hours",
        action="store_true",
        help="Run even outside configured market hours.",
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    client = Trading212Client(
        api_key=settings.t212_api_key,
        api_secret=settings.t212_api_secret,
        base_url=settings.t212_base_url,
    )
    storage = Storage(settings.db_path)

    if args.once:
        if args.ignore_market_hours or is_within_market_hours(settings):
            run_cycle(settings, client, storage, force_analysis=args.force_analysis)
        else:
            logger.info("Outside configured market hours; skipping (--ignore-market-hours to override).")
        return 0

    interval_sec = max(60, settings.monitor_interval_minutes * 60)
    logger.info(
        "Monitor daemon started (env=%s, interval=%dm, market_hours_only=%s)",
        settings.t212_env,
        settings.monitor_interval_minutes,
        settings.market_hours_only,
    )

    while not _SHUTDOWN:
        try:
            if args.ignore_market_hours or is_within_market_hours(settings):
                run_cycle(settings, client, storage, force_analysis=args.force_analysis)
            else:
                logger.debug("Outside market hours; sleeping.")
        except Exception:
            logger.exception("Error during monitor cycle; will retry next interval.")

        slept = 0
        while slept < interval_sec and not _SHUTDOWN:
            time.sleep(min(5, interval_sec - slept))
            slept += 5

    logger.info("Monitor stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
