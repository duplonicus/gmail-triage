"""Minimal sd_notify(3): READY/WATCHDOG/STATUS over $NOTIFY_SOCKET. No-op outside systemd."""
from __future__ import annotations

import os
import socket


def notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.connect(addr)
        s.sendall(msg.encode())


def ready(status: str = "") -> None:
    notify("READY=1" + (f"\nSTATUS={status}" if status else ""))


def watchdog() -> None:
    notify("WATCHDOG=1")


def status(text: str) -> None:
    notify(f"STATUS={text}")
