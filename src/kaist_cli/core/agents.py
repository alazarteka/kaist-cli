from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .distribution import SKILL_NAME, discover_distribution_info


class AgentCommandError(RuntimeError):
    def __init__(self, code: str, exit_code: int, retryable: bool, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.retryable = retryable
        self.hint = hint


@dataclass(frozen=True)
class AgentInstallSpec:
    agent: str
    label: str
    root: Path
    target_path: Path


def _bundled_skill_path() -> Path:
    distribution = discover_distribution_info()
    if distribution.bundled_skill_path is None or not distribution.bundled_skill_path.exists():
        raise AgentCommandError(
            "CONFIG_INVALID",
            40,
            False,
            "Bundled kaist skill is not available in this runtime.",
            "Use an installed or source checkout that includes skills/kaist-cli.",
        )
    return distribution.bundled_skill_path


def _codex_root() -> Path:
    raw = str(os.environ.get("CODEX_HOME") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def _resolve_custom_target(path: str | Path) -> tuple[Path, Path]:
    root = Path(path).expanduser()
    target = root if root.name == SKILL_NAME else root / SKILL_NAME
    return root, target


def resolve_agent_install_spec(agent: str, *, custom_path: str | None = None) -> AgentInstallSpec:
    agent_name = str(agent or "").strip().lower()
    if agent_name == "codex":
        root = _codex_root() / "skills"
        return AgentInstallSpec(agent="codex", label="Codex", root=root, target_path=root / SKILL_NAME)
    if agent_name == "claude":
        root = Path.home() / ".claude" / "skills"
        return AgentInstallSpec(agent="claude", label="Claude Code", root=root, target_path=root / SKILL_NAME)
    if agent_name == "gemini":
        root = Path.home() / ".gemini" / "skills"
        return AgentInstallSpec(agent="gemini", label="Gemini CLI", root=root, target_path=root / SKILL_NAME)
    if agent_name == "custom":
        if not str(custom_path or "").strip():
            raise AgentCommandError(
                "CONFIG_INVALID",
                40,
                False,
                "Custom agent installs require --path.",
                "Run `kaist agent install custom --path /target/dir`.",
            )
        root, target = _resolve_custom_target(str(custom_path))
        return AgentInstallSpec(agent="custom", label="Custom", root=root, target_path=target)
    raise AgentCommandError("CONFIG_INVALID", 40, False, f"Unsupported agent target: {agent}", None)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)


def _status_entry(spec: AgentInstallSpec, *, bundled_skill_path: Path) -> dict[str, Any]:
    path = spec.target_path
    exists = path.exists() or path.is_symlink()
    resolved: Path | None = None
    if exists:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = None
    if path.is_symlink():
        mode = "symlink"
    elif path.is_dir():
        mode = "copy"
    elif path.exists():
        mode = "other"
    else:
        mode = "missing"
    return {
        "agent": spec.agent,
        "label": spec.label,
        "root_path": str(spec.root),
        "target_path": str(path),
        "installed": exists,
        "mode": mode,
        "points_to_bundled_skill": resolved == bundled_skill_path.resolve() if resolved is not None else False,
        "resolved_path": str(resolved) if resolved is not None else None,
    }


def _install_target(spec: AgentInstallSpec, *, copy: bool, force: bool) -> dict[str, Any]:
    bundled_skill_path = _bundled_skill_path()
    path = spec.target_path
    existing = path.exists() or path.is_symlink()
    installed_mode = "copy" if copy else "symlink"

    if existing:
        current = _status_entry(spec, bundled_skill_path=bundled_skill_path)
        if current["points_to_bundled_skill"] and current["mode"] == installed_mode:
            return {
                **current,
                "source_path": str(bundled_skill_path),
                "action": "noop",
                "created": False,
            }
        if not force:
            raise AgentCommandError(
                "CONFIG_INVALID",
                40,
                False,
                f"Target path already exists: {path}",
                "Re-run with --force to replace it.",
            )
        _remove_path(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copytree(bundled_skill_path, path)
    else:
        path.symlink_to(bundled_skill_path, target_is_directory=True)

    return {
        **_status_entry(spec, bundled_skill_path=bundled_skill_path),
        "source_path": str(bundled_skill_path),
        "action": "installed",
        "created": True,
    }


def _uninstall_target(spec: AgentInstallSpec) -> dict[str, Any]:
    bundled_skill_path = _bundled_skill_path()
    path = spec.target_path
    existed = path.exists() or path.is_symlink()
    previous = _status_entry(spec, bundled_skill_path=bundled_skill_path)
    if existed:
        _remove_path(path)
    return {
        **previous,
        "action": "removed" if existed else "noop",
        "removed": existed,
        "installed": False,
        "mode": "missing",
        "resolved_path": None,
        "points_to_bundled_skill": False,
    }


def install_agent(agent: str, *, custom_path: str | None = None, copy: bool = False, force: bool = False) -> dict[str, Any]:
    spec = resolve_agent_install_spec(agent, custom_path=custom_path)
    result = _install_target(spec, copy=copy, force=force)
    result["bundled_skill_path"] = str(_bundled_skill_path())
    return result


def uninstall_agent(agent: str, *, custom_path: str | None = None) -> dict[str, Any]:
    spec = resolve_agent_install_spec(agent, custom_path=custom_path)
    result = _uninstall_target(spec)
    result["bundled_skill_path"] = str(_bundled_skill_path())
    return result


def agent_status(*, custom_path: str | None = None) -> dict[str, Any]:
    bundled_skill_path = _bundled_skill_path()
    agents = [
        _status_entry(resolve_agent_install_spec(name), bundled_skill_path=bundled_skill_path)
        for name in ("codex", "claude", "gemini")
    ]
    if str(custom_path or "").strip():
        agents.append(
            _status_entry(
                resolve_agent_install_spec("custom", custom_path=custom_path),
                bundled_skill_path=bundled_skill_path,
            )
        )
    return {
        "bundled_skill_path": str(bundled_skill_path),
        "agents": agents,
    }
