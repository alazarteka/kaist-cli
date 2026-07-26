from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .fsutil import remove_path
from typing import Any

from .distribution import SKILL_NAME, discover_distribution_info


INSTALL_MARKER_NAME = ".kaist-cli-install.json"
INSTALL_MARKER_VERSION = 1


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
    points_to_bundled_skill = resolved == bundled_skill_path.resolve() if resolved is not None else False
    managed_copy = _has_install_marker(path)
    if points_to_bundled_skill and path.is_symlink():
        ownership = "managed-symlink"
    elif managed_copy:
        ownership = "managed-copy"
    elif path.is_dir():
        ownership = "legacy-copy"
    elif exists:
        ownership = "unowned"
    else:
        ownership = "missing"
    return {
        "agent": spec.agent,
        "label": spec.label,
        "root_path": str(spec.root),
        "target_path": str(path),
        "installed": exists,
        "mode": mode,
        "points_to_bundled_skill": points_to_bundled_skill,
        "resolved_path": str(resolved) if resolved is not None else None,
        "ownership": ownership,
        "install_marker": str(path / INSTALL_MARKER_NAME) if path.is_dir() and not path.is_symlink() else None,
    }


def _has_install_marker(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    marker = path / INSTALL_MARKER_NAME
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("managed_by") == "kaist-cli"
        and payload.get("skill_name") == SKILL_NAME
        and payload.get("marker_version") == INSTALL_MARKER_VERSION
    )


def _write_install_marker(path: Path) -> None:
    (path / INSTALL_MARKER_NAME).write_text(
        json.dumps(
            {
                "managed_by": "kaist-cli",
                "skill_name": SKILL_NAME,
                "marker_version": INSTALL_MARKER_VERSION,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _install_target(spec: AgentInstallSpec, *, copy: bool, force: bool) -> dict[str, Any]:
    bundled_skill_path = _bundled_skill_path()
    path = spec.target_path
    existing = path.exists() or path.is_symlink()
    installed_mode = "copy" if copy else "symlink"

    if existing:
        current = _status_entry(spec, bundled_skill_path=bundled_skill_path)
        if (
            (current["points_to_bundled_skill"] and current["mode"] == "symlink")
            or (current["ownership"] == "managed-copy" and installed_mode == "copy")
        ):
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
        remove_path(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copytree(bundled_skill_path, path)
        _write_install_marker(path)
    else:
        path.symlink_to(bundled_skill_path, target_is_directory=True)

    return {
        **_status_entry(spec, bundled_skill_path=bundled_skill_path),
        "source_path": str(bundled_skill_path),
        "action": "installed",
        "created": True,
    }


def _uninstall_target(
    spec: AgentInstallSpec,
    *,
    confirm: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    bundled_skill_path = _bundled_skill_path()
    path = spec.target_path
    existed = path.exists() or path.is_symlink()
    previous = _status_entry(spec, bundled_skill_path=bundled_skill_path)
    ownership = str(previous.get("ownership") or "unowned")
    managed = ownership in {"managed-symlink", "managed-copy"}
    exact_confirmation = False
    if confirm is not None:
        try:
            exact_confirmation = Path(confirm).expanduser().resolve(strict=False) == path.resolve(strict=False)
        except OSError:
            exact_confirmation = False
    if existed and not managed and not exact_confirmation:
        raise AgentCommandError(
            "CONFIG_INVALID",
            40,
            False,
            f"Refusing to remove an unverified agent target: {path}",
            f"Only a kaist-cli-managed symlink or marked copy is removed automatically. For a legacy copy, rerun with `--confirm {path}` after verifying the exact target.",
        )
    if existed and dry_run:
        return {
            **previous,
            "action": "would_remove",
            "removed": False,
            "dry_run": True,
        }
    if existed:
        remove_path(path)
    return {
        **previous,
        "action": "removed" if existed else "noop",
        "removed": existed,
        "installed": False,
        "mode": "missing",
        "resolved_path": None,
        "points_to_bundled_skill": False,
        "ownership": "missing",
        "dry_run": False,
    }


def install_agent(agent: str, *, custom_path: str | None = None, copy: bool = False, force: bool = False) -> dict[str, Any]:
    spec = resolve_agent_install_spec(agent, custom_path=custom_path)
    result = _install_target(spec, copy=copy, force=force)
    result["bundled_skill_path"] = str(_bundled_skill_path())
    return result


def uninstall_agent(
    agent: str,
    *,
    custom_path: str | None = None,
    confirm: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    spec = resolve_agent_install_spec(agent, custom_path=custom_path)
    result = _uninstall_target(spec, confirm=confirm, dry_run=dry_run)
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
