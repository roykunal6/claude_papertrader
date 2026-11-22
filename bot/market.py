"""Load Nifty option contracts and work out how INDstocks wants option codes written."""
from __future__ import annotations

from dataclasses import replace

from .indstocks import IndstocksClient, detect_option_prefix
from .instruments import Contract, load_nifty_options


def load_contracts(client: IndstocksClient, cfg: dict, log=print) -> list[Contract]:
    contracts = load_nifty_options(client.fno_instruments_csv(), cfg["trade"]["underlying"])
    if not contracts:
        return contracts
    prefix = cfg["api"].get("option_prefix", "auto")
    if prefix == "auto":
        sample = contracts[0].scrip.split("_", 1)[1]
        prefix = detect_option_prefix(client, sample)
        log(f"Option codes use the {prefix}_ prefix.")
    if prefix != "NFO":
        contracts = [replace(c, scrip=f"{prefix}_{c.scrip.split('_', 1)[1]}") for c in contracts]
    return contracts
