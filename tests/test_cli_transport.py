"""CLI transport flags fold into one unambiguous session config."""

from __future__ import annotations

import pytest

from mirror_screen.cli import _config_from_args, build_parser
from mirror_screen.errors import MirrorScreenError


def _config(*argv: str):
    args = build_parser().parse_args(list(argv))
    return _config_from_args(args)


def test_transport_defaults_to_auto():
    config = _config("run")
    assert config.transport == "auto"
    assert config.tcp_host is None
    assert config.tcp_port == 5555


def test_tcp_endpoint_implies_tcp_transport():
    config = _config("run", "--tcp", "192.168.1.7:6000")
    assert config.transport == "tcp"
    assert config.tcp_host == "192.168.1.7"
    assert config.tcp_port == 6000


def test_tcp_without_port_uses_5555():
    config = _config("probe", "--tcp", "192.168.1.7")
    assert (config.transport, config.tcp_host, config.tcp_port) == (
        "tcp",
        "192.168.1.7",
        5555,
    )


def test_enable_wireless_selects_tcp_without_an_address():
    config = _config("run", "--enable-wireless")
    assert config.transport == "tcp"
    assert config.tcp_host is None


def test_explicit_usb_transport_is_kept():
    config = _config("run", "--transport", "usb")
    assert config.transport == "usb"


def test_tcp_and_usb_transport_conflict():
    with pytest.raises(MirrorScreenError):
        _config("run", "--transport", "usb", "--tcp", "192.168.1.7")


def test_enable_wireless_and_usb_transport_conflict():
    with pytest.raises(MirrorScreenError):
        _config("run", "--transport", "usb", "--enable-wireless")


def test_transport_flags_reach_every_session_command():
    for command in ("run", "ui", "probe"):
        config = _config(command, "--transport", "tcp", "--tcp", "10.0.0.9:5555")
        assert config.transport == "tcp", command
        assert config.tcp_host == "10.0.0.9", command


def test_devices_accepts_the_transport_flags():
    args = build_parser().parse_args(
        ["devices", "--transport", "tcp", "--tcp", "10.0.0.9:5555"]
    )
    assert (args.transport, args.tcp) == ("tcp", "10.0.0.9:5555")


def test_invalid_transport_port_is_rejected():
    with pytest.raises(MirrorScreenError):
        _config("run", "--tcp", "10.0.0.9:not-a-port")
