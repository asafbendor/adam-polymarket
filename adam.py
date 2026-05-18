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
from datetime import datetime, timezone

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
from trader_agent import TraderAgent
from sports_scout import find_sports_opportunities

DAILY_BUDGET  = float(os.getenv("DAILY_BUDGET", "10.0"))
BET_SIZE      = float(os.getenv("BET_SIZE", "1.0"))
SCAN_EVERY    = int(os.getenv("SCAN_EVERY_MINUTES", "20")) * 60
DB            = os.getenv("DB_PATH", "adam.db")
GAMMA_BASE    = "https://gamma-api.polymarket.com"


def _outcome_to_direction(outcome_name: str, outcomes: list) -> str:
    """
    Convert a raw outcome name to YES or NO.
    Polymarket binary markets have outcomes named "Yes"/"No" OR descriptive names.
    For descriptive names, YES = index 0 (the affirmative outcome).
    """
    n = outcome_name.strip().upper()
    if n in ("YES", "Y", "TRUE"):
        return "YES"
    if n in ("NO", "N", "FALSE"):
        return "NO"
    # Descriptive outcome: check position in outcomes list
    for i, o in enumerate(outcomes):
        if str(o).strip().upper() == n:
            return "YES" if i == 0 else "NO"
    return n  # fallback - return raw


