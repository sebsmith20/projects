"""Human-in-the-Loop Trade Approval & Execution CLI.

NEVER executes trades automatically. Every order requires explicit human approval
and passes through atomic SQLite state locking + duplicate-order safeguards.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from config import configure_logging, load_settings
from storage import Storage
from t212_client import OrderUncertainError, Trading212Client, Trading212Error

logger = logging.getLogger(__name__)


def _print_proposals(proposals: list[dict[str, Any]]) -> None:
    if not proposals:
        print("No proposals found.")
        return
    print(
        f"{'ID':<4} {'STATUS':<18} {'ACTION':<6} {'QTY':<8} "
        f"{'TICKER':<14} {'TYPE':<7} {'LIMIT':<9} {'REASONING'}"
    )
    print("-" * 100)
    for p in proposals:
        num = p.get("daily_num") or p["id"]
        lprice = f"{p['limit_price']:.2f}" if p["limit_price"] is not None else "-"
        reason = (p["reasoning"] or "")[:45]
        print(
            f"{num:<4} {p['status']:<18} {p['action']:<6} {p['quantity']:<8} "
            f"{p['t212_ticker']:<14} {p['order_type']:<7} {lprice:<9} {reason}"
        )


def approve_and_execute(
    proposal_id: int,
    storage: Storage,
    client: Trading212Client,
    env: str,
    skip_confirm: bool = False,
) -> bool:
    """Explicitly approve and execute a single PENDING proposal with idempotency safeguards."""
    proposal = storage.get_proposal(proposal_id)
    if not proposal:
        print(f"Proposal #{proposal_id} not found.")
        return False

    if proposal["status"] != "PENDING":
        print(
            f"Refusing to execute proposal #{proposal_id}: "
            f"status is '{proposal['status']}' (must be 'PENDING')."
        )
        return False

    action = proposal["action"]
    qty = float(proposal["quantity"])
    signed_qty = -abs(qty) if action == "SELL" else abs(qty)
    ticker = proposal["t212_ticker"]
    order_type = proposal["order_type"]
    limit_price = proposal["limit_price"]

    print("\n--- Order Confirmation ---")
    print(f"Environment   : {env.upper()}")
    print(f"Proposal ID   : {proposal_id}")
    print(f"Idempotency   : {proposal['idempotency_key']}")
    print(f"Action        : {action} {abs(qty)} shares of {ticker} (signed qty={signed_qty})")
    print(f"Order Type    : {order_type}" + (f" @ {limit_price}" if limit_price else ""))
    print(f"Reasoning     : {proposal['reasoning']}")

    if not skip_confirm:
        expected = f"APPROVE {proposal_id}"
        typed = input(f"\nType '{expected}' to submit this order (anything else cancels): ").strip()
        if typed != expected:
            print("Cancelled. Proposal remains PENDING.")
            return False

    # Atomic SQLite lock: PENDING -> APPROVED_IN_FLIGHT
    claimed = storage.claim_proposal_for_execution(proposal_id)
    if not claimed:
        print(
            f"Proposal #{proposal_id} was already claimed or modified by another process. Aborting."
        )
        return False

    # Duplicate-order check against live/demo open orders
    try:
        open_orders = client.get_open_orders()
        for o in open_orders:
            if o.get("ticker") == ticker and float(o.get("quantity", 0)) == signed_qty:
                msg = (
                    f"Duplicate safeguard triggered: open order {o.get('id')} already exists "
                    f"for {ticker} qty={signed_qty}."
                )
                storage.mark_proposal_result(
                    proposal_id,
                    status="FAILED",
                    response_payload={"error": msg, "existing_order": o},
                )
                print(msg)
                return False
    except Trading212Error as exc:
        logger.warning("Could not pre-check open orders (%s); proceeding with caution.", exc)

    # Submit order (NO auto-retry on network timeout)
    try:
        if order_type == "LIMIT":
            if limit_price is None:
                raise ValueError("LIMIT order missing limit_price.")
            result = client.place_limit_order(
                ticker=ticker,
                signed_quantity=signed_qty,
                limit_price=float(limit_price),
            )
        else:
            result = client.place_market_order(
                ticker=ticker,
                signed_quantity=signed_qty,
            )

        order_id = str(result.get("id", "")) or None
        storage.mark_proposal_result(
            proposal_id,
            status="EXECUTED",
            t212_order_id=order_id,
            response_payload=result,
        )
        print(f"Order EXECUTED successfully! Trading 212 Order ID: {order_id}")
        return True

    except OrderUncertainError as exc:
        storage.mark_proposal_result(
            proposal_id,
            status="FAILED",
            response_payload={"uncertain_error": str(exc)},
        )
        print(
            f"\n[CRITICAL] {exc}\n"
            f"Proposal #{proposal_id} marked FAILED to prevent duplicate submission."
        )
        return False

    except (Trading212Error, ValueError) as exc:
        storage.mark_proposal_result(
            proposal_id,
            status="FAILED",
            response_payload={"error": str(exc)},
        )
        print(f"Order FAILED: {exc}")
        return False


def interactive_loop(storage: Storage, client: Trading212Client, env: str) -> int:
    print(f"T212 Human-in-the-Loop Executor ({env.upper()} mode)")
    print("Commands: list | approve <id> | reject <id> | quit\n")
    _print_proposals(storage.list_proposals(status="PENDING"))

    while True:
        try:
            raw = input("\nt212-exec> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return 0

        if not raw:
            continue
        parts = raw.split()
        cmd = parts[0].lower()

        if cmd in {"quit", "exit", "q"}:
            return 0
        if cmd == "list":
            status = parts[1].upper() if len(parts) > 1 else None
            _print_proposals(storage.list_proposals(status=status))
        elif cmd == "approve" and len(parts) == 2 and parts[1].isdigit():
            pid = storage.resolve_proposal_id(int(parts[1]))
            if pid is None:
                print(f"Proposal #{parts[1]} not found.")
            else:
                approve_and_execute(pid, storage, client, env, skip_confirm=False)
        elif cmd == "reject" and len(parts) == 2 and parts[1].isdigit():
            num = int(parts[1])
            pid = storage.resolve_proposal_id(num)
            if pid is not None and storage.reject_proposal(pid):
                print(f"Proposal #{num} marked REJECTED.")
            else:
                print(f"Could not reject proposal #{num} (must exist and be PENDING).")
        else:
            print("Unknown command. Use: list [STATUS] | approve <id> | reject <id> | quit")


def main() -> int:
    parser = argparse.ArgumentParser(description="Human-in-the-Loop Trading 212 Order Executor")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List trade proposals")
    p_list.add_argument("--status", default=None, help="Filter by status (e.g. PENDING, EXECUTED)")

    p_approve = sub.add_parser("approve", help="Approve and execute a PENDING proposal")
    p_approve.add_argument("proposal_id", type=int, help="Proposal ID to approve")
    p_approve.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt",
    )

    p_reject = sub.add_parser("reject", help="Reject a PENDING proposal")
    p_reject.add_argument("proposal_id", type=int, help="Proposal ID to reject")

    sub.add_parser("interactive", help="Launch interactive approval shell")

    args = parser.parse_args()
    settings = load_settings()
    configure_logging(settings.log_level)
    storage = Storage(settings.db_path)

    if args.command == "list":
        _print_proposals(storage.list_proposals(status=args.status))
        return 0

    if args.command == "reject":
        pid = storage.resolve_proposal_id(args.proposal_id)
        if pid is not None and storage.reject_proposal(pid):
            print(f"Proposal #{args.proposal_id} marked REJECTED.")
            return 0
        print(f"Could not reject proposal #{args.proposal_id} (not found or not PENDING).")
        return 1

    client = Trading212Client(
        api_key=settings.t212_api_key,
        api_secret=settings.t212_api_secret,
        base_url=settings.t212_base_url,
    )

    if args.command == "approve":
        pid = storage.resolve_proposal_id(args.proposal_id)
        if pid is None:
            print(f"Proposal #{args.proposal_id} not found.")
            return 1
        ok = approve_and_execute(
            pid,
            storage,
            client,
            settings.t212_env,
            skip_confirm=args.yes,
        )
        return 0 if ok else 1

    if args.command == "interactive":
        return interactive_loop(storage, client, settings.t212_env)

    return 1


if __name__ == "__main__":
    sys.exit(main())
