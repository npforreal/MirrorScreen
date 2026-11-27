"""Transport resolution against a fake ``adb`` executable."""

from __future__ import annotations

import json
import stat

import pytest

from mirror_screen.adb.device import first_ipv4, transport_of
from mirror_screen.adb.transport import TransportManager, split_endpoint
from mirror_screen.config import SessionConfig
from mirror_screen.errors import DeviceError

_FAKE_ADB = '''#!/usr/bin/env python3
"""Minimal adb stand-in: records calls and replays a canned device list."""
import json
import os
import sys
from pathlib import Path

state_file = Path(os.environ["FAKE_ADB_STATE"])
state = json.loads(state_file.read_text()) if state_file.exists() else {}
state.setdefault("devices", [])
state.setdefault("log", [])

args = sys.argv[1:]
serial = None
if args[:1] == ["-s"]:
    serial = args[1]
    args = args[2:]
state["log"].append({"serial": serial, "args": args})

command = args[0] if args else ""
if command == "devices":
    print("List of devices attached")
    for device in state["devices"]:
        print(f"{device['serial']}\\t{device['state']} model:{device.get('model', 'SM_A176B')}")
elif command == "tcpip":
    state["tcpip"] = args[1]
elif command == "connect":
    endpoint = args[1]
    if state.get("connect_fails"):
        print(f"failed to connect to '{endpoint}'")
        sys.exit(1)
    if not any(device["serial"] == endpoint for device in state["devices"]):
        state["devices"].append({"serial": endpoint, "state": "device"})
    print(f"connected to {endpoint}")
elif command == "disconnect":
    endpoint = args[1]
    state["devices"] = [
        device for device in state["devices"] if device["serial"] != endpoint
    ]
    print(f"disconnected {endpoint}")
elif command == "shell":
    print(state.get("shell_output", ""))

state_file.write_text(json.dumps(state))
sys.exit(0)
'''


@pytest.fixture
def fake_adb(tmp_path, monkeypatch):
    """A fake adb plus helpers to read back its state."""
    script = tmp_path / "adb"
    script.write_text(_FAKE_ADB, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    state_path = tmp_path / "adb-state.json"
    monkeypatch.setenv("FAKE_ADB_STATE", str(state_path))

    class Fake:
        path = script
        state_file = state_path

        def set_state(self, **values):
            state = self.read()
            state.update(values)
            self.write(state)

        def read(self):
            if not self.state_file.exists():
                return {"devices": [], "log": []}
            return json.loads(self.state_file.read_text())

        def write(self, state):
            self.state_file.write_text(json.dumps(state))

        def calls(self, command):
            return [
                call for call in self.read()["log"] if call["args"][:1] == [command]
            ]

    return Fake()


def _usb_device(fake, serial="R5GL74L3WHX"):
    fake.set_state(devices=[{"serial": serial, "state": "device", "model": "SM_A176B"}])
    return serial


def test_transport_of_distinguishes_usb_and_tcp():
    assert transport_of("R5GL74L3WHX") == "usb"
    assert transport_of("192.168.1.7:5555") == "tcp"
    assert transport_of("[::1]:5555") == "usb"  # not an IPv4 endpoint
    assert transport_of(None) == "usb"


def test_first_ipv4_skips_loopback_and_junk():
    text = "inet 127.0.0.1/8 scope host lo\ninet 192.168.1.42/24 scope global wlan0"
    assert first_ipv4(text) == "192.168.1.42"
    assert first_ipv4("inet 0.0.0.0/0") is None
    assert first_ipv4("inet 999.1.1.1/8") is None
    assert first_ipv4("") is None


def test_split_endpoint_defaults_and_rejects_bad_port():
    assert split_endpoint("192.168.1.7") == ("192.168.1.7", 5555)
    assert split_endpoint("192.168.1.7:6000") == ("192.168.1.7", 6000)
    with pytest.raises(DeviceError):
        split_endpoint("192.168.1.7:http")


def test_auto_transport_prefers_usb(fake_adb, tmp_path):
    serial = _usb_device(fake_adb)
    manager = TransportManager(fake_adb.path, tmp_path / "cache")

    assert manager.resolve(SessionConfig()) == serial
    assert fake_adb.calls("connect") == []


def test_auto_transport_without_any_device_explains_the_options(fake_adb, tmp_path):
    manager = TransportManager(fake_adb.path, tmp_path / "cache")

    with pytest.raises(DeviceError) as caught:
        manager.resolve(SessionConfig())

    message = str(caught.value)
    assert "USB" in message
    assert "--enable-wireless" in message


def test_explicit_tcp_host_connects_and_is_remembered(fake_adb, tmp_path):
    cache = tmp_path / "cache"
    manager = TransportManager(fake_adb.path, cache)

    serial = manager.resolve(SessionConfig(transport="tcp", tcp_host="10.0.0.5"))

    assert serial == "10.0.0.5:5555"
    assert fake_adb.calls("connect")[0]["args"] == ["connect", "10.0.0.5:5555"]

    saved = json.loads((cache / "transport.json").read_text())
    assert saved == {"endpoint": "10.0.0.5:5555"}
    mode = (cache / "transport.json").stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_saved_endpoint_is_reused_on_the_next_run(fake_adb, tmp_path):
    cache = tmp_path / "cache"
    _usb_device(fake_adb, serial="192.168.1.20:5555")
    cache.mkdir()
    (cache / "transport.json").write_text(json.dumps({"endpoint": "192.168.1.20:5555"}))

    manager = TransportManager(fake_adb.path, cache)

    assert manager.resolve(SessionConfig()) == "192.168.1.20:5555"
    assert manager.resolve(SessionConfig()) == "192.168.1.20:5555"
    # Re-connecting an already-listed endpoint from a previous run once is fine;
    # repeating in the same process must not shell out again.
    assert len(fake_adb.calls("connect")) == 1


def test_usb_wins_over_a_saved_endpoint_in_auto_mode(fake_adb, tmp_path):
    serial = _usb_device(fake_adb)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "transport.json").write_text(json.dumps({"endpoint": "192.168.1.20:5555"}))

    manager = TransportManager(fake_adb.path, cache)

    assert manager.resolve(SessionConfig()) == serial
    assert fake_adb.calls("connect") == []


