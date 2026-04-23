import time

import requests
from config import settings
from eth_account import Account as EthAccount
from hyperliquid.utils.signing import float_to_wire, sign_l1_action
from src.exchanges.base import BaseExchange, Order, OrderSide, OrderType, Position
from src.utils.logger import logger

API_URL = "https://api.hyperliquid.xyz"


def _post(payload: dict, timeout: int = 15) -> dict:
    resp = requests.post(f"{API_URL}/info", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


class HyperliquidExchange(BaseExchange):
    def __init__(self):
        self.account_address = settings.HYPERLIQUID_ACCOUNT_ADDRESS
        self.private_key = settings.HYPERLIQUID_API_KEY
        # Balance query address — main wallet with funds
        self.wallet_address = settings.HYPERLIQUID_WALLET_ADDRESS or self.account_address
        self._coin_to_asset: dict[str, int] = {}
        self._sz_decimals: dict[str, int] = {}
        self._use_vault: bool = False  # vaultAddress in signing
        self._load_meta()
        self._detect_trading_mode()
        logger.info(f"Hyperliquid exchange initialized — wallet: {self.wallet_address}, signer: {self.account_address}")

    def _detect_trading_mode(self):
        """Auto-detect whether to use vault mode or direct signing.

        - If wallet_address == account_address → direct signing
        - If signer has funds → direct signing
        - If only main wallet has funds → try vault, fallback to direct (API wallet auth)
        """
        if self.wallet_address == self.account_address:
            self._use_vault = False
            return

        try:
            signer_bal = self._query_balance(self.account_address)
            wallet_bal = self._query_balance(self.wallet_address)

            if wallet_bal > 0:
                # Main wallet has funds — try vault first, fallback to direct
                # API wallet auth works via direct signing (no vaultAddress needed)
                self._use_vault = True  # will be tried first
                logger.info(f"API wallet mode: wallet=${wallet_bal:.2f}, signer=${signer_bal:.2f}")
            else:
                self._use_vault = False
                logger.warning(f"No funds — wallet: ${wallet_bal:.2f}, signer: ${signer_bal:.2f}")
        except Exception as e:
            logger.warning(f"Could not auto-detect trading mode: {e}")
            self._use_vault = False

    @staticmethod
    def _query_balance(address: str) -> float:
        data = _post({"type": "clearinghouseState", "user": address})
        return float(data.get("marginSummary", {}).get("accountValue", 0))

    def _load_meta(self):
        """Load coin-to-asset mapping from Hyperliquid metadata."""
        try:
            meta = _post({"type": "meta"})
            for i, entry in enumerate(meta.get("universe", [])):
                name = entry["name"]
                self._coin_to_asset[name] = i
                self._sz_decimals[name] = entry.get("szDecimals", 8)
            logger.info(f"Loaded {len(self._coin_to_asset)} perp assets from Hyperliquid")
        except Exception as e:
            logger.warning(f"Failed to load Hyperliquid metadata: {e}")

    # ---- Info queries ----

    def _coin(self, symbol: str) -> str:
        """Convert pair like BTC/USDC to Hyperliquid coin name BTC."""
        return symbol.split("/")[0].split("-")[0]

    def _asset(self, symbol: str) -> int:
        """Get the integer asset ID for a symbol."""
        coin = self._coin(symbol)
        asset = self._coin_to_asset.get(coin)
        if asset is None:
            raise ValueError(f"Unknown coin: {coin} — not found in Hyperliquid metadata")
        return asset

    def get_funding_rate(self, symbol: str) -> float:
        """Return the current *per-hour* funding rate for the perp.

        The Hyperliquid ``metaAndAssetCtxs`` payload is ``[meta, ctxs]`` with
        one ctx per universe asset; the ``funding`` field is the next-hour
        funding rate as a decimal (0.0001 = 0.01 % / hour). Missing or
        malformed data returns 0.0 so callers can treat it as "no info".
        """
        try:
            asset_idx = self._asset(symbol)
            data = _post({"type": "metaAndAssetCtxs"})
            # Response shape: [meta, [ctx0, ctx1, ...]]
            if not isinstance(data, list) or len(data) < 2:
                return 0.0
            ctxs = data[1]
            if asset_idx >= len(ctxs):
                return 0.0
            return float(ctxs[asset_idx].get("funding", 0.0))
        except Exception as e:
            logger.debug(f"Failed to fetch funding rate for {symbol}: {e}")
            return 0.0

    def get_open_interest(self, symbol: str) -> float:
        """Return the current open interest in BASE-asset units.

        From the same ``metaAndAssetCtxs`` payload we used for funding. The
        ``openInterest`` field is a string representing the notional open
        interest denominated in the base asset (e.g. "12345.6" BTC).
        Returns 0.0 on any parse or lookup failure so callers can treat it
        as "no signal".
        """
        try:
            asset_idx = self._asset(symbol)
            data = _post({"type": "metaAndAssetCtxs"})
            if not isinstance(data, list) or len(data) < 2:
                return 0.0
            ctxs = data[1]
            if asset_idx >= len(ctxs):
                return 0.0
            return float(ctxs[asset_idx].get("openInterest", 0.0))
        except Exception as e:
            logger.debug(f"Failed to fetch OI for {symbol}: {e}")
            return 0.0

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
        coin = self._coin(symbol)
        now_ms = int(time.time() * 1000)
        tf_seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        sec = tf_seconds.get(timeframe, 3600)
        start_ms = now_ms - (limit * sec * 1000)
        try:
            data = _post({
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": timeframe, "startTime": start_ms, "endTime": now_ms},
            })
        except Exception as e:
            logger.warning(f"Failed to fetch OHLCV for {coin}: {e}")
            return []
        return [
            [c["t"], float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"])]
            for c in data
        ]

    def fetch_balance(self) -> dict:
        data = _post({"type": "clearinghouseState", "user": self.wallet_address})
        margin = data.get("marginSummary", {})
        total = float(margin.get("accountValue", 0))
        used = float(margin.get("totalMarginUsed", 0))
        free = total - used
        return {
            "free": free,
            "used": used,
            "total": total,
        }

    def get_positions(self) -> list[Position]:
        data = _post({"type": "clearinghouseState", "user": self.wallet_address})
        positions = []
        for pos in data.get("assetPositions", []):
            sz = float(pos["position"]["szi"])
            if sz != 0:
                positions.append(Position(
                    symbol=pos["position"]["coin"],
                    side="long" if sz > 0 else "short",
                    size=abs(sz),
                    entry_price=float(pos["position"]["entryPx"]),
                    unrealized_pnl=float(pos["position"]["unrealizedPnl"]),
                ))
        return positions

    def get_ticker(self, symbol: str) -> dict:
        coin = self._coin(symbol)
        mid = self._get_mid_price(coin)
        return {"symbol": symbol, "last": mid, "bid": mid}

    def _get_mid_price(self, coin: str) -> float:
        data = _post({"type": "allMids"})
        return float(data.get(coin, 0))

    def _infer_tick_size(self, mid: float) -> float:
        """Infer price tick size from mid price magnitude."""
        if mid >= 10000:
            return 1.0      # BTC: $1
        elif mid >= 1000:
            return 0.1      # ETH: $0.1
        elif mid >= 100:
            return 0.01     # SOL: $0.01
        elif mid >= 10:
            return 0.001
        elif mid >= 1:
            return 0.0001
        return 0.00001

    def _round_price(self, price: float, tick: float, round_up: bool) -> float:
        """Round price to tick size, up or down."""
        import math
        steps = price / tick
        if round_up:
            return math.ceil(steps) * tick
        return math.floor(steps) * tick

    # ---- Order placement (signed via eth_account) ----

    def place_order(self, symbol: str, side: OrderSide, amount: float,
                    order_type: OrderType = OrderType.MARKET, price: float | None = None) -> Order:
        # Defensive: accept both OrderSide enum and plain "buy"/"sell" strings.
        # Historically a bug at the caller passed decision["action"] (str), which
        # crashed at logger.info(f"{side.value}…") AFTER the order was already
        # filled on the exchange — leaving an orphan position with no SL/TP and
        # no journal entry. Normalize at the boundary to make this impossible.
        if not isinstance(side, OrderSide):
            side = OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL
        coin = self._coin(symbol)
        asset = self._asset(symbol)
        is_buy = side == OrderSide.BUY
        nonce = int(time.time() * 1000)

        sz_decimals = self._sz_decimals.get(coin, 8)
        import math
        # Round UP to avoid falling below minimum notional ($10)
        multiplier = 10 ** sz_decimals
        rounded_amount = math.ceil(amount * multiplier) / multiplier
        sz_str = float_to_wire(rounded_amount)

        if order_type == OrderType.MARKET:
            # IOC with aggressive price to simulate market order
            mid = self._get_mid_price(coin)
            if mid > 0:
                tick = self._infer_tick_size(mid)
                raw_px = mid * 1.05 if is_buy else mid * 0.95
                limit_px = self._round_price(raw_px, tick, round_up=is_buy)
            else:
                limit_px = price or 0
            order_wire = {
                "a": asset,
                "b": is_buy,
                "p": float_to_wire(limit_px),
                "s": sz_str,
                "r": False,
                "t": {"limit": {"tif": "Ioc"}},
            }
        else:
            order_wire = {
                "a": asset,
                "b": is_buy,
                "p": float_to_wire(price),
                "s": sz_str,
                "r": False,
                "t": {"limit": {"tif": "Gtc"}},
            }

        result = self._send_order(order_wire, nonce)

        logger.info(f"Hyperliquid order: {side.value} {amount} {symbol} result={result}")

        # Parse response: {"status": "ok", "response": {"data": {"statuses": [...]}}}
        # Error response: {"status": "err", "response": "error string"}
        api_status = result.get("status", "")
        if api_status == "err":
            error_msg = result.get("response", "unknown error")
            logger.error(f"Hyperliquid API error: {error_msg}")
            return Order(id="", symbol=symbol, side=side, type=order_type,
                         price=price or 0, amount=amount, status="rejected")

        resp_data = result.get("response", {})
        if isinstance(resp_data, str):
            logger.error(f"Hyperliquid API error: {resp_data}")
            return Order(id="", symbol=symbol, side=side, type=order_type,
                         price=price or 0, amount=amount, status="rejected")

        statuses = resp_data.get("data", {}).get("statuses", [])
        oid = ""
        st = "rejected"
        if statuses:
            s = statuses[0]
            if "resting" in s:
                oid = str(s["resting"].get("oid", ""))
                st = "open"
            elif "filled" in s:
                oid = str(s["filled"].get("oid", ""))
                st = "filled"
            elif "error" in s:
                logger.error(f"Hyperliquid order error: {s['error']}")
                st = "rejected"

        return Order(
            id=oid,
            symbol=symbol,
            side=side,
            type=order_type,
            price=price or 0,
            amount=amount,
            status=st,
        )

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        asset = self._asset(symbol)
        nonce = int(time.time() * 1000)
        cancel_action = {"a": asset, "o": int(order_id)}
        result = self._send_action("cancel", cancel_action, nonce)
        logger.info(f"Hyperliquid order cancelled: {order_id}")
        return result

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Get open (trigger) orders, optionally filtered by symbol."""
        data = _post({"type": "openOrders", "user": self.wallet_address})
        if symbol:
            coin = self._coin(symbol)
            data = [o for o in data if o.get("coin") == coin]
        return data

    def cancel_trigger_orders(self, symbol: str) -> int:
        """Cancel all open trigger orders for a symbol. Returns count cancelled."""
        orders = self.get_open_orders(symbol)
        cancelled = 0
        for o in orders:
            oid = o.get("oid")
            if oid:
                try:
                    self.cancel_order(str(oid), symbol)
                    cancelled += 1
                except Exception as e:
                    logger.warning(f"Failed to cancel order {oid}: {e}")
        if cancelled:
            logger.info(f"Cancelled {cancelled} trigger orders for {symbol}")
        return cancelled

    def _build_sl_tp_action(
        self, symbol: str, side: OrderSide, amount: float,
        sl_price: float, tp_price: float, reference_price: float,
    ) -> dict:
        """Build the normalTpsl action payload for SL/TP reduce-only pair."""
        if not isinstance(side, OrderSide):
            side = OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL
        coin = self._coin(symbol)
        asset = self._asset(symbol)
        is_long = side == OrderSide.BUY
        close_side = not is_long

        sz_decimals = self._sz_decimals.get(coin, 8)
        import math
        multiplier = 10 ** sz_decimals
        rounded_sz = math.ceil(amount * multiplier) / multiplier
        sz_str = float_to_wire(rounded_sz)

        tick = self._infer_tick_size(reference_price)

        sl_wire = {
            "a": asset,
            "b": close_side,
            "p": float_to_wire(self._round_price(sl_price, tick, not is_long)),
            "s": sz_str,
            "r": True,
            "t": {"trigger": {"triggerPx": float_to_wire(self._round_price(sl_price, tick, is_long)), "isMarket": True, "tpsl": "sl"}},
        }
        tp_wire = {
            "a": asset,
            "b": close_side,
            "p": float_to_wire(self._round_price(tp_price, tick, is_long)),
            "s": sz_str,
            "r": True,
            "t": {"trigger": {"triggerPx": float_to_wire(self._round_price(tp_price, tick, not is_long)), "isMarket": True, "tpsl": "tp"}},
        }
        return {"type": "order", "orders": [sl_wire, tp_wire], "grouping": "normalTpsl"}

    def _send_tpsl(self, action: dict) -> dict:
        nonce = int(time.time() * 1000)
        wallet = self._get_wallet()
        vault_addr = self.wallet_address if self._use_vault else None
        signature = sign_l1_action(wallet, action, vault_addr, nonce, None, True)
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": vault_addr,
            "expiresAfter": None,
        }
        resp = requests.post(f"{API_URL}/exchange", json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def place_sl_tp(self, symbol: str, close_side: str, amount: float,
                    sl_price: float, tp_price: float) -> dict:
        """Place a new SL/TP pair (e.g. after a trailing update).

        `close_side` is the side that CLOSES the current position:
          - long positions  → "sell"
          - short positions → "buy"
        """
        side_enum = OrderSide.SELL if close_side == "sell" else OrderSide.BUY
        # The action builder expects the ORIGINAL trade side, so invert:
        orig_side = OrderSide.BUY if side_enum == OrderSide.SELL else OrderSide.SELL
        ref_px = self._get_mid_price(self._coin(symbol)) or sl_price
        action = self._build_sl_tp_action(symbol, orig_side, amount, sl_price, tp_price, ref_px)
        try:
            result = self._send_tpsl(action)
            if result.get("status") == "err":
                logger.error(f"Trailing SL/TP re-place failed: {result.get('response')}")
            else:
                logger.info(f"Trailing SL/TP re-placed: SL={sl_price:.4f} TP={tp_price:.4f} {symbol}")
            return result
        except Exception as e:
            logger.error(f"Trailing SL/TP re-place error: {e}")
            return {"status": "err", "response": str(e)}

    def place_order_with_sl_tp(
        self, symbol: str, side: OrderSide, amount: float,
        entry_price: float, sl_price: float, tp_price: float,
    ) -> tuple[Order, float | None, float | None]:
        """Place market entry order, then attach SL/TP trigger pair.

        Returns (entry_order, sl_price, tp_price).
        If entry fails, sl/tp will be None.
        """
        if not isinstance(side, OrderSide):
            side = OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL
        # 1. Place entry order
        entry_order = self.place_order(symbol, side, amount, OrderType.MARKET)
        if entry_order.status not in ("filled", "open"):
            logger.error(f"Entry order failed ({entry_order.status}), skipping SL/TP")
            return entry_order, None, None

        # 2/3. Build and send SL+TP pair
        action = self._build_sl_tp_action(symbol, side, amount, sl_price, tp_price, entry_price)
        try:
            result = self._send_tpsl(action)
            if result.get("status") == "err":
                logger.error(f"SL/TP placement failed: {result.get('response')}")
            else:
                logger.info(f"SL/TP placed: SL={sl_price:.2f}, TP={tp_price:.2f} for {symbol}")
        except Exception as e:
            logger.error(f"SL/TP placement error: {e}")

        return entry_order, sl_price, tp_price

    # ---- Signing helpers ----

    def _get_wallet(self):
        return EthAccount.from_key(self.private_key)

    def _send_order(self, order_wire: dict, nonce: int) -> dict:
        """Sign and send an order via Hyperliquid's EIP-712 flow."""
        wallet = self._get_wallet()
        vault_addr = self.wallet_address if self._use_vault else None

        action = {
            "type": "order",
            "orders": [order_wire],
            "grouping": "na",
        }

        signature = sign_l1_action(
            wallet,
            action,
            vault_addr,
            nonce,
            None,
            True,
        )

        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": vault_addr,
            "expiresAfter": None,
        }

        resp = requests.post(f"{API_URL}/exchange", json=payload, timeout=15)
        resp.raise_for_status()
        result = resp.json()

        # If vault mode fails, retry without vault (API wallet direct auth)
        if self._use_vault and result.get("status") == "err" and "not registered" in str(result.get("response", "")).lower():
            logger.warning("Vault not registered, retrying direct API wallet signing...")
            self._use_vault = False
            new_nonce = int(time.time() * 1000) + 1
            return self._send_order(order_wire, new_nonce)

        return result

    def _send_action(self, action_type: str, action: dict, nonce: int) -> dict:
        wallet = self._get_wallet()
        vault_addr = self.wallet_address if self._use_vault else None
        signature = sign_l1_action(wallet, action, vault_addr, nonce, None, True)
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": vault_addr,
            "expiresAfter": None,
        }
        resp = requests.post(f"{API_URL}/exchange", json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