async def _check_resolution(session: aiohttp.ClientSession, cid: str) -> tuple[bool, str]:
    """Returns (resolved, direction_that_won) where direction is 'YES' or 'NO'."""
    for params in [{"conditionId": cid}, {"condition_id": cid}]:
        try:
            async with session.get(f"{GAMMA_BASE}/markets", params=params,
                                   timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status != 200: continue
                data = await r.json(content_type=None)
            markets = data if isinstance(data, list) else [data]
            for m in markets:
                if not m: continue
                is_resolved = m.get("resolved") or m.get("closed") or False
                if not is_resolved: continue

                op = m.get("outcomePrices") or []
                on = m.get("outcomes") or []
                if isinstance(op, str):
                    try: op = json.loads(op)
                    except: op = []
                if isinstance(on, str):
                    try: on = json.loads(on)
                    except: on = []

                # Method 1: find outcome with price = 1.0
                for i, price in enumerate(op):
                    try:
                        if float(price) >= 0.99 and i < len(on):
                            return True, _outcome_to_direction(str(on[i]), on)
                    except (ValueError, TypeError):
                        continue

                # Method 2: explicit resolution field
                raw_winner = (m.get("resolution") or m.get("winner") or
                              m.get("resolvedOutcome") or "")
                if raw_winner:
                    return True, _outcome_to_direction(str(raw_winner), on)

        except Exception as e:
            logger.debug(f"Resolution check {cid}: {e}")
    return False, ""


def _classify_question(question: str) -> str:
    """Classify a market question into a category for learning."""
    q = question.lower()
    if any(w in q for w in ["btc","bitcoin","eth","ethereum","sol","crypto","price"]):
        return "crypto"
    if any(w in q for w in ["iran","peace","war","ceasefire","nato","sanction","election","president","senate","congress","vote"]):
        return "geopolitical"
    if any(w in q for w in ["nba","nfl","mlb","nhl","soccer","football","tennis","golf","cricket","pga","ipl","win the","beat","championship"]):
        return "sports"
    if any(w in q for w in ["eurovision","oscar","grammy","award","box office","album","celebrity"]):
        return "entertainment"
    return "other"


def _update_rules_from_outcome(bet: dict, won: bool, pnl: float):
    """
    Updates Scout's behavioral rules based on resolved bet outcome.
    This is real learning - rules change behavior, not just store history.
    """
    question = bet.get("question", "")
    category = _classify_question(question)
    direction = bet.get("direction", "")
    market_price = bet.get("market_price", 0.5)

    # Load existing category stats
    stats_key = f"category_stats_{category}"
    existing = memory.recall("scout", stats_key) or "wins=0,losses=0,total_pnl=0.0"
    try:
        parts = dict(item.split("=") for item in existing.split(","))
        wins   = int(parts.get("wins", 0))
        losses = int(parts.get("losses", 0))
        total_pnl = float(parts.get("total_pnl", 0))
    except Exception:
        wins = losses = 0; total_pnl = 0.0

    if won: wins += 1
    else:   losses += 1
    total_pnl = round(total_pnl + pnl, 2)
    total = wins + losses
    win_rate = wins / total if total else 0

    memory.remember("scout", stats_key,
        f"wins={wins},losses={losses},total_pnl={total_pnl}")

    # Update behavioral rule based on accumulated stats
    rule_key = f"rule_{category}"
    if total >= 3:  # enough data to form a rule
        if win_rate >= 0.70:
            rule = f"PRIORITIZE {category.upper()} bets - win rate {win_rate:.0%} over {total} bets, P&L {total_pnl:+.2f}"
        elif win_rate <= 0.35:
            rule = f"AVOID {category.upper()} bets - win rate only {win_rate:.0%} over {total} bets, P&L {total_pnl:+.2f}. Skip unless very strong signal."
        else:
            rule = f"NEUTRAL on {category.upper()} - win rate {win_rate:.0%} over {total} bets, P&L {total_pnl:+.2f}"
        memory.remember("scout", rule_key, rule)
        memory.agent_log("scout",
            f"Rule updated [{category}]: {win_rate:.0%} WR ({wins}W/{losses}L) P&L={total_pnl:+.2f}")

    # Record individual outcome
    memory.agent_log("scout",
        f"{'WIN' if won else 'LOSS'} {direction} on [{category}] {question[:60]} P&L={pnl:+.2f}")


async def _binance_fallback(session: aiohttp.ClientSession) -> list[dict]:
    """Direct Binance price comparison - no Claude needed. Pure math."""
    from scout_agent import _fetch_markets, _TICKERS
    import re as _re

    markets = await _fetch_markets(session)
    opps = []
    BINANCE = "https://api.binance.com/api/v3/ticker/price"

    for m in markets:
        q = m["question"]
        # Find ticker
        sym = None
        for alias, s in _TICKERS.items():
            if alias in q.lower():
                sym = s; break
        if not sym:
            continue
        # Find target price
        pm = _re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*([kK]?)", q)
        if not pm:
            continue
        target = float(pm.group(1).replace(",",""))
        if pm.group(2).lower() == "k":
            target *= 1000

        # Get Binance price
        try:
            async with session.get(BINANCE, params={"symbol":sym},
                                   timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200: continue
                current = float((await r.json(content_type=None))["price"])
        except Exception:
            continue

        q_low = q.lower()

        # "dip/fall/crash/drop to X" = asks if price goes DOWN to X
        # "above/exceed/over/stay above X" = asks if price stays ABOVE X
        # "hit/reach X" = asks if price goes UP to X (when X > current)
        going_down_words = ["dip", "fall", "drop", "crash", "below", "under",
                            "less than", "dip to", "fall to", "drop to"]
        going_up_words   = ["above", "exceed", "over", "higher", "surpass",
                            "stay above", "close above", "end above", "hit",
                            "reach", "pass", "break"]

        is_going_down = any(w in q_low for w in going_down_words)
        is_going_up   = any(w in q_low for w in going_up_words)

        # For "going down" questions: YES = price drops to target
        #   current >> target → needs big DROP → P(YES) is LOW → BET NO
        #   current << target → price already below target → P(YES) HIGH → BET YES
        # For "going up" questions: YES = price rises to target
        #   current >> target → already above target → P(YES) HIGH → BET YES
        #   current << target → needs big RISE → P(YES) LOW → BET NO

        if is_going_down and not is_going_up:
            gap = (current - target) / current  # positive = current is above target
            if gap >= 0.30:    bet_dir, prob, mp, tid = "NO",  0.93, m["no_price"],  m["no_token_id"]
            elif gap >= 0.20:  bet_dir, prob, mp, tid = "NO",  0.87, m["no_price"],  m["no_token_id"]
            elif gap >= 0.12:  bet_dir, prob, mp, tid = "NO",  0.78, m["no_price"],  m["no_token_id"]
            elif gap <= -0.20: bet_dir, prob, mp, tid = "YES", 0.87, m["yes_price"], m["yes_token_id"]
            else: continue
        elif is_going_up or (not is_going_down):
            gap = (current - target) / target   # positive = current already above target
            if gap >= 0.20:    bet_dir, prob, mp, tid = "YES", 0.93, m["yes_price"], m["yes_token_id"]
            elif gap >= 0.12:  bet_dir, prob, mp, tid = "YES", 0.87, m["yes_price"], m["yes_token_id"]
            elif gap >= 0.06:  bet_dir, prob, mp, tid = "YES", 0.78, m["yes_price"], m["yes_token_id"]
            elif gap <= -0.30: bet_dir, prob, mp, tid = "NO",  0.93, m["no_price"],  m["no_token_id"]
            elif gap <= -0.20: bet_dir, prob, mp, tid = "NO",  0.87, m["no_price"],  m["no_token_id"]
            elif gap <= -0.12: bet_dir, prob, mp, tid = "NO",  0.78, m["no_price"],  m["no_token_id"]
            else: continue
        else:
            continue

        token_id = tid
        edge = prob - mp
        if edge < 0.05: continue

        opps.append({
            "condition_id":   m["condition_id"],
            "question":       q,
            "direction":      bet_dir,
            "token_id":       token_id,
            "market_price":   mp,
            "estimated_prob": prob,
            "reason":         f"{sym.replace('USDT','')} at ${current:,.0f}, target ${target:,.0f}, gap {gap*100:.0f}%",
            "neg_risk":       m["neg_risk"],
            "market_url":     m["market_url"],
            "end_date":       m["end_date"],
        })

    logger.info(f"Binance fallback found {len(opps)} opportunities")
    return opps


async def run_cycle(session: aiohttp.ClientSession, trader: TraderAgent):
    today        = state.today()
    stats        = state.get_daily(today, DB)
    budget_left  = DAILY_BUDGET - stats.get("spent", 0)

    logger.info(f"Cycle start | budget_left=${budget_left:.2f}")
    memory.agent_log("adam", f"Cycle start | budget=${budget_left:.2f}")
    state.log("INFO", f"Cycle start | budget_left=${budget_left:.2f}", DB)

    # 1. Check resolved bets
    now_iso = datetime.now(timezone.utc).isoformat()
    for bet in state.get_pending(DB):
        # Force-check if end_date has passed (market must be resolved by now)
        end = bet.get("end_date","")
        past_end = bool(end and end < now_iso[:10])
        resolved, winner = await _check_resolution(session, bet["condition_id"])
        if not resolved and past_end:
            # Try again with a small delay - market might be delayed in resolving
            await asyncio.sleep(1)
            resolved, winner = await _check_resolution(session, bet["condition_id"])
            if not resolved:
                state.log("WARNING", f"Bet past end_date but unresolved: {bet['question'][:60]}", DB)
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

        # Systematic learning - update behavioral rules based on outcomes
        _update_rules_from_outcome(bet, won, pnl)

        sign  = "+" if pnl >= 0 else ""
        emoji = "WIN" if won else "LOSS"
        await telegram.send(
            f"<b>{emoji}</b>\n{bet['question'][:120]}\nP&L: {sign}${pnl:.2f}"
        )

    # 2. Scout finds opportunities
    if budget_left < BET_SIZE:
        logger.info("Budget exhausted")
        return

    # Inject past outcomes into Scout context before every run
    recent_outcomes = memory.get_outcomes(10)
    win_count  = sum(1 for o in recent_outcomes if o.get("outcome") == "won")
    loss_count = sum(1 for o in recent_outcomes if o.get("outcome") == "lost")
    if recent_outcomes:
        memory.remember("scout", "recent_performance",
            f"Last {len(recent_outcomes)} resolved bets: {win_count}W / {loss_count}L. "
            f"Recent: " + " | ".join(
                f"{o['outcome'].upper()} {o['direction']} on {o['question'][:40]}"
                for o in recent_outcomes[:3]
            )
        )

    # Scout: Binance (crypto) + ESPN (sports) - no API calls, no cost
    from scout_agent import _fetch_markets
    all_markets = await _fetch_markets(session)

    crypto_opps = await _binance_fallback(session)
    sports_opps = await find_sports_opportunities(all_markets, session)
    opps = crypto_opps + sports_opps

    memory.agent_log("adam", f"Scout found {len(opps)} ({len(crypto_opps)} crypto, {len(sports_opps)} sports)")
    state.log("INFO", f"Scout: {len(crypto_opps)} crypto + {len(sports_opps)} sports = {len(opps)} total", DB)
    if not opps:
        memory.remember("scout", "last_zero_cycle", f"{state.today()} - 0 crypto, 0 sports")

    state.log("INFO", f"Total opportunities: {len(opps)}", DB)
    if not opps:
        logger.info("No opportunities this cycle")
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
                condition_id = opp.get("condition_id",""),
                direction    = opp.get("direction","YES"),
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
            # Store failure in Trader memory so it learns
            import hashlib as _h
            key = "fail_" + _h.md5(err[:50].encode()).hexdigest()[:8]
            memory.remember("trader", key, f"Error: {err[:200]} | market: {opp.get('question','')[:60]}")
            memory.agent_log("trader", f"Bet failed: {err[:150]}")


async def main():
    memory.init()
    state.init(DB)

    # Auto-seed on first run (no learnings yet in DB)
    if not memory.recall("scout", "btc_price_may_2026"):
        try:
            import seed_memory
            seed_memory.seed_known()
            state.log("INFO", "Memory seeded from week-1 learnings", DB)
            logger.info("Memory seeded with week-1 learnings")
        except Exception as e:
            logger.warning(f"Seed failed (non-critical): {e}")
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
        consecutive_failures = 0

        while True:
            try:
                await run_cycle(session, trader)
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                logger.error(f"Cycle error #{consecutive_failures}: {e}")
                state.log("ERROR", f"Cycle error: {e}", DB)
                # Silent - no Telegram on errors, only on bets and resolutions

            # Retry quickly after failures, normal interval when healthy
            if consecutive_failures > 0:
                retry_in = min(60 * consecutive_failures, 300)  # 1min, 2min, 3min... max 5min
                logger.info(f"Retrying in {retry_in}s (failure #{consecutive_failures})")
                await asyncio.sleep(retry_in)
            else:
                await asyncio.sleep(SCAN_EVERY)


if __name__ == "__main__":
    asyncio.run(main())