def test_enable_wireless_runs_tcpip_discovers_and_connects(fake_adb, tmp_path):
    serial = _usb_device(fake_adb)
    fake_adb.set_state(shell_output="4: wlan0    inet 192.168.1.42/24 brd scope global")
    cache = tmp_path / "cache"
    manager = TransportManager(fake_adb.path, cache, restart_wait=0)

    endpoint = manager.resolve(SessionConfig(transport="tcp"))

    assert endpoint == "192.168.1.42:5555"
    tcpip = fake_adb.calls("tcpip")[0]
    assert tcpip["serial"] == serial
    assert tcpip["args"] == ["tcpip", "5555"]
    assert json.loads((cache / "transport.json").read_text()) == {
        "endpoint": "192.168.1.42:5555"
    }


def test_enable_wireless_without_wifi_reports_the_missing_step(fake_adb, tmp_path):
    _usb_device(fake_adb)
    fake_adb.set_state(shell_output="")
    manager = TransportManager(fake_adb.path, tmp_path / "cache", restart_wait=0)

    with pytest.raises(DeviceError) as caught:
        manager.resolve(SessionConfig(transport="tcp"))

    assert "Wi-Fi" in str(caught.value)


def test_enable_wireless_needs_usb(fake_adb, tmp_path):
    manager = TransportManager(fake_adb.path, tmp_path / "cache", restart_wait=0)

    with pytest.raises(DeviceError) as caught:
        manager.enable_wireless()

    assert "USB" in str(caught.value)


def test_failed_connect_points_at_the_usb_setup(fake_adb, tmp_path):
    fake_adb.set_state(connect_fails=True)
    manager = TransportManager(fake_adb.path, tmp_path / "cache")

    with pytest.raises(DeviceError) as caught:
        manager.ensure_connected("10.0.0.9:5555")

    message = str(caught.value)
    assert "10.0.0.9:5555" in message
    assert "--enable-wireless" in message


def test_release_is_idempotent_and_only_drops_owned_endpoints(fake_adb, tmp_path):
    _usb_device(fake_adb, serial="192.168.1.20:5555")
    fake_adb.set_state(devices=[{"serial": "192.168.1.20:5555", "state": "device"}])
    manager = TransportManager(fake_adb.path, tmp_path / "cache")

    manager.ensure_connected("192.168.1.20:5555")
    manager.release()
    manager.release()

    disconnects = fake_adb.calls("disconnect")
    assert [call["args"] for call in disconnects] == [
        ["disconnect", "192.168.1.20:5555"]
    ]


def test_forget_removes_the_remembered_endpoint(fake_adb, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "transport.json").write_text(json.dumps({"endpoint": "10.0.0.7:5555"}))
    manager = TransportManager(fake_adb.path, cache)

    manager.forget()
    manager.forget()  # deleting twice must not raise

    assert not (cache / "transport.json").exists()


def test_connecting_the_same_endpoint_twice_reuses_it(fake_adb, tmp_path):
    manager = TransportManager(fake_adb.path, tmp_path / "cache")

    assert manager.ensure_connected("10.0.0.5:5555") == "10.0.0.5:5555"
    assert manager.ensure_connected("10.0.0.5:5555") == "10.0.0.5:5555"

    assert len(fake_adb.calls("connect")) == 1
