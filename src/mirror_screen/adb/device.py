"""Thin wrapper around the ``adb`` command line."""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import AdbError, DeviceError


@dataclass(frozen=True, slots=True)
class Device:
    """One entry from ``adb devices -l``."""

    serial: str
    state: str
    model: str | None = None
    product: str | None = None
    device: str | None = None
    transport_id: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.state == "device"

    @property
    def display_name(self) -> str:
        label = self.model or self.product or self.device or self.serial
        return label.replace("_", " ")

    def __str__(self) -> str:
        return f"{self.serial} ({self.display_name}) [{self.state}]"


#: Attributes in ``adb devices -l`` output, such as ``model:Pixel_7``.
_DEVICE_ATTR_RE = re.compile(r"(\w+):(\S+)")

#: A TCP endpoint serial such as ``192.168.1.7:5555``.
_TCP_SERIAL_RE = re.compile(r"^[A-Za-z0-9._\-]+:\d{1,5}$")

#: A dotted-quad IPv4 address.
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def transport_of(serial: str | None) -> str:
    """Return ``"tcp"`` for ``host:port`` serials, else ``"usb"``.

    ``adb`` reports a wireless device by its endpoint, so the serial is the
    only signal needed; there is no extra round trip.
    """
    if serial and _TCP_SERIAL_RE.match(serial):
        return "tcp"
    return "usb"


def first_ipv4(text: str) -> str | None:
    """Return the first usable IPv4 address in ``text``, or ``None``.

    Loopback and the unspecified address are skipped: the phone's own Wi-Fi
    address is what ``adb connect`` needs.
    """
    for match in _IPV4_RE.finditer(text or ""):
        candidate = match.group(1)
        parts = candidate.split(".")
        if len(parts) != 4:
            continue
        try:
            values = [int(part) for part in parts]
        except ValueError:
            continue
        if not all(0 <= value <= 255 for value in values):
            continue
        if candidate.startswith("127.") or candidate == "0.0.0.0":
            continue
        return candidate
    return None


class Adb:
    """Run adb commands against one device (or against the adb server)."""

    def __init__(
        self,
        adb_path: Path | str,
        serial: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.adb_path = Path(adb_path)
        self.serial = serial
        self.timeout = timeout

    # -- process plumbing ---------------------------------------------------
    def _base(self) -> list[str]:
        cmd = [str(self.adb_path)]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Run ``adb <args...>`` and return the completed process."""
        cmd = self._base() + list(args)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb timed out: {' '.join(cmd)}") from exc
        except OSError as exc:
            raise AdbError(f"could not run adb: {exc}") from exc

        if check and result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise AdbError(
                f"adb failed ({result.returncode}): {' '.join(args)}\n{stderr}"
            )
        return result

    def popen(self, *args: str) -> subprocess.Popen[str]:
        """Start an adb command, streaming its combined output."""
        cmd = self._base() + list(args)
        try:
            return subprocess.Popen(
                cmd,
                # Never inherit our stdin: a closed or EOF stdin can make the
                # remote shell tear the command down.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise AdbError(f"could not run adb: {exc}") from exc

    # -- device discovery ---------------------------------------------------
    def devices(self) -> list[Device]:
        result = self.run("devices", "-l")
        devices: list[Device] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            attrs = dict(_DEVICE_ATTR_RE.findall(line))
            devices.append(
                Device(
                    serial=parts[0],
                    state=parts[1],
                    model=attrs.get("model"),
                    product=attrs.get("product"),
                    device=attrs.get("device"),
                    transport_id=attrs.get("transport_id"),
                )
            )
        return devices

    def require_device(self) -> Device:
        """Return the single usable device, or raise with a helpful message."""
        devices = self.devices()
        usable = [d for d in devices if d.is_usable]

        if self.serial:
            for device in devices:
                if device.serial == self.serial:
                    if device.is_usable:
                        return device
                    raise DeviceError(
                        f"device {self.serial} is not ready (state: {device.state}). "
                        "Unlock the phone and accept the USB debugging prompt."
                    )
            raise DeviceError(f"device {self.serial} is not connected")

        if not usable:
            if any(d.state == "unauthorized" for d in devices):
                raise DeviceError(
                    "device is unauthorized: unlock the phone and accept the "
                    "'Allow USB debugging' prompt"
                )
            raise DeviceError(
                "no Android device found. Connect a phone over USB with USB "
                "debugging enabled, then run 'mirror-screen devices'."
            )

        if len(usable) > 1:
            listed = "\n".join(f"  - {d.serial} ({d.display_name})" for d in usable)
            raise DeviceError(
                "several devices are connected; choose one with --serial:\n" + listed
            )
        return usable[0]

    # -- device operations --------------------------------------------------
    def getprop(self, name: str) -> str:
        result = self.run("shell", "getprop", name, check=False)
        return (result.stdout or "").strip()

    def shell(self, command: str, *, check: bool = True, timeout: float | None = None):
        return self.run("shell", command, check=check, timeout=timeout)

    def push(self, local: Path | str, remote: str) -> None:
        self.run("push", str(local), remote, timeout=120.0)

    def forward(self, local_port: int, remote: str) -> int:
        """Forward a local TCP port to a device-side socket; return the port."""
        result = self.run("forward", f"tcp:{local_port}", remote)
        output = (result.stdout or "").strip()
        # adb prints the allocated port, which may differ when :0 was requested.
        if output.isdigit():
            return int(output)
        return local_port

    def forward_remove(self, local_port: int) -> None:
        self.run("forward", "--remove", f"tcp:{local_port}", check=False, timeout=10.0)

    def reverse(self, remote: str, local: str) -> None:
        """Map a device-side address onto a host-side one.

        The device connects to ``remote``; adb tunnels it to ``local``.
        """
        self.run("reverse", remote, local)

    def reverse_remove(self, remote: str) -> None:
        self.run("reverse", "--remove", remote, check=False, timeout=10.0)

    def wait_for_device(self, timeout: float = 30.0) -> None:
        self.run("wait-for-device", timeout=timeout)

    # -- wireless transport -------------------------------------------------
    def tcpip(self, port: int = 5555) -> None:
        """Restart adbd on the device so it also listens on ``port``."""
        self.run("tcpip", str(port), timeout=self.timeout)

    def connect(self, endpoint: str, *, timeout: float = 20.0) -> str:
        """``adb connect host:port``; return adb's combined output."""
        result = self.run("connect", endpoint, check=False, timeout=timeout)
        return f"{result.stdout or ''}\n{result.stderr or ''}".strip()

    def disconnect(self, endpoint: str) -> None:
        """Drop a TCP endpoint; missing endpoints are not an error."""
        self.run("disconnect", endpoint, check=False, timeout=10.0)

    def wlan_ipv4(self) -> str | None:
        """Return the phone's Wi-Fi address without assuming an interface."""
        commands = (
            ("shell", "ip", "-f", "inet", "addr", "show", "wlan0"),
            ("shell", "ip", "route", "get", "1.1.1.1"),
            ("shell", "getprop", "dhcp.wlan0.ipaddress"),
        )
        for command in commands:
            result = self.run(*command, check=False)
            address = first_ipv4((result.stdout or "") + (result.stderr or ""))
            if address:
                return address
        return None

    @staticmethod
    def build_shell_command(program: str, *args: str) -> str:
        """Quote a device-side command line for ``adb shell``."""
        return " ".join([program, *(shlex.quote(arg) for arg in args)])
