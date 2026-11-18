"""Charges for one NSE options order (premium side). Rates are set in config.toml."""
from __future__ import annotations


def order_charges(premium_value: float, side: str, rates: dict) -> float:
    """Return total charges in rupees for one order.

    premium_value: price * quantity
    side: "BUY" or "SELL"
    """
    brokerage = rates["brokerage_per_order"]
    exchange = premium_value * rates["exchange_txn_pct"] / 100
    sebi = premium_value * rates["sebi_fee_pct"] / 100
    stt = premium_value * rates["stt_sell_pct"] / 100 if side == "SELL" else 0.0
    stamp = premium_value * rates["stamp_buy_pct"] / 100 if side == "BUY" else 0.0
    gst = (brokerage + exchange + sebi) * rates["gst_pct"] / 100
    return round(brokerage + exchange + sebi + stt + stamp + gst, 2)
