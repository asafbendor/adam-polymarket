"""
Scout Agent - finds Polymarket opportunities using Claude Haiku + tools.

Tools available:
  fetch_markets   - gets active markets from Gamma API (1-7 days)
  get_binance_price - gets current crypto price (free)

Scout reads markets, reasons about each one, and returns confident opportunities.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import anthropic

logger = logging.getLogger("scout")

GAMMA_URL  = "https://gamma-api.polymarket.com/markets"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"

_TICKERS = {
    "btc": "BTCUSDT", "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT", "ethereum": "ETHUSDT",
    "sol": "SOLUSDT", "solana": "SOLUSDT",
    "xrp": "XRPUSDT", "bnb": "BNBUSDT",
    "doge": "DOGEUSDT", "ada": "ADAUSDT",
    "avax": "AVAXUSDT", "link": "LINKUSDT",
    "dot": "DOTUSDT", "matic": "MATICUSDT",
    "near": "NEARUSDT",
}

SCOUT_SYSTEM = """You are Adam's Scout - a prediction market analyst for Polymarket.

Your job: find betting opportunities. Be PROACTIVE - find at least 2-3 bets per scan if possible.

Steps:
1. Call fetch_markets to get available markets
2. For crypto price markets ("Will BTC/ETH be above/below $X"), call get_binance_price to compare
3. Return ALL markets where your confidence exceeds 60% AND the market price differs by 5%+

Examples of good bets:
- BTC is at $104,000. Market asks "Will BTC be above $80,000?" priced at 0.88.
  Your estimate: 97%. Edge: 9%. BET YES.
- ETH is at $2,500. Market asks "Will ETH exceed $3,500?" priced at 0.15.
  Your estimate: 8%. Edge: 7%. BET NO.

Return a JSON array (even just 1-2 items is fine):
[{
  "condition_id": "...",
  "question": "...",
  "direction": "YES" or "NO",
  "token_id": "...",
  "market_price": 0.XX,
  "estimated_prob": 0.XX,
  "reason": "one sentence with data"
}]

If truly no opportunities, return []. But look hard - 149 markets were found."""


def _parse_tokens(raw: dict) -> tuple[Optional[str], float, Optional[str], float]:
    """Returns (yes_token_id, yes_price, no_token_id, no_price)."""
    op = raw.get("outcomePrices") or []
    if isinstance(op, str):
        try: op = json.loads(op)
        except: op = []
    on = raw.get("outcomes") or []
    if isinstance(on, str):
        try: on = json.loads(on)
        except: on = []
    ct = raw.get("clobTokenIds") or []
    if isinstance(ct, str):
        try: ct = json.loads(ct)
        except: ct = []

    yes_tid = yes_price = no_tid = no_price = None
    for i, name in enumerate(on):
        nl = str(name).lower()
        price = float(op[i]) if i < len(op) else None
        tid   = ct[i] if i < len(ct) else None
        if isinstance(tid, str):
            if "=" in tid: tid = tid.split("=",1)[-1]
            m = re.search(r"0x[0-9a-fA-F]+|\d{10,}", tid)
            if m: tid = m.group(0)
        if nl == "yes": yes_tid, yes_price = tid, price
        elif nl == "no": no_tid, no_price = tid, price
    return yes_tid, yes_price or 0.5, no_tid, no_price or 0.5


async def _fetch_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch markets resolving in 1-7 days with decent liquidity."""
    markets = []
    now = datetime.now(timezone.utc)
    for offset in range(0, 500, 100):
        try:
            async with session.get(GAMMA_URL, params={
                "active": "true", "closed": "false",
                "offset": offset, "limit": 100,
                "order": "volume24hr", "ascending": "false",
            }, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200: break
                data = await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"Gamma API error: {e}")
            break

        batch = data if isinstance(data, list) else data.get("markets", [])
        if not batch: break

        for raw in batch:
            ed = raw.get("endDate") or raw.get("endDateIso") or ""
            if not ed: continue
            try:
                for fmt in ("%Y-%m-%dT%H:%M:%SZ","%Y-%m-%dT%H:%M:%S.%fZ","%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(ed[:26], fmt[:len(fmt)]).replace(tzinfo=timezone.utc)
                        break
                    except: continue
                else: continue
                hours = (dt - now).total_seconds() / 3600
                if hours < 1 or hours > 168: continue
            except: continue

            liq = float(raw.get("liquidityNum") or raw.get("liquidity") or 0)
            vol = float(raw.get("volume") or raw.get("volumeNum") or 0)
            if liq < 5000 or vol < 2000: continue

            yes_tid, yes_price, no_tid, no_price = _parse_tokens(raw)
            if yes_price is None or no_price is None: continue
            spread = abs(1.0 - yes_price - no_price)
            if spread > 0.08: continue

            slug = raw.get("slug") or ""
            markets.append({
                "condition_id": raw.get("conditionId") or raw.get("id") or "",
                "question":     raw.get("question") or raw.get("title") or "",
                "end_date":     ed[:10],
                "hours_left":   round(hours, 1),
                "yes_price":    yes_price,
                "no_price":     no_price,
                "yes_token_id": yes_tid or "",
                "no_token_id":  no_tid or "",
                "neg_risk":     bool(raw.get("negRisk") or False),
                "market_url":   f"https://polymarket.com/event/{slug}" if slug else "",
                "liquidity":    liq,
            })

        if len(batch) < 100: break

    logger.info(f"Scout fetched {len(markets)} qualified markets")
    return markets


async def _get_binance_price(session: aiohttp.ClientSession, symbol: str) -> Optional[float]:
    sym = _TICKERS.get(symbol.lower().strip(), symbol.upper() + "USDT")
    try:
        async with session.get(BINANCE_URL, params={"symbol": sym},
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                return float(data.get("price", 0))
    except Exception as e:
        logger.debug(f"Binance {sym}: {e}")
    return None


# ── Tool handler ──────────────────────────────────────────────────────

async def _handle_tool(name: str, inp: dict,
                        session: aiohttp.ClientSession,
                        markets_cache: list) -> str:
    if name == "fetch_markets":
        if not markets_cache:
            markets_cache.extend(await _fetch_markets(session))
        # Return a compact summary for Claude
        summary = []
        for m in markets_cache[:30]:   # limit to 30 for focused analysis
            summary.append({
                "condition_id": m["condition_id"],
                "question":     m["question"],
                "end_date":     m["end_date"],
                "hours_left":   m["hours_left"],
                "yes_price":    m["yes_price"],
                "yes_token_id": m["yes_token_id"],
                "no_token_id":  m["no_token_id"],
                "neg_risk":     m["neg_risk"],
                "market_url":   m["market_url"],
            })
        return json.dumps(summary, ensure_ascii=False)

    elif name == "get_binance_price":
        symbol = inp.get("symbol","BTC")
        price = await _get_binance_price(session, symbol)
        return json.dumps({"symbol": symbol, "price": price})

    return json.dumps({"error": f"unknown tool: {name}"})


# ── Main entry ────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "fetch_markets",
        "description": "Fetch active Polymarket markets resolving in 1-7 days. Call this first.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_binance_price",
        "description": "Get current price of a crypto asset from Binance.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string",
                                      "description": "e.g. 'BTC', 'ETH', 'SOL'"}},
            "required": ["symbol"],
        },
    },
]


