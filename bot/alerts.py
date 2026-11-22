"""Loud, plain-English alerts: big terminal banner, Mac pop-up with sound and voice, and Telegram.

Every alert says exactly what to do, e.g.
    BUY NOW: NIFTY 23 NOV 25400 CE, 2 lots (130 qty), around ₹212
"""
from __future__ import annotations

import os
import platform
import subprocess

import requests


class Alerter:
    def __init__(self, cfg: dict, log=print):
        a = cfg.get("alerts", {})
        self.mac = a.get("mac_popup", True) and platform.system() == "Darwin"
        self.voice = a.get("mac_voice", True) and self.mac
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.log = log

    def send(self, title: str, body: str, speak: str | None = None) -> None:
        bar = "#" * 70
        self.log(f"\n{bar}\n##  {title}\n##  {body}\n{bar}")
        if self.mac:
            self._mac(title, body, speak)
        if self.telegram_token and self.telegram_chat:
            self._telegram(f"{title}\n{body}")

    def _mac(self, title: str, body: str, speak: str | None) -> None:
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')
        try:
            subprocess.run(["osascript", "-e",
                            f'display notification "{esc(body)}" with title "{esc(title)}" sound name "Glass"'],
                           timeout=5, check=False, capture_output=True)
            if self.voice and speak:
                subprocess.Popen(["say", speak])
        except Exception as e:  # noqa: BLE001
            self.log(f"(Mac alert failed: {e})")

    def _telegram(self, text: str) -> None:
        try:
            requests.post(f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                          data={"chat_id": self.telegram_chat, "text": text}, timeout=10)
        except Exception as e:  # noqa: BLE001
            self.log(f"(Telegram alert failed: {e})")


def contract_label(contract) -> str:
    """'NIFTY 23 NOV 25400 CE' style label, like the INDstocks screen."""
    return f"NIFTY {contract.expiry:%d %b}".upper() + f" {contract.strike:.0f} {contract.opt_type}"
