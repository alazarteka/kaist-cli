from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from .contracts import SystemAdapter


@dataclass
class SystemRegistry:
    _adapters: dict[str, SystemAdapter] = field(default_factory=dict)

    def register(self, adapter: SystemAdapter) -> None:
        if adapter.system_name in self._adapters:
            raise ValueError(f"Duplicate system adapter registration: {adapter.system_name}")
        self._adapters[adapter.system_name] = adapter

    def register_parsers(self, top_subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        for adapter in self._adapters.values():
            adapter.register(top_subparsers)

    @property
    def adapters(self) -> dict[str, SystemAdapter]:
        return dict(self._adapters)


def default_registry() -> SystemRegistry:
    from ..systems.klms.adapter import KlmsAdapter
    from ..systems.update.adapter import UpdateAdapter
    from ..systems.version.adapter import VersionAdapter

    registry = SystemRegistry()
    registry.register(VersionAdapter())
    registry.register(UpdateAdapter())
    registry.register(KlmsAdapter())
    # PortalAdapter disabled — scaffold only, no implemented functionality yet
    return registry
