from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KlmsPaths:
    home_root: Path
    private_root: Path
    files_root: Path
    profile_lock_path: Path
    profile_dir: Path
    config_path: Path
    storage_state_path: Path
    snapshot_path: Path
    cache_path: Path
    auth_session_path: Path
    notice_store_path: Path
    media_recency_store_path: Path
    endpoint_discovery_path: Path
    api_map_path: Path
    playwright_browsers_dir: Path


def resolve_paths() -> KlmsPaths:
    home_root = Path(os.environ.get("KAIST_CLI_HOME") or str(Path.home() / ".kaist-cli")).expanduser()
    private_root = home_root / "private" / "klms"
    files_root = home_root / "files" / "klms"
    return KlmsPaths(
        home_root=home_root,
        private_root=private_root,
        files_root=files_root,
        profile_lock_path=private_root / ".lock",
        profile_dir=private_root / "profile",
        config_path=private_root / "config.toml",
        storage_state_path=private_root / "storage_state.json",
        snapshot_path=private_root / "snapshot.json",
        cache_path=private_root / "cache.json",
        auth_session_path=private_root / "auth_session.json",
        notice_store_path=private_root / "notice_store.json",
        media_recency_store_path=private_root / "media_recency_store.json",
        endpoint_discovery_path=private_root / "endpoint_discovery.json",
        api_map_path=private_root / "api_map.json",
        playwright_browsers_dir=private_root / "playwright-browsers",
    )


def chmod_best_effort(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        pass


def ensure_private_dirs(paths: KlmsPaths) -> None:
    paths.private_root.mkdir(parents=True, exist_ok=True)
    chmod_best_effort(paths.private_root, 0o700)
    paths.files_root.mkdir(parents=True, exist_ok=True)


def configure_playwright_env(paths: KlmsPaths) -> Path:
    ensure_private_dirs(paths)
    paths.playwright_browsers_dir.mkdir(parents=True, exist_ok=True)
    chmod_best_effort(paths.playwright_browsers_dir, 0o700)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(paths.playwright_browsers_dir))
    return paths.playwright_browsers_dir
