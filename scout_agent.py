"""
Scout Agent - autonomous research agent with memory and self-improvement.

Runs a full Claude tool-use loop. Has access to:
- fetch_markets: get Polymarket markets
- get_price: check Binance prices
- remember_learning: store what worked/didn't work
- recall_learnings: retrieve past learnings to improve decisions
- record_reflection: after seeing bet outcomes, update strategy

Scout improves over time by analyzing past wins/losses and adjusting its criteria.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import anthropic

import memory

logger = logging.getLogger("scout")

GAMMA_URL   = "https://gamma-api.polymarket.com/markets"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"

_TICKERS = {
    "btc": "BTCUSDT", "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT", "ethereum": "ETHUSDT", "ether": "ETHUSDT",
    "sol": "SOLUSDT", "solana": "SOLUSDT",
    "xrp": "XRPUSDT", "bnb": "BNBUSDT",
    "doge": "DOGEUSDT", "ada": "ADAUSDT",
    "avax": "AVAXUSDT", "link": "LINKUSDT",
    "matic": "MATICUSDT", "near": "NEARUSDT",
    "atom": "ATOMUSDT", "dot": "DOTUSDT",
}

TOOLS = [
    {
        "name": "fetch_markets",
        "description": "Fetch active Polymarket markets resolving in 1-7 days. Always call this first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_price",
        "description": "Get current Binance price for a crypto asset (BTC, ETH, SOL, etc.)",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "e.g. BTC, ETH, SOL"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "recall_learnings",
        "description": "Recall past learnings to improve decision-making. Call this before deciding.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "remember_learning",
        "description": "Store an important learning for future use. Call after finding opportunities.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key":   {"type": "string", "description": "Short category, e.g. 'btc_margin_threshold'"},
                "value": {"type": "string", "description": "The learning or rule to remember"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "get_past_outcomes",
        "description": "See results of past bets to calibrate confidence. Returns win/loss history.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

SYSTEM = """You are Scout - an autonomous prediction market research agent for Polymarket.

Your mission: find profitable betting opportunities (markets resolving within 7 days).

Your process:
1. Call recall_learnings to remember what worked before
2. Call get_past_outcomes to see your track record
3. Call fetch_markets to see available markets
4. For crypto price markets, call get_price to compare vs target
5. Call remember_learning to record any useful patterns you discover
6. Return your opportunities

Rules for betting:
- Crypto price markets: use get_price to compare. If BTC=$104k and market asks "above $80k" at 0.88, that's a CLEAR YES bet
- Only recommend when confidence > 60% AND edge > 5% vs market price
- Learn from past outcomes: if a strategy lost, adjust it
- Be specific in reasons: include actual prices and percentages

Output: JSON array of opportunities:
[{"condition_id":"...","question":"...","direction":"YES/NO",
  "token_id":"...","market_price":0.XX,"estimated_prob":0.XX,"reason":"..."}]

