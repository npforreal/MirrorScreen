"""Every subcommand's --help must render (argparse %-formats help strings)."""

from __future__ import annotations

import pytest

from mirror_screen.cli import build_parser

SUBCOMMANDS = ["run", "devices", "setup", "ui", "probe", "selftest"]


@pytest.mark.parametrize("command", [None, *SUBCOMMANDS])
def test_help_renders(command: str | None, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--help"] if command is None else [command, "--help"]
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_clean_window_help_mentions_full_video() -> None:
    text = build_parser()._subparsers._group_actions[0].choices["run"].format_help()
    assert "100% video" in text
