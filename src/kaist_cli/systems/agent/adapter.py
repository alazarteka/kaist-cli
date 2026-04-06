from __future__ import annotations

import argparse
from typing import Any

from ...core.agents import agent_status, install_agent, uninstall_agent
from ...core.contracts import SystemAdapter


class AgentAdapter(SystemAdapter):
    system_name = "agent"

    def register(self, top_subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        agent = top_subparsers.add_parser(
            "agent",
            help="Install or inspect bundled kaist agent skills for Codex, Claude, and Gemini",
        )
        agent_sub = agent.add_subparsers(dest="action", required=True, metavar="ACTION")

        install = agent_sub.add_parser("install", help="Install the bundled `kaist-cli` skill for a supported agent")
        install.add_argument("target", choices=["codex", "claude", "gemini", "custom"])
        install.add_argument("--path", help="Target directory for custom installs.")
        install.add_argument("--copy", action="store_true", help="Copy the skill directory instead of creating a symlink.")
        install.add_argument("--force", action="store_true", help="Replace an existing target path.")
        install.set_defaults(
            handler=self._handle_install,
            schema_name="kaist.cli.agent.install.v1",
            command_path="agent install",
        )

        status = agent_sub.add_parser("status", help="Show bundled `kaist-cli` skill status for Codex, Claude, Gemini, and optional custom path")
        status.add_argument("--path", help="Also inspect a custom install root.")
        status.set_defaults(
            handler=self._handle_status,
            schema_name="kaist.cli.agent.status.v1",
            command_path="agent status",
        )

        uninstall = agent_sub.add_parser("uninstall", help="Remove an installed `kaist-cli` skill from a supported agent path")
        uninstall.add_argument("target", choices=["codex", "claude", "gemini", "custom"])
        uninstall.add_argument("--path", help="Target directory for custom uninstalls.")
        uninstall.set_defaults(
            handler=self._handle_uninstall,
            schema_name="kaist.cli.agent.uninstall.v1",
            command_path="agent uninstall",
        )

    def _handle_install(self, args: argparse.Namespace) -> dict[str, Any]:
        return install_agent(args.target, custom_path=args.path, copy=bool(args.copy), force=bool(args.force))

    def _handle_status(self, args: argparse.Namespace) -> dict[str, Any]:
        return agent_status(custom_path=args.path)

    def _handle_uninstall(self, args: argparse.Namespace) -> dict[str, Any]:
        return uninstall_agent(args.target, custom_path=args.path)