Return [] only if truly no opportunities exist after thorough analysis."""


async def _handle_tool(name: str, inp: dict,
                        session: aiohttp.ClientSession,
                        cache: list) -> str:
    if name == "fetch_markets":
        if not cache:
            cache.extend(await _fetch_markets(session))
        items = []
        for m in cache[:30]:
            items.append({
                "condition_id": m["condition_id"],
                "question":     m["question"],
                "end_date":     m["end_date"],
                "hours_left":   m["hours_left"],
                "yes_price":    m["yes_price"],
                "yes_token_id": m["yes_token_id"],
                "no_token_id":  m["no_token_id"],
                "neg_risk":     m["neg_risk"],
                "market_url":   m["market_url"],
                "liquidity":    m["liquidity"],
            })
        return json.dumps(items)

    if name == "get_price":
        sym  = inp.get("symbol","BTC").strip().upper()
        pair = _TICKERS.get(sym.lower(), sym + "USDT")
        try:
            async with session.get(BINANCE_URL, params={"symbol": pair},
                                    timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    price = float(d.get("price", 0))
                    return json.dumps({"symbol": sym, "pair": pair, "price": price})
        except Exception as e:
            return json.dumps({"error": str(e)})

    if name == "recall_learnings":
        learnings = memory.recall_all("scout")
        return json.dumps(learnings if learnings else {"note": "No learnings yet - this is your first run"})

    if name == "remember_learning":
        memory.remember("scout", inp.get("key",""), inp.get("value",""))
        return json.dumps({"saved": True})

    if name == "get_past_outcomes":
        outcomes = memory.get_outcomes(20)
        if not outcomes:
            return json.dumps({"note": "No past outcomes yet"})
        summary = []
        for o in outcomes:
            summary.append({
                "question":       o["question"][:80],
                "direction":      o["direction"],
                "estimated_prob": o["estimated_prob"],
                "market_price":   o["market_price"],
                "outcome":        o["outcome"],
                "pnl":            o["pnl"],
            })
        wins  = sum(1 for o in outcomes if o["outcome"] == "won")
        total = len(outcomes)
        return json.dumps({
            "win_rate": f"{wins}/{total}",
            "total_pnl": sum(o["pnl"] for o in outcomes if o["pnl"]),
            "recent":   summary,
        })

    return json.dumps({"error": f"unknown tool: {name}"})


async def find_opportunities(session: aiohttp.ClientSession) -> list[dict]:
    api_key = os.getenv("ANTHROPIC_API_KEY","").strip().lstrip("=")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return []

    client   = anthropic.Anthropic(api_key=api_key)
    cache: list = []
    messages = [{"role": "user", "content":
        "Find profitable Polymarket betting opportunities for the next 7 days. "
        "Start by recalling your learnings and past outcomes, then fetch markets and analyze."}]

    for round_num in range(10):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            for block in resp.content:
                if hasattr(block, "text"):
                    m = re.search(r"\[.*\]", block.text, re.DOTALL)
                    if m:
                        try:
                            opps = json.loads(m.group(0))
                            # Enrich from cache
                            cmap = {m["condition_id"]: m for m in cache}
                            result = []
                            for opp in opps:
                                cached = cmap.get(opp.get("condition_id",""), {})
                                dir_ = opp.get("direction","YES").upper()
                                result.append({
                                    "condition_id":   opp.get("condition_id",""),
                                    "question":       opp.get("question",""),
                                    "direction":      dir_,
                                    "token_id":       opp.get("token_id","") or
                                                      (cached.get("yes_token_id","") if dir_=="YES"
                                                       else cached.get("no_token_id","")),
                                    "market_price":   float(opp.get("market_price",0.5)),
                                    "estimated_prob": float(opp.get("estimated_prob",0.5)),
                                    "reason":         opp.get("reason",""),
                                    "neg_risk":       cached.get("neg_risk",False),
                                    "market_url":     cached.get("market_url",""),
                                    "end_date":       cached.get("end_date",""),
                                })
                            logger.info(f"Scout found {len(result)} opportunities (round {round_num+1})")
                            memory.agent_log("scout", f"Found {len(result)} opportunities")
                            return result
                        except json.JSONDecodeError:
                            pass
            return []

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = await _handle_tool(block.name, block.input, session, cache)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})

    return []


async def _fetch_markets(session: aiohttp.ClientSession) -> list[dict]:
    markets = []
    now = datetime.now(timezone.utc)
    for offset in range(0, 400, 100):
        try:
            async with session.get(GAMMA_URL, params={
                "active":"true","closed":"false","offset":offset,"limit":100,
                "order":"volume24hr","ascending":"false",
            }, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200: break
                data = await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"Gamma: {e}"); break

        batch = data if isinstance(data,list) else data.get("markets",[])
        if not batch: break

        for raw in batch:
            ed = raw.get("endDate") or raw.get("endDateIso") or ""
            if not ed: continue
            try:
                for fmt in ("%Y-%m-%dT%H:%M:%SZ","%Y-%m-%dT%H:%M:%S.%fZ","%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(ed[:26],fmt[:len(fmt)]).replace(tzinfo=timezone.utc)
                        break
                    except: continue
                else: continue
                hours = (dt - now).total_seconds()/3600
                if hours < 1 or hours > 168: continue
            except: continue

            liq = float(raw.get("liquidityNum") or raw.get("liquidity") or 0)
            vol = float(raw.get("volume") or raw.get("volumeNum") or 0)
            if liq < 5000 or vol < 2000: continue

            op = raw.get("outcomePrices") or []
            if isinstance(op,str):
                try: op=json.loads(op)
                except: op=[]
            on = raw.get("outcomes") or []
            if isinstance(on,str):
                try: on=json.loads(on)
                except: on=[]
            ct = raw.get("clobTokenIds") or []
            if isinstance(ct,str):
                try: ct=json.loads(ct)
                except: ct=[]

            yes_tid=yes_price=no_tid=no_price=None
            for i,name in enumerate(on):
                nl=str(name).lower()
                price=float(op[i]) if i<len(op) else None
                tid=ct[i] if i<len(ct) else None
                if isinstance(tid,str):
                    if "=" in tid: tid=tid.split("=",1)[-1]
                    m2=re.search(r"0x[0-9a-fA-F]+|\d{10,}",tid)
                    if m2: tid=m2.group(0)
                if nl=="yes": yes_tid,yes_price=tid,price
                elif nl=="no": no_tid,no_price=tid,price

            if yes_price is None or no_price is None: continue
            if abs(1.0-yes_price-no_price) > 0.08: continue

            slug=raw.get("slug") or ""
            markets.append({
                "condition_id": raw.get("conditionId") or raw.get("id") or "",
                "question":     raw.get("question") or raw.get("title") or "",
                "end_date":     ed[:10],
                "hours_left":   round(hours,1),
                "yes_price":    yes_price,
                "no_price":     no_price,
                "yes_token_id": yes_tid or "",
                "no_token_id":  no_tid or "",
                "neg_risk":     bool(raw.get("negRisk") or False),
                "market_url":   f"https://polymarket.com/event/{slug}" if slug else "",
                "liquidity":    liq,
            })
        if len(batch) < 100: break
    logger.info(f"Scout fetched {len(markets)} markets")
    return markets
