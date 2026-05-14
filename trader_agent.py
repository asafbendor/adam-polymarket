"""
Trader Agent - autonomous execution agent with self-healing capabilities.

When an order fails, Trader:
1. Reads the error message
2. Checks its memory for known fixes
3. Inspects available client methods
4. Tries different approaches
5. Remembers what worked for next time

This means it fixes order_version_mismatch, auth errors, etc. WITHOUT human intervention.
"""
import json
import logging
import math
import os
import re
from typing import Optional

import anthropic

import memory

logger = logging.getLogger("trader")

TOOLS = [
    {
        "name": "inspect_client",
        "description": "List all available methods on the Polymarket CLOB client. Use when unsure about API.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "try_place_order",
        "description": "Attempt to place a bet order with given parameters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "token_id":    {"type": "string"},
                "price":       {"type": "number", "description": "Limit price 0.01-0.97"},
                "size":        {"type": "number", "description": "Number of shares"},
                "neg_risk":    {"type": "boolean", "default": False},
                "sig_type":    {"type": "integer", "description": "0=EOA, 1=PROXY", "default": 0},
                "use_creds":   {"type": "boolean", "description": "Whether to set API creds", "default": True},
            },
            "required": ["token_id", "price", "size"],
        },
    },
    {
        "name": "recall_fixes",
        "description": "Recall known fixes for order errors from memory.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "remember_fix",
        "description": "Store a successful fix for future use.",
        "input_schema": {
            "type": "object",
            "properties": {
                "error_pattern": {"type": "string", "description": "The error that was fixed"},
                "fix":           {"type": "string", "description": "What fixed it"},
            },
            "required": ["error_pattern", "fix"],
        },
    },
    {
        "name": "get_balance",
        "description": "Check USDC balance in the Polymarket account.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

SYSTEM = """You are Trader - an autonomous order execution agent for Polymarket.

Your job: place a bet order. If it fails, diagnose and fix it yourself.

Process:
1. Call recall_fixes to remember known solutions from past errors
2. Call inspect_client to see available methods if unsure
3. Call try_place_order with appropriate parameters
4. If order fails, analyze the error, adjust parameters, and retry
5. If you find a fix, call remember_fix to save it for next time

Common issues and their fixes:
- "create_or_derive_api_creds not found" -> try other method names, or set use_creds=false
- "order_version_mismatch" -> try neg_risk=true, or try different sig_type
- "unauthorized" -> api credentials issue, try use_creds=false (V2 may auto-auth)
- "invalid price" -> round price to 2 decimal places

Return JSON: {"success": true/false, "order_id": "...", "message": "..."}"""


class TraderAgent:
    def __init__(self):
        self._key   = os.getenv("POLYMARKET_PRIVATE_KEY","").strip().lstrip("=")
        self._proxy = os.getenv("POLYMARKET_PROXY_ADDRESS","").strip().lstrip("=")
        self._client = None
        self._init_client(sig_type=0, use_creds=True)

    def _init_client(self, sig_type: int = 0, use_creds: bool = True):
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.constants import POLYGON
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=self._key,
                chain_id=POLYGON,
                signature_type=sig_type,
                funder=self._proxy,
            )
            if use_creds:
                for method in ["create_or_derive_api_creds","derive_api_creds",
                               "get_or_create_api_creds","create_api_key"]:
                    fn = getattr(self._client, method, None)
                    if fn:
                        try:
                            creds = fn()
                            if creds:
                                try: self._client.set_api_creds(creds)
                                except Exception: pass
                                logger.info(f"Creds set via {method}")
                                break
                        except Exception as e:
                            logger.debug(f"{method}: {e}")
            return True
        except Exception as e:
            logger.error(f"Client init failed: {e}")
            return False

    def _try_order(self, token_id: str, price: float, size: float,
                   neg_risk: bool = False) -> dict:
        if not self._client:
            return {"ok": False, "error": "Client not initialized"}
        try:
            from py_clob_client_v2.clob_types import OrderArgs
            from py_clob_client_v2.order_builder.constants import BUY
            args = OrderArgs(token_id=token_id, price=round(price,2), size=size, side=BUY)
            resp = self._client.create_and_post_order(
                args, options={"tick_size":"0.01","neg_risk":neg_risk}
            )
            order_id = ""
            if isinstance(resp,dict):
                order_id = (resp.get("orderID") or resp.get("order_id") or
                            (resp.get("order") or {}).get("id") or "")
            elif hasattr(resp,"order_id"):
                order_id = resp.order_id or ""
            return {"ok": True, "order_id": order_id, "response": repr(resp)[:200]}
        except Exception as e:
            return {"ok": False, "error": str(e), "error_type": type(e).__name__}

    def _handle_tool(self, name: str, inp: dict) -> str:
        if name == "inspect_client":
            if not self._client:
                return json.dumps({"error": "no client"})
            methods = [m for m in dir(self._client) if not m.startswith("_")]
            return json.dumps({"available_methods": methods})

        if name == "try_place_order":
            sig_type  = inp.get("sig_type", 0)
            use_creds = inp.get("use_creds", True)
            # Re-init client if parameters differ
            self._init_client(sig_type=sig_type, use_creds=use_creds)
            result = self._try_order(
                token_id = inp["token_id"],
                price    = inp["price"],
                size     = inp["size"],
                neg_risk = inp.get("neg_risk", False),
            )
            logger.info(f"try_place_order result: {result}")
            memory.agent_log("trader", f"try_place_order: {result}")
            return json.dumps(result)

        if name == "recall_fixes":
            fixes = memory.recall_all("trader")
            return json.dumps(fixes if fixes else {"note": "No fixes stored yet"})

        if name == "remember_fix":
            memory.remember("trader", inp.get("error_pattern",""), inp.get("fix",""))
            return json.dumps({"saved": True})

        if name == "get_balance":
            if not self._client:
                return json.dumps({"error": "no client"})
            try:
                for call in [
                    lambda: self._client.get_balance_allowance(params={"asset_type":0}),
                    lambda: self._client.get_balance_allowance({"asset_type":0}),
                    lambda: self._client.get_balance_allowance(),
                ]:
                    try:
                        d = call()
                        if d:
                            raw = d.get("balance") or d.get("balance_usdc") or 0
                            return json.dumps({"usdc": float(raw)/1_000_000})
                    except Exception: continue
            except Exception as e:
                return json.dumps({"error": str(e)})
            return json.dumps({"usdc": None})

        return json.dumps({"error": f"unknown: {name}"})

    def place_bet(self, token_id: str, market_price: float,
                  bet_size: float = 1.0, neg_risk: bool = False) -> dict:
        """
        Autonomous bet placement with self-healing.
        Uses Claude to reason about failures and retry.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY","").strip().lstrip("=")
        if not api_key:
            # Fallback: try directly without LLM reasoning
            limit_price = round(min(market_price * 1.03, 0.97), 2)
            shares = math.ceil(bet_size / limit_price * 100) / 100
            while limit_price * shares < 1.0:
                shares = round(shares + 0.01, 2)
            return self._try_order(token_id, limit_price, shares, neg_risk)

        limit_price = round(min(market_price * 1.03, 0.97), 2)
        shares = math.ceil(bet_size / limit_price * 100) / 100
        while limit_price * shares < 1.0:
            shares = round(shares + 0.01, 2)

        client   = anthropic.Anthropic(api_key=api_key)
        messages = [{"role": "user", "content":
            f"Place a BUY order with these parameters:\n"
            f"token_id: {token_id}\n"
            f"price: {limit_price}\n"
            f"size: {shares}\n"
            f"neg_risk: {neg_risk}\n\n"
            f"Start by recalling any known fixes, then try placing the order. "
            f"If it fails, diagnose and retry with adjusted parameters."}]

        for _ in range(6):
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "end_turn":
                for block in resp.content:
                    if hasattr(block,"text"):
                        m = re.search(r"\{.*\}", block.text, re.DOTALL)
                        if m:
                            try:
                                result = json.loads(m.group(0))
                                return {"ok": result.get("success",False),
                                        "order_id": result.get("order_id",""),
                                        "message": result.get("message","")}
                            except Exception: pass
                return {"ok": False, "order_id": "", "message": "No result from Trader"}

            if resp.stop_reason == "tool_use":
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        result = self._handle_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                messages.append({"role": "user", "content": tool_results})

        return {"ok": False, "order_id": "", "message": "Max retries reached"}
