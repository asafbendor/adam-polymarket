"""
Trader Agent - places orders on Polymarket via py_clob_client_v2.
Pure execution, no reasoning.
"""
import logging
import math
import os
from typing import Optional

logger = logging.getLogger("trader")


class Trader:
    def __init__(self):
        self._client = None
        self._init()

    def _init(self):
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.constants import POLYGON

            key   = os.getenv("POLYMARKET_PRIVATE_KEY","").strip().lstrip("=")
            proxy = os.getenv("POLYMARKET_PROXY_ADDRESS","").strip().lstrip("=")
            if not key or not proxy:
                logger.error("Missing POLYMARKET_PRIVATE_KEY or POLYMARKET_PROXY_ADDRESS")
                return

            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=key,
                chain_id=POLYGON,
                signature_type=0,   # 0 = EOA (V2)
                funder=proxy,
            )
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            logger.warning(f"Trader ready | api_key={getattr(creds,'api_key','?')[:8]}...")

        except ImportError:
            logger.error("py_clob_client_v2 not installed")
        except Exception as e:
            logger.error(f"Trader init failed: {e}")

    # ── Execution ────────────────────────────────────────────────────

    def place_bet(self, token_id: str, market_price: float,
                  bet_size: float = 1.0, neg_risk: bool = False) -> dict:
        """
        Places a limit BUY order.
        Returns: {"ok": bool, "order_id": str, "limit_price": float, "message": str}
        """
        if not self._client:
            return {"ok": False, "order_id": "", "message": "Trader not initialized"}
        if not token_id:
            return {"ok": False, "order_id": "", "message": "No token_id"}

        limit_price = round(min(market_price * 1.03, 0.97), 2)
        if limit_price <= 0:
            return {"ok": False, "order_id": "", "message": "Invalid price"}

        shares = math.ceil(bet_size / limit_price * 100) / 100
        while limit_price * shares < 1.0:
            shares = round(shares + 0.01, 2)

        try:
            from py_clob_client_v2.clob_types import OrderArgs
            from py_clob_client_v2.order_builder.constants import BUY

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=shares,
                side=BUY,
            )
            resp = self._client.create_and_post_order(
                order_args,
                options={"tick_size": "0.01", "neg_risk": neg_risk},
            )
            logger.warning(f"Trader response: {repr(resp)[:300]}")

            order_id = ""
            if isinstance(resp, dict):
                order_id = (resp.get("orderID") or resp.get("order_id") or
                            (resp.get("order") or {}).get("id") or "")
            elif hasattr(resp, "order_id"):
                order_id = resp.order_id or ""

            return {"ok": True, "order_id": order_id,
                    "limit_price": limit_price, "message": "Order placed"}

        except Exception as e:
            import traceback
            msg = f"{type(e).__name__}: {e}"
            logger.warning(f"Trader place_bet failed: {msg}\n{traceback.format_exc()[:300]}")
            return {"ok": False, "order_id": "", "limit_price": limit_price, "message": msg}

    def get_order(self, order_id: str) -> dict:
        if not self._client or not order_id:
            return {}
        try:
            return self._client.get_order(order_id) or {}
        except Exception as e:
            logger.debug(f"get_order {order_id}: {e}")
            return {}

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
        except Exception as e:
            logger.debug(f"Balance check: {e}")
        return None