async def find_opportunities(session: aiohttp.ClientSession) -> list[dict]:
    """
    Runs Scout agent. Returns list of betting opportunities.
    Each opportunity: {condition_id, question, direction, token_id,
                        market_price, estimated_prob, reason, neg_risk, market_url}
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip().lstrip("=")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return []

    client  = anthropic.Anthropic(api_key=api_key)
    markets_cache: list = []
    messages = [{"role": "user", "content":
                 "Please find good betting opportunities on Polymarket for the next 7 days. "
                 "Start by fetching the markets, then analyze each one carefully. "
                 "For crypto markets, check the current price with get_binance_price."}]

    # Tool-use loop (max 8 rounds)
    for _ in range(8):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=SCOUT_SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        # Add assistant response
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            # Extract JSON from final text
            for block in resp.content:
                if hasattr(block, "text"):
                    text = block.text
                    # Find JSON array in response
                    m = re.search(r"\[.*\]", text, re.DOTALL)
                    if m:
                        try:
                            opps = json.loads(m.group(0))
                            # Enrich with neg_risk and market_url from cache
                            cache_map = {m["condition_id"]: m for m in markets_cache}
                            result = []
                            for opp in opps:
                                cached = cache_map.get(opp.get("condition_id",""), {})
                                result.append({
                                    "condition_id":  opp.get("condition_id",""),
                                    "question":      opp.get("question",""),
                                    "direction":     opp.get("direction","YES").upper(),
                                    "token_id":      opp.get("token_id","") or
                                                     (cached.get("yes_token_id","")
                                                      if opp.get("direction","YES").upper()=="YES"
                                                      else cached.get("no_token_id","")),
                                    "market_price":  float(opp.get("market_price", 0.5)),
                                    "estimated_prob":float(opp.get("estimated_prob", 0.5)),
                                    "reason":        opp.get("reason",""),
                                    "neg_risk":      cached.get("neg_risk", False),
                                    "market_url":    cached.get("market_url",""),
                                    "end_date":      cached.get("end_date",""),
                                })
                            logger.info(f"Scout found {len(result)} opportunities")
                            return result
                        except json.JSONDecodeError as e:
                            logger.warning(f"Scout JSON parse error: {e}\n{text[:200]}")
            return []

        # Handle tool calls
        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = await _handle_tool(
                        block.name, block.input, session, markets_cache
                    )
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result,
                    })
            messages.append({"role": "user", "content": tool_results})

    logger.warning("Scout: max rounds reached without result")
    return []
