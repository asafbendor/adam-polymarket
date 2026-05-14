"""
Trader Agent - places orders using the proven py-clob-client 0.34.6.
This exact pattern worked in the old polymarket-bot project.
No V2, no extras - just what works.
"""
import asyncio
import logging
import math
import os
from typing import Optional

import memory

logger = logging.getLogger("trader")


class TraderAgent:
    def __init__(self):
        self._key   = os.getenv("POLYMARKET_PRIVATE_KEY","").strip().lstrip("=")
        self._proxy = os.getenv("POLYMARKET_PROXY_ADDRESS","").strip().lstrip("=")
        self._client = None
        self._init()

    def _init(self):
        """
        Hybrid init:
        - Old client (0.34.6) derives API credentials (auth) - this works
        - V2 client places orders with correct EIP-712 version "2" - fixes order_version_mismatch
        - Credentials from old client are passed to V2 client
        """
        if not self._key or not self._proxy:
            logger.error("Missing POLYMARKET credentials")
            return

        # Step 1: derive credentials using old client (proven to work)
        creds = None
        try:
            from py_clob_client.client import ClobClient as OldClient
            from py_clob_client.constants import POLYGON as OLD_POLYGON
            old = OldClient(
                host="https://clob.polymarket.com",
                key=self._key, chain_id=OLD_POLYGON,
                signature_type=2, funder=self._proxy,
            )
            creds = old.create_or_derive_api_creds()
            logger.warning(f"Creds from old client OK: {getattr(creds,'api_key','?')[:8]}...")
        except Exception as e:
            logger.error(f"Old client creds failed: {e}")
            memory.remember("trader", "creds_error", str(e))
            return

        # Step 2: init V2 client for order placement (correct EIP-712 v2 signing)
        try:
            from py_clob_client_v2.client import ClobClient as V2Client
            from py_clob_client_v2.constants import POLYGON as V2_POLYGON
            self._client = V2Client(
                host="https://clob.polymarket.com",
                key=self._key, chain_id=V2_POLYGON,
                signature_type=2,   # proxy wallet - same as old client
                funder=self._proxy,
            )
            # Set credentials derived by old client
            self._client.set_api_creds(creds)
            logger.warning("V2 client init OK with old credentials")
            memory.remember("trader", "status", "V2 client + old credentials hybrid")
        except Exception as e:
            logger.error(f"V2 client init failed: {e}")
            # Fallback: try V2 with sig_type=0
            try:
                from py_clob_client_v2.client import ClobClient as V2Client
                from py_clob_client_v2.constants import POLYGON as V2_POLYGON
                self._client = V2Client(
                    host="https://clob.polymarket.com",
                    key=self._key, chain_id=V2_POLYGON,
                    signature_type=0, funder=self._proxy,
                )
                self._client.set_api_creds(creds)
                logger.warning("V2 client init OK (sig_type=0 fallback)")
                memory.remember("trader", "status", "V2 sig_type=0 fallback")
            except Exception as e2:
                logger.error(f"V2 fallback also failed: {e2}")
                memory.remember("trader", "init_error", str(e2))

    def place_bet(self, token_id: str, market_price: float,
                  bet_size: float = 1.0, neg_risk: bool = False,
                  condition_id: str = "", direction: str = "YES") -> dict:
        if not self._client:
            return {"ok": False, "order_id": "", "message": "Client not initialized"}

        limit_price = round(min(market_price * 1.03, 0.97), 4)

        # Resolve real token_id from CLOB
        try:
            loop = asyncio.get_event_loop()
            data = loop.run_until_complete(
                asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._client.get_market(condition_id)
                )
            ) if condition_id else {}
            for tok in (data.get("tokens") or []):
                if str(tok.get("outcome","")).upper() == direction.upper():
                    token_id = str(tok["token_id"])
                    break
        except Exception:
            pass  # use token_id from Scout

        shares = math.ceil(bet_size / limit_price * 100) / 100
        while limit_price * shares < 1.0:
            shares = round(shares + 0.01, 2)

        try:
            # Use V2 OrderArgs for correct EIP-712 version "2" signing
            try:
                from py_clob_client_v2.clob_types import OrderArgs
                from py_clob_client_v2.order_builder.constants import BUY
                order_args = OrderArgs(token_id=token_id, price=limit_price,
                                       size=shares, side=BUY)
            except Exception:
                from py_clob_client.clob_types import OrderArgs
                order_args = OrderArgs(token_id=token_id, price=limit_price,
                                       size=shares, side="BUY")

            resp = self._client.create_and_post_order(order_args)
            logger.warning(f"[LIVE] Response: {repr(resp)[:300]}")

            order_id = ""
            if isinstance(resp, dict):
                order_id = (
                    resp.get("orderID") or resp.get("order_id") or
                    (resp.get("order") or {}).get("id") or ""
                )
            elif hasattr(resp, "order_id"):
                order_id = resp.order_id or ""

            memory.remember("trader", "last_success",
                            f"order_id={order_id} price={limit_price} shares={shares}")
            memory.agent_log("trader", f"Order placed: {direction} {shares}sh @ {limit_price}")
            return {"ok": True, "order_id": order_id, "limit_price": limit_price,
                    "message": f"Order placed: {shares}sh @ {limit_price}"}

        except Exception as e:
            import traceback
            msg = f"{type(e).__name__}: {e}"
            logger.warning(f"Order failed: {msg}")
            memory.remember("trader", f"last_error", msg[:200])
            memory.agent_log("trader", f"Order failed: {msg[:150]}")
            return {"ok": False, "order_id": "", "limit_price": limit_price, "message": msg}

    def get_balance(self) -> Optional[float]:
        if not self._client:
            return None
        try:
            for call in [
                lambda: self._client.get_balance_allowance(params={"asset_type": 0}),
                lambda: self._client.get_balance_allowance({"asset_type": 0}),
                lambda: self._client.get_balance_allowance(),
            ]:
                try:
                    data = call()
                    if data:
                        raw = data.get("balance") or data.get("balance_usdc") or 0
                        return float(raw) / 1_000_000
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def get_order(self, order_id: str) -> dict:
        if not self._client or not order_id:
            return {}
        try:
            return self._client.get_order(order_id) or {}
        except Exception:
            return {}

    def _handle_tool(self, name: str, inp: dict) -> str:
        """Keep compatibility with adam.py balance check."""
        import json
        if name == "get_balance":
            bal = self.get_balance()
            return json.dumps({"usdc": bal})
        return json.dumps({"error": f"unknown: {name}"})
