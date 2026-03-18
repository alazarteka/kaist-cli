from __future__ import annotations

import pytest

from kaist_cli.cli.parser import build_parser


def test_registry_registers_core_and_system_commands() -> None:
    parser = build_parser()

    args_version = parser.parse_args(["version"])
    assert args_version.system == "version"
    assert callable(args_version.handler)

    args_update = parser.parse_args(["update", "--check"])
    assert args_update.system == "update"
    assert callable(args_update.handler)

    args_klms = parser.parse_args(["klms", "courses", "list"])
    assert args_klms.system == "klms"
    assert args_klms.group == "courses"
    assert args_klms.action == "list"
    assert callable(args_klms.handler)

    args_install_browser = parser.parse_args(["klms", "auth", "install-browser"])
    assert args_install_browser.system == "klms"
    assert args_install_browser.group == "auth"
    assert args_install_browser.action == "install-browser"
    assert callable(args_install_browser.handler)


def test_old_flat_klms_commands_are_not_parseable() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["klms", "list", "courses"])
