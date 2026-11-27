"""Android platform-tools integration."""

from __future__ import annotations

from .device import Adb, Device, first_ipv4, transport_of
from .tools import cache_root, ensure_adb, find_adb, install_platform_tools
from .transport import (
    DEFAULT_TCP_PORT,
    TransportManager,
    is_tcp_serial,
    split_endpoint,
)

__all__ = [
    "DEFAULT_TCP_PORT",
    "Adb",
    "Device",
    "TransportManager",
    "cache_root",
    "ensure_adb",
    "find_adb",
    "first_ipv4",
    "install_platform_tools",
    "is_tcp_serial",
    "split_endpoint",
    "transport_of",
]
