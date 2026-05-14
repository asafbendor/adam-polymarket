"""
Adam - Orchestrator.

Coordinates Scout Agent and Trader Agent.
Reports every action to the user via Telegram.
Both agents have memory and self-improve over time.
"""
import asyncio
import json
import logging
import os

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("adam")

import memory
import state
import telegram
from scout_agent import find_opportunities
from trader_agent import TraderAgent

DAILY_BUDGET  = float(os.getenv("DAILY_BUDGET", "10.0"))
BET_SIZE      = float(os.getenv("BET_SIZE", "1.0"))
SCAN_EVERY    = int(os.getenv("SCAN_EVERY_MINUTES", "20")) * 60
DB            = os.getenv("DB_PATH", "adam.db")
GAMMA_BASE    = "https://gamma-api.polymarket.com"


async def _check_resolution(session: aiohttp.ClientSession, cid: str) -> tuple[bool, str]:
    try:
        async with session.get(f"{GAMMA_BASE}/markets",
                               params={"conditionId": cid},
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status != 200: return False, ""
            data = await r.json(content_type=None)
        markets = data if isinstance(data,list) else [data]
        for m in markets:
            if not m.get("resolved") and not m.get("closed"): return False, ""
            op = m.get("outcomePrices") or []
            if isinstance(op,str):
                try: op=json.loads(op)
                except: op=[]
            on = m.get("outcomes") or []
            if isinstance(on,str):
                try: on=json.loads(on)
                except: on=[]
            for i,price in enumerate(op):
                if float(price) >= 0.99 and i < len(on):
                    return True, str(on[i]).upper()
            winner = (m.get("resolution") or m.get("winner") or "").upper()
            if winner: return True, winner
    except Exception as e:
        logger.debug(f"Resolution check {cid}: {e}")
    return False, ""


async def run_cycle(session: aiohttp.ClientSession, trader: TraderAgent):
    today        = state.today()
    stats        = state.get_daily(today, DB)
    budget_left  = DAILY_BUDGET - stats.get("spent", 0)

    logger.info(f"Cycle start | budget_left=${budget_left:.2f}")
    memory.agent_log("adam", f"Cycle start | budget=${budget_left:.2f}")

    # 1. Check resolved bets
    for bet in state.get_pending(DB):
        resolved, winner = await _check_resolution(session, bet["condition_id"])
        if not resolved:
            if bet["status"] == "pending" and bet.get("order_id"):
                try:
                    loop = asyncio.get_event_loop()
                    resp = await loop.run_in_executor(
                        None, lambda: trader._client.get_order(bet["order_id"])
                        if trader._client else None
                    )
                    if resp and resp.get("status","").lower() in ("matched","filled"):
                        state.update_bet(bet["id"], {
                            "status":"filled",
                            "fill_price": float(resp.get("avg_price", bet["limit_price"]) or bet["limit_price"])
                        }, DB)
                        await telegram.send(f"<b>Order filled</b>\n{bet['question'][:100]}")
                except Exception: pass
            continue

        fill  = bet.get("fill_price") or bet["limit_price"]
        won   = winner == bet["direction"]
        if won:
            pnl = round((BET_SIZE/fill)*(1.0-fill), 4) if fill > 0 else 0
        else:
            pnl = -BET_SIZE

        state.update_bet(bet["id"], {"status":"won" if won else "lost","pnl":pnl}, DB)
        day = (bet.get("created_at") or "")[:10] or today
        state.add_daily(day,"realized_pnl",pnl,DB)
        state.add_daily(day,"bets_won" if won else "bets_lost",1,DB)

        # Record outcome for Scout to learn from
        memory.record_outcome(
            bet["question"], bet["direction"],
            bet.get("estimated_prob",0.5), bet.get("market_price",0.5),
            "won" if won else "lost", pnl,
            bet.get("reason","")
        )

        sign  = "+" if pnl >= 0 else ""
        emoji = "WIN" if won else "LOSS"
        await telegram.send(
            f"<b>{emoji}</b>\n{bet['question'][:120]}\nP&L: {sign}${pnl:.2f}"
        )

    # 2. Scout finds opportunities
    if budget_left < BET_SIZE:
        logger.info("Budget exhausted")
        return

    opps = await find_opportunities(session)
    memory.agent_log("adam", f"Scout returned {len(opps)} opportunities")

    if not opps:
        logger.info("Scout found no opportunities this cycle")
        return

    # 3. Trader places bets
    placed = 0
    for opp in opps:
        if budget_left < BET_SIZE:
            break
        cid = opp.get("condition_id","")
        if not cid or state.already_bet(cid, DB):
            continue

        logger.info(f"Trader placing: {opp['direction']} on {opp['question'][:60]}")

        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: trader.place_bet(
                token_id     = opp.get("token_id",""),
                market_price = opp.get("market_price", 0.5),
                bet_size     = BET_SIZE,
                neg_risk     = opp.get("neg_risk", False),
            )
        )

        if result.get("ok"):
            bet_id = state.insert_bet({
                "condition_id":   cid,
                "question":       opp.get("question",""),
                "direction":      opp.get("direction","YES"),
                "market_price":   opp.get("market_price",0.5),
                "estimated_prob": opp.get("estimated_prob",0.5),
                "reason":         opp.get("reason",""),
                "bet_size":       BET_SIZE,
                "limit_price":    result.get("limit_price", opp.get("market_price",0.5)),
                "order_id":       result.get("order_id",""),
                "token_id":       opp.get("token_id",""),
                "status":         "pending",
                "end_date":       opp.get("end_date",""),
                "market_url":     opp.get("market_url",""),
            }, DB)
            state.add_daily(today,"spent",BET_SIZE,DB)
            state.add_daily(today,"bets_placed",1,DB)
            budget_left -= BET_SIZE
            placed += 1

            prob  = opp.get("estimated_prob",0)*100
            edge  = (opp.get("estimated_prob",0)-opp.get("market_price",0.5))*100
            msg   = (f"<b>Bet placed</b>\n"
                     f"{opp['question'][:120]}\n"
                     f"Direction: {opp['direction']} | Market: {opp.get('market_price',0.5):.2f} | "
                     f"Est: {prob:.0f}% | Edge: +{edge:.0f}%\n"
                     f"Reason: {opp.get('reason','')[:120]}")
            if opp.get("market_url"):
                msg += f"\n<a href='{opp['market_url']}'>View</a>"
            await telegram.send(msg)
        else:
            err = result.get("message","")
            logger.warning(f"Bet failed: {err} | {opp.get('question','')[:60]}")
            state.log("WARNING", f"Bet failed: {err}", DB)
            # Don't notify user on every failure - Trader handles retries internally


async def main():
    memory.init()
    state.init(DB)
    trader = TraderAgent()

    try:
        loop = asyncio.get_event_loop()
        bal_data = await loop.run_in_executor(None, trader._handle_tool, "get_balance", {})
        bal = json.loads(bal_data).get("usdc")
        bal_str = f"${bal:.2f}" if bal else "unknown"
    except Exception:
        bal_str = "unknown"

    await telegram.send(
        f"<b>Adam started</b>\n"
        f"Budget: ${DAILY_BUDGET}/day | Bet: ${BET_SIZE}\n"
        f"USDC: {bal_str}\n"
        f"Agents: Scout (self-improving) + Trader (self-healing)\n"
        f"Scanning every {SCAN_EVERY//60} min"
    )

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            await run_cycle(session, trader)
        except Exception as e:
            logger.error(f"First cycle: {e}")
            await telegram.send(f"<b>First cycle error:</b> {e}")

        while True:
            await asyncio.sleep(SCAN_EVERY)
            try:
                await run_cycle(session, trader)
            except Exception as e:
                logger.error(f"Cycle error: {e}")
                await telegram.send(f"<b>Adam error:</b> {e}")


if __name__ == "__main__":
    asyncio.run(main())
