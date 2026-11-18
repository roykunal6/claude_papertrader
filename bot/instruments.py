"""Find Nifty option contracts from the INDstocks F&O instrument list (CSV)."""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import date, datetime

DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y", "%Y%m%d", "%d%b%Y")


@dataclass(frozen=True)
class Contract:
    scrip: str          # e.g. "NFO_51011", used for quotes and candles
    symbol: str         # trading symbol
    strike: float
    opt_type: str       # "CE" or "PE"
    expiry: date
    lot_size: int


def parse_date(text: str) -> date:
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognised expiry date format: {text!r}")


def _is_underlying(row: dict, underlying: str) -> bool:
    u = underlying.upper()
    for key in ("SYMBOL_NAME", "UNDERLYING_SCRIP_NAME"):
        v = (row.get(key) or "").strip().upper()
        if v in (u, f"{u} 50"):
            return True
    return False


def load_nifty_options(csv_text: str, underlying: str = "NIFTY") -> list[Contract]:
    out: list[Contract] = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        row = {k.strip(): (v or "") for k, v in row.items() if k}
        opt = row.get("OPTION_TYPE", "").strip().upper()
        if opt not in ("CE", "PE"):
            continue
        if row.get("EXCH", "NSE").strip().upper() not in ("NSE", "NFO"):
            continue
        if not _is_underlying(row, underlying):
            continue
        try:
            out.append(Contract(
                scrip=f"NFO_{row['SECURITY_ID'].strip()}",
                symbol=row.get("TRADING_SYMBOL", "").strip(),
                strike=float(row["STRIKE_PRICE"]),
                opt_type=opt,
                expiry=parse_date(row["EXPIRY_DATE"]),
                lot_size=int(float(row["LOT_UNITS"])),
            ))
        except (KeyError, ValueError):
            continue
    return out


def nearest_expiry(contracts: list[Contract], today: date) -> date | None:
    future = sorted({c.expiry for c in contracts if c.expiry >= today})
    return future[0] if future else None


def itm_strikes(spot: float, step: int) -> tuple[float, float]:
    """Nearest in-the-money strikes: (call strike below spot, put strike above spot)."""
    ce = math.floor(spot / step) * step
    if ce >= spot:
        ce -= step
    pe = math.ceil(spot / step) * step
    if pe <= spot:
        pe += step
    return float(ce), float(pe)


def pick(contracts: list[Contract], expiry: date, strike: float, opt_type: str) -> Contract | None:
    for c in contracts:
        if c.expiry == expiry and c.opt_type == opt_type and abs(c.strike - strike) < 0.01:
            return c
    return None
