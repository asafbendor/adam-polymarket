"""
Adam - Orchestrator.

Coordinates Scout (finds opportunities) and Trader (executes bets).
Reports every action to the user via Telegram.
Runs every 20 minutes.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv

import state
import telegram
from scout import find_opportunities
from trader import Trader

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("adam")

DAILY_BUDGET = float(os.getenv("DAILY_BUDGET", "10.0"))
BET_SIZE     = float(os.getenv("BET_SIZE", "1.0"))
SCAN_EVERY   = int(os.getenv("SCAN_EVERY_MINUTES", "20")) * 60
DB           = os.getenv("DB_PATH", "adam.db")
GAMMA_BASE   = "https://gamma-api.polymarket.com"


# ── Resolution check ─────────────────────────────────────────────────

async def _check_resolution(session: aiohttp.ClientSession, cid: str) -> tuple[bool, str]:
    """Returns (resolved, winning_outcome) where outcome is 'YES' or 'NO'."""
    try:
        async with session.get(f"{GAMMA_BASE}/markets",
                               params={"conditionId": cid},
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status != 200:
                return False, ""
            data = await r.json(content_type=None)

        markets = data if isinstance(data, list) else [data]
        for m in markets:
            if not m.get("resolved") and not m.get("closed"):
                return False, ""
            # Find winner from outcome prices
            op = m.get("outcomePrices") or []
            if isinstance(op, str):
                try: op = json.loads(op)
                except: op = []
            on = m.get("outcomes") or []
            if isinstance(on, str):
                try: on = json.loads(on)
                except: on = []
            for i, price in enumerate(op):
                if float(price) >= 0.99 and i < len(on):
                    return True, str(on[i]).upper()
            winner = (m.get("resolution") or m.get("winner") or "").upper()
            if winner:
                return True, winner
    except Exception as e:
        logger.debug(f"Resolution check {cid}: {e}")
    return False, ""


# ── One cycle ────────────────────────────────────────────────────────

async def run_cycle(session: aiohttp.ClientSession, trader: Trader):
    today = state.today()
    stats = state.get_daily(today, DB)
    budget_left = DAILY_BUDGET - stats.get("spent", 0)

    state.log("INFO", f"Cycle start | budget_left=${budget_left:.2f}", DB)
    logger.info(f"Cycle start | budget_left=${budget_left:.2f}")

    # ── 1. Check pending bets for resolution ─────────────────────────
    pending = state.get_pending(DB)
    for bet in pending:
        resolved, winner = await _check_resolution(session, bet["condition_id"])
        if not resolved:
            # Check if order is filled
            if bet["status"] == "pending" and bet["order_id"]:
                resp = trader.get_order(bet["order_id"])
                if resp.get("status", "").lower() in ("matched", "filled"):
                    state.update_bet(bet["id"], {"status": "filled",
                        "fill_price": float(resp.get("avg_price", bet["limit_price"]) or bet["limit_price"])}, DB)
                    await telegram.send(
                        f"<b>Order filled</b>\n{bet['question'][:100]}"
                    )
            continue

        # Market resolved
        fill = bet.get("fill_price") or bet["limit_price"]
        if not fill: fill = bet["limit_price"]
        bet_won = winner == bet["direction"]

        if bet_won:
            shares = BET_SIZE / fill if fill > 0 else 1
            pnl    = round(shares * (1.0 - fill), 4)
            status = "won"
        else:
            pnl    = -BET_SIZE
            status = "lost"

        state.update_bet(bet["id"], {"status": status, "pnl": pnl}, DB)
        day = bet.get("created_at","")[:10] or today
        state.add_daily(day, "realized_pnl", pnl, DB)
        state.add_daily(day, "bets_won" if bet_won else "bets_lost", 1, DB)

        sign   = "+" if pnl >= 0 else ""
        emoji  = "WIN" if bet_won else "LOSS"
        msg    = (f"<b>{emoji}</b>\n"
                  f"{bet['question'][:120]}\n"
                  f"P&L: {sign}${pnl:.2f}")
        await telegram.send(msg)
        state.log("INFO", f"{emoji}: {bet['question'][:60]} pnl={sign}{pnl:.2f}", DB)

    # ── 2. Scout finds new opportunities ─────────────────────────────
    if budget_left < BET_SIZE:
        logger.info("Budget exhausted, skipping scout")
        state.log("INFO", "Budget exhausted, skipping scout", DB)
        return

    opportunities = await find_opportunities(session)
    state.log("INFO", f"Scout found {len(opportunities)} opportunities", DB)
    logger.info(f"Scout found {len(opportunities)} opportunities")

    placed = 0
    for opp in opportunities:
        if budget_left < BET_SIZE:
            break
        cid = opp.get("condition_id","")
        if not cid or state.already_bet(cid, DB):
            continue

        # ── 3. Trader places bet ──────────────────────────────────────
        result = trader.place_bet(
            token_id    = opp.get("token_id",""),
            market_price= opp.get("market_price", 0.5),
            bet_size    = BET_SIZE,
            neg_risk    = opp.get("neg_risk", False),
        )

        direction = opp.get("direction","YES")
        limit_price = result.get("limit_price", opp.get("market_price",0.5))

        if result["ok"]:
            bet_id = state.insert_bet({
                "condition_id":  cid,
                "question":      opp.get("question",""),
                "direction":     direction,
                "market_price":  opp.get("market_price",0.5),
                "estimated_prob":opp.get("estimated_prob",0.5),
                "reason":        opp.get("reason",""),
                "bet_size":      BET_SIZE,
                "limit_price":   limit_price,
                "order_id":      result.get("order_id",""),
                "token_id":      opp.get("token_id",""),
                "status":        "pending",
                "end_date":      opp.get("end_date",""),
                "market_url":    opp.get("market_url",""),
            }, DB)

            state.add_daily(today, "spent", BET_SIZE, DB)
            state.add_daily(today, "bets_placed", 1, DB)
            budget_left -= BET_SIZE
            placed += 1

            prob_pct  = opp.get("estimated_prob",0) * 100
            edge_pct  = (opp.get("estimated_prob",0) - opp.get("market_price",0.5)) * 100
            msg = (f"<b>Bet placed</b>\n"
                   f"{opp['question'][:120]}\n"
                   f"Direction: {direction} | Price: {opp.get('market_price',0.5):.2f} | "
                   f"Est: {prob_pct:.0f}% | Edge: +{edge_pct:.0f}%\n"
                   f"Reason: {opp.get('reason','')[:120]}")
            if opp.get("market_url"):
                msg += f"\n<a href='{opp['market_url']}'>View market</a>"
            await telegram.send(msg)
            state.log("INFO", f"Bet: {direction} '{opp['question'][:60]}' "
                               f"est={prob_pct:.0f}% edge=+{edge_pct:.0f}%", DB)
        else:
            logger.warning(f"Bet failed: {result['message']} | {opp.get('question','')[:60]}")
            state.log("WARNING", f"Bet failed: {result['message']}", DB)

    if placed == 0 and len(opportunities) > 0:
        logger.info("Scout found opportunities but none were placed")
    elif placed == 0:
        logger.info("No opportunities this cycle")


# ── Main loop ────────────────────────────────────────────────────────

async def main():
    state.init(DB)
    trader = Trader()

    bal = trader.get_balance()
    bal_str = f"${bal:.2f}" if bal is not None else "unknown"

    await telegram.send(
        f"<b>Adam started</b>\n"
        f"Budget: ${DAILY_BUDGET}/day | Bet size: ${BET_SIZE}\n"
        f"USDC balance: {bal_str}\n"
        f"Scanning every {SCAN_EVERY//60} minutes"
    )
    state.log("INFO", f"Adam started | balance={bal_str}", DB)

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Run immediately on startup
        try:
            await run_cycle(session, trader)
        except Exception as e:
            logger.error(f"First cycle error: {e}")
            await telegram.send(f"<b>Error in first cycle:</b> {e}")

        # Then every SCAN_EVERY seconds
        while True:
            await asyncio.sleep(SCAN_EVERY)
            try:
                await run_cycle(session, trader)
            except Exception as e:
                logger.error(f"Cycle error: {e}")
                await telegram.send(f"<b>Adam error:</b> {e}")


if __name__ == "__main__":
    asyncio.run(main())
