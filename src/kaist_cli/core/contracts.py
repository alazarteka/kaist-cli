from __future__ import annotations

import argparse
from typing import Any, Protocol


class SystemAdapter(Protocol):
    system_name: str

    def register(self, top_subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        ...


class AuthProvider(Protocol):
    def login(self, base_url: str | None = None) -> dict[str, Any]:
        ...

    async def status(self, *, validate: bool = True) -> dict[str, Any]:
        ...

    def refresh(self, base_url: str | None = None, *, validate: bool = True) -> dict[str, Any]:
        ...

    async def doctor(self, *, validate: bool = True) -> dict[str, Any]:
        ...


class ResourceService(Protocol):
    async def list(self, **kwargs: Any) -> list[dict[str, Any]]:
        ...
