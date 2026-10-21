"""Live broker adapter around smartapi-python (SmartConnect)."""
from __future__ import annotations

import logging
import time
from datetime import datetime

import pyotp

from bot.models import Instrument
from bot.risk import OrderThrottle

log = logging.getLogger("angelbot.broker")

TERMINAL = {"complete": "complete", "rejected": "rejected", "cancelled": "cancelled"}


class BrokerError(Exception):
    pass


class LiveBroker:
    mode = "live"

    def __init__(self, creds: dict, throttle: OrderThrottle | None = None, ordertag: str = "angelbot"):
        from SmartApi import SmartConnect  # imported lazily so tests don't need network

        self.creds = creds
        self.api = SmartConnect(api_key=creds["api_key"])
        self.throttle = throttle or OrderThrottle(3)
        self.ordertag = ordertag
        self.session: dict = {}
        self.last_login = 0.0

    # ---------------- session ----------------
    def login(self, retries: int = 3) -> dict:
        last_err = None
        for i in range(retries):
            try:
                totp = pyotp.TOTP(self.creds["totp_secret"]).now()
                resp = self.api.generateSession(self.creds["client_code"], self.creds["pin"], totp)
                if not resp or not resp.get("status"):
                    raise BrokerError(f"login failed: {resp.get('message') if resp else resp}")
                self.session = resp["data"]
                self.last_login = time.time()
                log.info("logged in as %s", self.session.get("clientcode"))
                return self.session
            except Exception as e:  # network errors, bad TOTP window, etc.
                last_err = e
                log.warning("login attempt %d failed: %s", i + 1, e)
                time.sleep(3 * (i + 1))
        raise BrokerError(f"login failed after {retries} attempts: {last_err}")

    @property
    def jwt(self) -> str:
        return self.session.get("jwtToken", "")

    @property
    def feed_token(self) -> str:
        return self.session.get("feedToken") or self.api.getfeedToken()

    def _call(self, fn, *args, relogin_ok: bool = True):
        """Call a SmartConnect method; re-login once on auth errors."""
        try:
            resp = fn(*args)
        except Exception as e:
            msg = str(e)
            if relogin_ok and any(s in msg for s in ("Invalid Token", "AG8001", "AG8002", "AB1010", "Token")):
                log.warning("auth error (%s) — re-logging in", msg)
                self.login()
                return self._call(fn, *args, relogin_ok=False)
            raise BrokerError(msg) from e
        if isinstance(resp, dict) and resp.get("status") is False:
            code = resp.get("errorcode", "")
            if relogin_ok and code in ("AG8001", "AG8002", "AB1010"):
                self.login()
                return self._call(fn, *args, relogin_ok=False)
            raise BrokerError(f"{code} {resp.get('message')}")
        return resp

    # ---------------- orders ----------------
    def _params(self, inst: Instrument, side: str, qty: int, price: float) -> dict:
        return {
            "variety": "NORMAL",
            "tradingsymbol": inst.symbol,
            "symboltoken": inst.token,
            "transactiontype": side,
            "exchange": inst.exchange,
            "ordertype": "LIMIT",          # algo orders: LIMIT only (no MARKET / IOC)
            "producttype": inst.product,
            "duration": "DAY",
            "price": f"{price:.2f}",
            "quantity": str(int(qty)),
            "ordertag": self.ordertag,
        }

    def place_limit(self, inst: Instrument, side: str, qty: int, price: float) -> str:
        self.throttle.acquire()
        resp = self._call(self.api.placeOrderFullResponse, self._params(inst, side, qty, price))
        oid = (resp or {}).get("data", {}).get("orderid") if isinstance(resp, dict) else None
        if not oid:
            raise BrokerError(f"place order failed: {resp}")
        return str(oid)

    def modify_price(self, order_id: str, inst: Instrument, side: str, qty: int, price: float) -> None:
        self.throttle.acquire()
        p = self._params(inst, side, qty, price)
        p.pop("transactiontype"); p.pop("ordertag")
        p["orderid"] = order_id
        self._call(self.api.modifyOrder, p)

    def cancel(self, order_id: str) -> None:
        self.throttle.acquire()
        self._call(self.api.cancelOrder, order_id, "NORMAL")

    def order_statuses(self, order_ids: list[str]) -> dict[str, dict]:
        if not order_ids:
            return {}
        resp = self._call(self.api.orderBook)
        out = {}
        for o in (resp or {}).get("data") or []:
            oid = str(o.get("orderid"))
            if oid in order_ids:
                st = str(o.get("status", "")).lower()
                out[oid] = {
                    "status": TERMINAL.get(st, "open"),
                    "raw_status": st,
                    "filled_qty": int(float(o.get("filledshares") or 0)),
                    "avg_fill": float(o.get("averageprice") or 0),
                    "reason": o.get("text") or "",
                }
        return out

    # ---------------- account / data ----------------
    def positions(self) -> list[dict]:
        resp = self._call(self.api.position)
        out = []
        for p in (resp or {}).get("data") or []:
            netqty = int(float(p.get("netqty") or 0))
            if netqty == 0:
                continue
            avg = p.get("netprice") or p.get("avgnetprice") or (
                p.get("buyavgprice") if netqty > 0 else p.get("sellavgprice"))
            out.append({"symbol": p.get("tradingsymbol"), "token": str(p.get("symboltoken")),
                        "netqty": netqty, "avg": float(avg or 0), "product": p.get("producttype")})
        return out

    def funds(self) -> dict:
        return (self._call(self.api.rmsLimit) or {}).get("data") or {}

    def ltp(self, inst: Instrument) -> float:
        d = self._call(self.api.ltpData, inst.exchange, inst.symbol, inst.token)
        return float(d["data"]["ltp"])

    def candles(self, inst: Instrument, interval: str, start: datetime, end: datetime) -> list[list]:
        params = {
            "exchange": inst.exchange,
            "symboltoken": inst.token,
            "interval": interval,
            "fromdate": start.strftime("%Y-%m-%d %H:%M"),
            "todate": end.strftime("%Y-%m-%d %H:%M"),
        }
        resp = self._call(self.api.getCandleData, params)
        return (resp or {}).get("data") or []

    def logout(self) -> None:
        try:
            self.api.terminateSession(self.creds["client_code"])
        except Exception:
            pass
