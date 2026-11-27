"""Command line interface for Mirror Screen."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .adb import (
    DEFAULT_TCP_PORT,
    Adb,
    TransportManager,
    cache_root,
    ensure_adb,
    split_endpoint,
    transport_of,
)
from .config import (
    DEFAULT_AUDIO_BIT_RATE,
    DEFAULT_VIDEO_BIT_RATE,
    STREAM_PROFILES,
    SessionConfig,
)
from .errors import MirrorScreenError
from .protocol.const import AUDIO_CODECS, VIDEO_CODECS
from .scrcpy import SCRCPY_VERSION, ensure_server_jar

LOG_LEVELS = ("verbose", "debug", "info", "warn", "error")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mirror-screen",
        description="Mirror an Android screen to this computer, with high "
        "resolution and low latency.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="print available stream profiles with their default settings",
    )
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="start mirroring (default)")
    _add_run_options(run)
    run.set_defaults(func=_cmd_run)

    devices = subparsers.add_parser("devices", help="list connected Android devices")
    _add_common_options(devices)
    devices.set_defaults(func=_cmd_devices)

    setup = subparsers.add_parser("setup", help="download adb and the on-device server")
    _add_common_options(setup)
    setup.add_argument("--force", action="store_true", help="re-download the server")
    setup.set_defaults(func=_cmd_setup)

    ui = subparsers.add_parser(
        "ui",
        help="open the setup window (same as double-clicking the launcher)",
    )
    _add_common_options(ui)
    ui.add_argument(
        "--link-quality",
        choices=sorted(STREAM_PROFILES),
        default=None,
        help="stream profile preset (codec, rates, resolution, buffer); "
        "default follows the connection type",
    )
    ui.add_argument("--max-size", type=int, default=0, help="cap the largest dimension")
    ui.add_argument(
        "--bit-rate", type=int, default=DEFAULT_VIDEO_BIT_RATE, help="target bit rate"
    )
    ui.add_argument(
        "--max-fps", type=float, default=0.0, help="frame-rate cap; 0 is uncapped"
    )
    ui.add_argument("--no-vsync", action="store_true", help="disable vsync")
    ui.add_argument(
        "--render-thread", action="store_true", help="prefer a dedicated render thread"
    )
    ui.set_defaults(func=_cmd_ui)

    probe = subparsers.add_parser(
        "probe",
        help="decode from a real device for a few seconds and report",
    )
    _add_common_options(probe)
    _add_video_options(probe)
    probe.add_argument(
        "--duration",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="how long to decode for",
    )
    probe.add_argument(
        "--screenshot",
        type=Path,
        default=None,
        help="render the last decoded frame to this PNG",
    )
    probe.add_argument(
        "--no-check-control",
        action="store_true",
        help="skip the control-socket round trip check",
    )
    probe.set_defaults(func=_cmd_probe)

    selftest = subparsers.add_parser(
        "selftest",
        help="verify demuxing, decoding and rendering without a device",
    )
    selftest.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="where to write temporary clips (default: the cache directory)",
    )
    selftest.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write a rendered PNG of the test pattern here",
    )
    selftest.set_defaults(func=_cmd_selftest)

    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--serial", "-s", help="device serial (needed if several)")
    parser.add_argument("--adb", help="path to an adb binary or platform-tools dir")
    parser.add_argument("--cache-dir", type=Path, help="override the cache location")
    transport = parser.add_argument_group("transport")
    transport.add_argument(
        "--transport",
        choices=["auto", "usb", "tcp"],
        default="auto",
        help="how to reach the device: auto (serial, then USB, then saved "
        "wireless), usb, or tcp",
    )
    transport.add_argument(
        "--tcp",
        metavar="HOST[:PORT]",
        default=None,
        help="connect wirelessly to HOST (port defaults to 5555)",
    )
    transport.add_argument(
        "--enable-wireless",
        action="store_true",
        help="switch the USB-attached phone to adb over TCP, remember its "
        "address, then mirror wirelessly",
    )
    transport.add_argument(
        "--disconnect-wireless",
        action="store_true",
        help="disconnect and forget the remembered wireless endpoint before starting",
    )


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    _add_common_options(parser)
    _add_video_options(parser)
    _add_window_options(parser)


def _add_video_options(parser: argparse.ArgumentParser) -> None:
    """Options that are forwarded to the on-device server."""
    video = parser.add_argument_group("video (forwarded to the device)")
    video.add_argument(
        "--max-size",
        "-m",
        type=int,
        default=0,
        metavar="PX",
        help="cap the largest dimension; 0 keeps the native resolution",
    )
    video.add_argument(
        "--max-fps",
        type=float,
        default=0.0,
        metavar="FPS",
        help="cap the frame rate; 0 leaves it uncapped",
    )
    video.add_argument(
        "--bit-rate",
        "-b",
        type=int,
        default=DEFAULT_VIDEO_BIT_RATE,
        metavar="BPS",
        help="target bit rate in bits per second",
    )
    video.add_argument(
        "--codec",
        choices=sorted(VIDEO_CODECS),
        default="h264",
        help="video codec; h264 is the most compatible and the fastest to decode",
    )
    video.add_argument(
        "--codec-option",
        action="append",
        default=[],
        metavar="KEY:VALUE",
        help="extra MediaCodec option, repeatable (for example "
        "profile:1,i-frame-interval:10)",
    )
    video.add_argument("--audio", action="store_true", help="play phone audio locally")
    playback = video.add_mutually_exclusive_group()
    playback.add_argument(
        "--keep-phone-audio",
        dest="phone_playback",
        action="store_const",
        const="keep",
        help="keep playing audio on the phone while forwarding it (default)",
    )
    playback.add_argument(
        "--mute-phone-audio",
        dest="phone_playback",
        action="store_const",
        const="mute",
        help="mute phone playback while forwarding audio",
    )
    video.add_argument(
        "--audio-output",
        metavar="ID",
        help="local audio output device id; default uses the system output",
    )
    video.add_argument(
        "--audio-bit-rate",
        type=int,
        default=DEFAULT_AUDIO_BIT_RATE,
        metavar="BPS",
        help="audio bitrate forwarded to the device encoder",
    )
    video.add_argument(
        "--audio-codec",
        choices=sorted(AUDIO_CODECS),
        default="opus",
        help="audio codec negotiated with the device",
    )
    video.add_argument(
        "--video-buffer",
        type=int,
        default=0,
        metavar="MS",
        help="client-side jitter buffer in milliseconds; 0 keeps the lowest "
        "latency, 300 matches the measured smooth USB profile",
    )
    video.add_argument(
        "--list-profiles",
        action="store_true",
        help="print available stream profiles with their default settings",
    )

    device = parser.add_argument_group("device behaviour")
    device.add_argument("--show-touches", action="store_true", help="draw touch points")
    device.add_argument(
        "--no-control",
        action="store_true",
        help="do not send input events to the device",
    )
    device.add_argument(
        "--no-stay-awake",
        action="store_true",
        help="let the device screen sleep while mirroring",
    )
    device.add_argument(
        "--no-power-on",
        action="store_true",
        help="do not wake the device when the session starts",
    )
    device.add_argument(
        "--keep-active",
        action="store_true",
        help="keep the device awake without changing global settings",
    )
    device.add_argument(
        "--force-adb-forward",
        action="store_true",
        help="connect out to the device (adb forward) instead of the default "
        "reverse tunnel; only needed where 'adb reverse' is unavailable",
    )
    device.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default="info",
        help="verbosity of the on-device server",
    )


def _add_window_options(parser: argparse.ArgumentParser) -> None:
    """Options that only affect the client-side window."""
    window = parser.add_argument_group("window")
    window.add_argument(
        "--fullscreen", "-f", action="store_true", help="start fullscreen"
    )
    window.add_argument(
        "--always-on-top", action="store_true", help="keep the window above others"
    )
    window.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="extra zoom on top of the fit-to-window size (try 2.0)",
    )
    window.add_argument(
        "--integer-scale",
        action="store_true",
        help="prefer whole-number scaling to keep pixels crisp",
    )
    window.add_argument(
        "--nearest",
        action="store_true",
        help="use nearest-neighbour filtering instead of linear",
    )
    window.add_argument(
        "--no-vsync",
        action="store_true",
        help="do not wait for the display refresh (lower latency, may tear)",
    )
    window.add_argument(
        "--no-stats", action="store_true", help="hide the live statistics"
    )
    window.add_argument(
        "--no-auto-resize",
        action="store_true",
        help="do not reshape the window when the device rotates",
    )
    window.add_argument(
        "--no-reconnect",
        action="store_true",
        help="stop instead of rebuilding the session when the stream drops",
    )
    window.add_argument(
        "--trace",
        action="store_true",
        help="log live statistics every 2s (useful when diagnosing stutter)",
    )
    clean_grp = window.add_mutually_exclusive_group()
    clean_grp.add_argument(
        "--clean-window",
        dest="clean_window",
        action="store_true",
        default=None,
        help="frameless OBS clean-capture window (no window chrome, 100%% video quad)",
    )
    clean_grp.add_argument(
        "--no-clean-window",
        dest="clean_window",
        action="store_false",
        help="standard decorated window with OS title bar (default)",
    )
    aspect_grp = window.add_mutually_exclusive_group()
    aspect_grp.add_argument(
        "--lock-aspect-ratio",
        dest="lock_aspect_ratio",
        action="store_true",
        default=None,
        help="lock window aspect ratio to match video geometry (default)",
    )
    aspect_grp.add_argument(
        "--no-lock-aspect-ratio",
        dest="lock_aspect_ratio",
        action="store_false",
        help="allow free window resizing without aspect ratio snap",
    )
    window.add_argument(
        "--color-matrix",
        choices=["auto", "bt601", "bt709"],
        default="auto",
        help="override the YUV colour matrix",
    )
    window.add_argument(
        "--color-range",
        choices=["auto", "limited", "full"],
        default="auto",
        help="override the YUV colour range",
    )
    window.add_argument(
        "--scroll-scale",
        type=float,
        default=1.0,
        help="scroll amount per wheel notch; use a negative value to flip it",
    )
    window.add_argument(
        "--render-thread",
        action="store_true",
        help="prefer a dedicated rendering thread when available",
    )


def _config_from_args(args: argparse.Namespace) -> SessionConfig:
    codec_options: list[tuple[str, str]] = []
    for item in getattr(args, "codec_option", []) or []:
        if ":" not in item:
            raise MirrorScreenError(
                f"invalid --codec-option {item!r}; expected KEY:VALUE"
            )
        key, _, value = item.partition(":")
        codec_options.append((key.strip(), value.strip()))

    transport, tcp_host, tcp_port = _transport_from_args(args)

    config = SessionConfig(
        serial=args.serial,
        adb_path=args.adb,
        cache_dir=args.cache_dir,
        transport=transport,
        tcp_host=tcp_host,
        tcp_port=tcp_port,
        max_size=args.max_size,
        max_fps=args.max_fps,
        video_bit_rate=args.bit_rate,
        video_codec=getattr(args, "codec", "h264"),
        video_codec_options=codec_options,
        audio=getattr(args, "audio", False),
        audio_bit_rate=getattr(args, "audio_bit_rate", DEFAULT_AUDIO_BIT_RATE),
        audio_codec=getattr(args, "audio_codec", "opus"),
        phone_playback=getattr(args, "phone_playback", None) or "keep",
        audio_output=getattr(args, "audio_output", None),
        video_buffer_ms=getattr(args, "video_buffer", 0),
        control=not getattr(args, "no_control", False),
        show_touches=getattr(args, "show_touches", False),
        stay_awake=not getattr(args, "no_stay_awake", False),
        power_on=not getattr(args, "no_power_on", False),
        keep_active=getattr(args, "keep_active", False),
        log_level=getattr(args, "log_level", "info"),
        force_adb_forward=getattr(args, "force_adb_forward", False),
        fullscreen=getattr(args, "fullscreen", False),
        always_on_top=getattr(args, "always_on_top", False),
        scale=getattr(args, "scale", 1.0),
        integer_scale=getattr(args, "integer_scale", False),
        filter_mode="nearest" if getattr(args, "nearest", False) else "linear",
        render_thread=getattr(args, "render_thread", False),
        link_quality=getattr(args, "link_quality", None),
        vsync=not getattr(args, "no_vsync", False),
        show_stats=not getattr(args, "no_stats", False),
        auto_resize_window=not getattr(args, "no_auto_resize", False),
        auto_reconnect=not getattr(args, "no_reconnect", False),
        trace=getattr(args, "trace", False),
        color_matrix=getattr(args, "color_matrix", "auto"),
        color_range=getattr(args, "color_range", "auto"),
        scroll_scale=getattr(args, "scroll_scale", 1.0),
        clean_window=bool(getattr(args, "clean_window", False)),
        lock_aspect_ratio=True
        if getattr(args, "lock_aspect_ratio", None) is None
        else bool(getattr(args, "lock_aspect_ratio", True)),
    )
    link_quality = getattr(args, "link_quality", None)
    if getattr(args, "command", None) == "ui" and link_quality:
        config.apply_link_quality()
    return config


def _transport_from_args(args: argparse.Namespace) -> tuple[str, str | None, int]:
    """Fold ``--transport``, ``--tcp`` and ``--enable-wireless`` into one choice."""
    transport = getattr(args, "transport", "auto") or "auto"
    tcp_host: str | None = None
    tcp_port = DEFAULT_TCP_PORT
    endpoint = getattr(args, "tcp", None)
    if endpoint:
        if transport == "usb":
            raise MirrorScreenError(
                "--tcp needs a wireless transport, not --transport usb"
            )
        transport = "tcp"
        tcp_host, tcp_port = split_endpoint(endpoint)
    if getattr(args, "enable_wireless", False):
        if transport == "usb":
            raise MirrorScreenError(
                "--enable-wireless needs a USB setup step; it conflicts with "
                "--transport usb"
            )
        transport = "tcp"
    return transport, tcp_host, tcp_port


def _transport_manager(args: argparse.Namespace):
    """Build the transport manager the one-shot commands need."""
    adb_path = ensure_adb(
        args.adb, progress=lambda message: print(message, file=sys.stderr)
    )
    cache = args.cache_dir or cache_root()
    _, _, port = _transport_from_args(args)
    return TransportManager(adb_path, cache, port=port)


def _cmd_run(args: argparse.Namespace) -> int:
    from .app import run

    config = _config_from_args(args)
    config.validate()
    return run(config, progress=lambda message: print(message, file=sys.stderr))


def _cmd_ui(args: argparse.Namespace) -> int:
    from .app import run

    # The main window is the settings screen; there is no separate dialog.
    config = _config_from_args(args)
    config.validate()
    return run(config, progress=lambda message: print(message, file=sys.stderr))


def _cmd_devices(args: argparse.Namespace) -> int:
    manager = _transport_manager(args)

    if args.disconnect_wireless:
        endpoint = manager.load_saved()
        manager.forget()
        print(f"forgot wireless endpoint: {endpoint or 'none remembered'}")
        return 0

    if args.enable_wireless:
        try:
            endpoint = manager.enable_wireless(args.serial, manager.port)
        except MirrorScreenError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"wireless ready: {endpoint} (saved; unplug the USB cable)")
        return 0

    devices = Adb(manager.adb_path).devices()
    if not devices:
        print("No devices found.")
        print(
            "Connect a phone over USB, enable Developer options > USB debugging, "
            "then accept the prompt on the phone."
        )
        saved = manager.load_saved()
        if saved:
            print(f"Remembered wireless endpoint: {saved} (run with --transport tcp)")
        return 1

    for device in devices:
        state = "ready" if device.is_usable else device.state
        print(
            f"{device.serial}\t{state}\t{transport_of(device.serial)}\t"
            f"{device.display_name}"
        )
    saved = manager.load_saved()
    if saved:
        print(f"Remembered wireless endpoint: {saved}")
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    say = lambda message: print(message, file=sys.stderr)

    adb_path = ensure_adb(args.adb, progress=say)
    print(f"adb:          {adb_path}")

    cache = args.cache_dir or cache_root()
    jar = ensure_server_jar(cache, progress=say, refresh=args.force)
    print(f"server jar:   {jar}")
    print(f"server ver.:  {SCRCPY_VERSION}")
    print(f"cache dir:    {cache}")
    print()
    print("Setup complete. Run 'mirror-screen' to start mirroring.")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    from .probe import run_probe

    config = _config_from_args(args)
    config.validate()

    result = run_probe(
        config,
        duration=args.duration,
        screenshot=args.screenshot,
        check_control=not args.no_check_control,
        progress=lambda message: print(message, file=sys.stderr),
    )
    print()
    print(result.render())
    return 0 if result.ok else 1


def _cmd_selftest(args: argparse.Namespace) -> int:
    from .selfcheck import run_self_check

    work_dir = args.work_dir or (cache_root() / "selftest")
    report = run_self_check(
        work_dir,
        output_image=args.output,
        progress=lambda message: print(message, file=sys.stderr),
    )
    print(report.render())
    if report.image_path is not None:
        print(f"\nRendered test pattern: {report.image_path}")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Default to "run" when no subcommand is given, but leave the parser-level
    # flags (help, version) alone.
    if not arguments or (
        arguments[0].startswith("-")
        and arguments[0] not in {"-h", "--help", "--version", "--list-profiles"}
    ):
        arguments.insert(0, "run")

    parser = build_parser()
    args = parser.parse_args(arguments)

    level = logging.DEBUG if "debug" in arguments or "-v" in arguments else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        if getattr(args, "list_profiles", False):
            from .config import STREAM_PROFILES

            print(
                f"{'Profile':<20} {'Codec':<6} {'Bitrate':<10} {'Audio':<8} {'Buffer':<8} {'Max Size':<8}"
            )
            print("-" * 64)
            for name, p in STREAM_PROFILES.items():
                print(
                    f"{name:<20} {p.get('video_codec', 'h264'):<6} "
                    f"{p['video_bit_rate'] // 1_000_000}M{'':<7} "
                    f"{p.get('audio_bit_rate', 320_000) // 1000}k{'':<5} "
                    f"{p.get('video_buffer_ms', 0)}ms{'':<5} "
                    f"{'native' if p['max_size'] == 0 else str(p['max_size']) + 'px'}"
                )
            return 0

        if args.command != "devices" and getattr(args, "disconnect_wireless", False):
            manager = _transport_manager(args)
            endpoint = manager.load_saved()
            manager.forget()
            print(
                f"forgot wireless endpoint: {endpoint or 'none remembered'}",
                file=sys.stderr,
            )
        return int(args.func(args))
    except MirrorScreenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
