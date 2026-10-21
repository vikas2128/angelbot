from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class Instrument:
    symbol: str          # trading symbol, e.g. SBIN-EQ
    token: str           # symbol token
    exchange: str        # NSE / NFO / BSE ...
    lot_size: int = 1
    tick_size: float = 0.05   # in ₹
    qty: int = 1              # configured qty (shares, or lots for F&O)
    product: str = "INTRADAY"

    @property
    def order_qty(self) -> int:
        return self.qty * self.lot_size

    @property
    def ws_exchange_type(self) -> int:
        # SmartWebSocketV2 exchange types
        return {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "CDS": 13}.get(self.exchange, 1)


@dataclass
class Tick:
    token: str
    ltp: float
    ts: datetime


@dataclass
class Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ticks: int = 0

    def to_dict(self):
        d = asdict(self)
        d["start"] = self.start.isoformat()
        return d


@dataclass
class Position:
    symbol: str
    qty: int = 0                 # +long / -short / 0 flat
    avg_price: float = 0.0
    entry_time: Optional[datetime] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    best_price: Optional[float] = None   # for trailing stop
    realized_pnl: float = 0.0

    @property
    def side(self) -> int:
        return (self.qty > 0) - (self.qty < 0)

    def unrealized(self, ltp: float) -> float:
        return (ltp - self.avg_price) * self.qty if self.qty else 0.0


@dataclass
class OrderRecord:
    order_id: str
    symbol: str
    side: str              # BUY / SELL
    qty: int
    price: float
    purpose: str           # ENTRY / EXIT
    placed_at: datetime
    status: str = "open"   # open / complete / rejected / cancelled
    filled_qty: int = 0
    avg_fill: float = 0.0
    chases: int = 0
    reason: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status in ("complete", "rejected", "cancelled")
