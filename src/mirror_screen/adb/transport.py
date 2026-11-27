"""Resolve a USB or TCP (``adb connect``) transport for one session.

The reference scripts enable wireless debugging with ``adb tcpip 5555`` while
the phone is on USB, discover its Wi-Fi address, and remember it. This module
does the same without hardcoding a serial, an interface, or an address.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Literal

from ..config import SessionConfig
from ..errors import AdbError, DeviceError
from .device import Adb, Device, transport_of

log = logging.getLogger(__name__)

TransportKind = Literal["auto", "usb", "tcp"]

#: ``adb tcpip`` default port, matching the reference scripts.
DEFAULT_TCP_PORT = 5555

#: How long adbd needs to restart after ``adb tcpip`` before it listens again.
_WIRELESS_RESTART_WAIT = 1.5


def is_tcp_serial(serial: str | None) -> bool:
    """True when the serial looks like ``host:port`` rather than a USB id."""
    return transport_of(serial) == "tcp"


def split_endpoint(
    endpoint: str, default_port: int = DEFAULT_TCP_PORT
) -> tuple[str, int]:
    """Split ``host[:port]``; bare hosts use ``default_port``."""
    host, sep, port = endpoint.rpartition(":")
    if not sep:
        return endpoint, default_port
    if not port.isdigit():
        raise DeviceError(f"invalid TCP endpoint: {endpoint!r} (expected host:port)")
    return host, int(port)


class TransportManager:
    """Own USB/TCP resolution and the endpoints this process connected.

    ``resolve`` is idempotent: reconnecting reuses an endpoint that is still
    listed by adb and re-runs ``adb connect`` when it disappeared.
    """

    def __init__(
        self,
        adb_path: Path | str,
        cache_dir: Path | str,
        *,
        port: int = DEFAULT_TCP_PORT,
        timeout: float = 20.0,
        restart_wait: float = _WIRELESS_RESTART_WAIT,
    ) -> None:
        self.adb_path = Path(adb_path)
        self.cache_dir = Path(cache_dir)
        self.port = port
        self.timeout = timeout
        self.restart_wait = restart_wait
        self._discovery = Adb(self.adb_path, timeout=timeout)
        self._owned: set[str] = set()
        self._endpoint_file = self.cache_dir / "transport.json"

    # -- public API ---------------------------------------------------------
    def resolve(self, config: SessionConfig) -> str:
        """Return a serial usable by :class:`Adb` for this configuration."""
        kind: TransportKind = config.transport
        explicit = config.serial

        if explicit and is_tcp_serial(explicit):
            return self.ensure_connected(explicit)
        if kind == "usb":
            return self._resolve_usb(explicit)
        if kind == "tcp":
            if explicit:
                return self.ensure_connected(self._with_port(explicit, config.tcp_port))
            if config.tcp_host:
                return self.ensure_connected(
                    self._with_port(config.tcp_host, config.tcp_port)
                )
            saved = self.load_saved()
            if saved:
                try:
                    return self.ensure_connected(saved)
                except DeviceError:
                    usb = self.pick_usb()
                    if usb is not None:
                        log.info(
                            "saved endpoint unreachable; re-enabling wireless via USB on %s",
                            usb.serial,
                        )
                        return self.enable_wireless(usb.serial, config.tcp_port)
                    raise
            return self.enable_wireless(None, config.tcp_port)

        # auto: an explicit serial wins, then USB, then a saved TCP endpoint.
        if explicit:
            return explicit
        usb = self.pick_usb()
        if usb is not None:
            return usb.serial
        saved = self.load_saved()
        if saved:
            try:
                return self.ensure_connected(saved)
            except DeviceError:
                pass
        raise DeviceError(
            "no Android device found. Connect a phone over USB with USB "
            "debugging enabled, or set up wireless with --enable-wireless."
        )

    def pick_usb(self) -> Device | None:
        """Return the first usable USB device, or ``None``."""
        for device in self._discovery.devices():
            if device.is_usable and not is_tcp_serial(device.serial):
                return device
        return None

    def enable_wireless(
        self, serial: str | None = None, port: int | None = None
    ) -> str:
        """Switch a USB device to TCP, discover its address, connect, persist."""
        port = port or self.port
        device = self._usb_device(serial)
        log.info("enabling adb over TCP on %s (port %d)", device.serial, port)
        usb = Adb(self.adb_path, device.serial, timeout=self.timeout)
        usb.tcpip(port)
        # adbd restarts; the USB transport may blink out for a moment.
        time.sleep(self.restart_wait)
        address = usb.wlan_ipv4()
        if address is None:
            raise DeviceError(
                "could not find the phone's Wi-Fi address. Make sure Wi-Fi is "
                "on, then retry while the phone is still connected over USB."
            )
        return self.ensure_connected(f"{address}:{port}")

    def ensure_connected(self, endpoint: str) -> str:
        """Connect ``host:port`` if needed and return it as the serial."""
        host, port = split_endpoint(endpoint, self.port)
        endpoint = f"{host}:{port}"
        if endpoint in self._owned and self._is_listed(endpoint):
            return endpoint

        output = Adb(self.adb_path, timeout=self.timeout).connect(endpoint)
        if not self._is_listed(endpoint):
            raise DeviceError(
                f"could not connect to {endpoint}: {output or 'no response'}. "
                "Connect the phone over USB once and run --enable-wireless."
            )
        self._owned.add(endpoint)
        self.save_endpoint(endpoint)
        log.info("connected over TCP: %s", endpoint)
        return endpoint

    def disconnect(self, endpoint: str | None = None) -> None:
        """Disconnect one endpoint (or every endpoint this process owns)."""
        targets = {endpoint} if endpoint else set(self._owned)
        self._owned -= targets
        for target in targets:
            try:
                Adb(self.adb_path, timeout=10.0).disconnect(target)
            except AdbError as exc:  # pragma: no cover - best effort
                log.debug("could not disconnect %s: %s", target, exc)

    def release(self) -> None:
        """Disconnect the endpoints this process connected (idempotent)."""
        self.disconnect(None)

    def forget(self) -> None:
        """Disconnect and delete the remembered endpoint."""
        self.release()
        with contextlib.suppress(OSError):
            self._endpoint_file.unlink()

    # -- persistence --------------------------------------------------------
    def load_saved(self) -> str | None:
        try:
            data = json.loads(self._endpoint_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        endpoint = data.get("endpoint") if isinstance(data, dict) else None
        return endpoint if is_tcp_serial(endpoint) else None

    def save_endpoint(self, endpoint: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._endpoint_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"endpoint": endpoint}), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        temporary.replace(self._endpoint_file)

    # -- helpers ------------------------------------------------------------
    def _resolve_usb(self, serial: str | None) -> str:
        if serial:
            device = self._find(serial)
            if device is None:
                raise DeviceError(f"device {serial} is not connected")
            if not device.is_usable:
                raise DeviceError(
                    f"device {serial} is not ready (state: {device.state}). "
                    "Unlock the phone and accept the USB debugging prompt."
                )
            return serial
        device = self.pick_usb()
        if device is None:
            raise DeviceError(
                "no USB device found. Check the cable and that USB debugging "
                "is enabled, or use --transport tcp."
            )
        return device.serial

    def _usb_device(self, serial: str | None) -> Device:
        device = self._find(serial) if serial else self.pick_usb()
        if device is None or not device.is_usable:
            raise DeviceError(
                "wireless setup needs the phone on USB once. Connect the cable "
                "and accept the USB debugging prompt, then retry."
            )
        if is_tcp_serial(device.serial):
            raise DeviceError(
                "wireless setup needs a USB connection; the selected device is "
                "already a TCP endpoint."
            )
        return device

    def _find(self, serial: str) -> Device | None:
        for device in self._discovery.devices():
            if device.serial == serial:
                return device
        return None

    def _is_listed(self, endpoint: str) -> bool:
        return any(
            device.serial == endpoint and device.is_usable
            for device in self._discovery.devices()
        )

    def _with_port(self, host: str, port: int) -> str:
        return host if is_tcp_serial(host) else f"{host}:{port}"


__all__ = [
    "DEFAULT_TCP_PORT",
    "TransportKind",
    "TransportManager",
    "is_tcp_serial",
    "split_endpoint",
]
