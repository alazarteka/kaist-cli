from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .versioning import version_string


RELEASE_REPO = "alazarteka/kaist-cli"
GITHUB_API_BASE = "https://api.github.com"


class SelfUpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    browser_download_url: str
    size: int


def _urlopen(request: urllib.request.Request, *, timeout: int) -> Any:
    context = None
    try:
        import certifi  # type: ignore[import-untyped]

        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        context = None

    if context is None:
        return urllib.request.urlopen(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def normalize_version(value: str) -> str:
    s = str(value or "").strip()
    if s.startswith("v"):
        return s[1:]
    return s


def _version_key(value: str) -> tuple[int, ...]:
    parts = [int(x) for x in re.findall(r"\d+", normalize_version(value))]
    return tuple(parts)


def _platform_target(system_name: str, machine_name: str) -> str:
    sys_l = (system_name or "").strip().lower()
    mach_l = (machine_name or "").strip().lower()
    if mach_l in {"aarch64"}:
        mach_l = "arm64"
    if mach_l in {"amd64"}:
        mach_l = "x86_64"

    if sys_l == "darwin":
        if mach_l == "arm64":
            return "darwin-arm64"
        if mach_l == "x86_64":
            return "darwin-x86_64"
    if sys_l == "linux":
        if mach_l == "arm64":
            return "linux-arm64"
        if mach_l == "x86_64":
            return "linux-x86_64"
    if sys_l == "windows":
        if mach_l in {"x86_64", "arm64"}:
            return f"windows-{mach_l}"
    raise SelfUpdateError(f"Unsupported platform for self-update: {system_name}/{machine_name}")


def platform_target() -> str:
    return _platform_target(platform.system(), platform.machine())


def _github_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "kaist-cli-updater",
        },
    )
    try:
        with _urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SelfUpdateError("No GitHub release found yet for this repository.") from exc
        raise SelfUpdateError(f"Failed to reach GitHub API: {exc}") from exc
    except urllib.error.URLError as exc:
        raise SelfUpdateError(f"Failed to reach GitHub API: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SelfUpdateError(f"Invalid JSON from GitHub API: {exc}") from exc
    if not isinstance(payload, dict):
        raise SelfUpdateError("Unexpected GitHub API payload type")
    return payload


def fetch_latest_release() -> dict[str, Any]:
    return _github_json(f"{GITHUB_API_BASE}/repos/{RELEASE_REPO}/releases/latest")


def _coerce_assets(release: dict[str, Any]) -> list[ReleaseAsset]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        return []
    out: list[ReleaseAsset] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip()
        url = str(asset.get("browser_download_url") or "").strip()
        size_raw = asset.get("size")
        if not name or not url:
            continue
        try:
            size = int(size_raw)
        except Exception:
            size = 0
        out.append(ReleaseAsset(name=name, browser_download_url=url, size=size))
    return out


def select_archive_asset(assets: list[ReleaseAsset], target: str) -> ReleaseAsset:
    pattern = re.compile(rf"^kaist-v?\d+\.\d+\.\d+(-[0-9A-Za-z.\-]+)?-{re.escape(target)}\.tar\.gz$")
    direct_hits = [asset for asset in assets if pattern.match(asset.name)]
    if direct_hits:
        return sorted(direct_hits, key=lambda a: a.name)[-1]
    fallback_hits = [asset for asset in assets if asset.name.endswith(f"-{target}.tar.gz")]
    if fallback_hits:
        return sorted(fallback_hits, key=lambda a: a.name)[-1]
    raise SelfUpdateError(f"No release asset found for target '{target}'")


def select_checksums_asset(assets: list[ReleaseAsset]) -> ReleaseAsset:
    for asset in assets:
        if asset.name == "checksums.txt":
            return asset
    raise SelfUpdateError("Release does not include checksums.txt")


def _download_to_path(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "kaist-cli-updater"})
    try:
        with _urlopen(request, timeout=60) as response:
            destination.write_bytes(response.read())
    except urllib.error.URLError as exc:
        raise SelfUpdateError(f"Failed downloading release asset: {exc}") from exc


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def parse_checksums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        filename = parts[-1].strip().lstrip("*")
        if filename:
            out[filename] = digest
    return out


def _extract_binary(archive_path: Path, destination_dir: Path) -> Path:
    with tarfile.open(archive_path, "r:gz") as tar:
        candidates = [member for member in tar.getmembers() if member.isfile() and Path(member.name).name == "kaist"]
        if not candidates:
            raise SelfUpdateError("Release archive does not contain 'kaist' binary")
        member = sorted(candidates, key=lambda m: len(Path(m.name).parts))[0]
        extracted = tar.extractfile(member)
        if extracted is None:
            raise SelfUpdateError("Failed to extract binary from archive")
        out_path = destination_dir / "kaist"
        with out_path.open("wb") as handle:
            shutil.copyfileobj(extracted, handle)
    os.chmod(out_path, 0o755)
    return out_path


def _current_binary_path() -> Path:
    import sys

    if not bool(getattr(sys, "frozen", False)):
        raise SelfUpdateError(
            "Self-update is only supported for standalone kaist binaries. "
            "Current runtime is a Python environment (e.g. uv/pip/pipx source run)."
        )
    return Path(sys.executable).resolve()


def check_for_update() -> dict[str, Any]:
    current = version_string()
    target = platform_target()
    release = fetch_latest_release()
    latest_tag = str(release.get("tag_name") or "").strip()
    latest = normalize_version(latest_tag)
    assets = _coerce_assets(release)
    archive_asset = select_archive_asset(assets, target)
    has_checksums = any(asset.name == "checksums.txt" for asset in assets)
    return {
        "ok": True,
        "repo": RELEASE_REPO,
        "current_version": current,
        "latest_version": latest,
        "latest_tag": latest_tag,
        "update_available": normalize_version(current) != latest,
        "platform_target": target,
        "archive_asset": archive_asset.name,
        "has_checksums": has_checksums,
    }


def perform_self_update() -> dict[str, Any]:
    check = check_for_update()
    if not check.get("update_available"):
        return {
            "ok": True,
            "updated": False,
            **check,
            "message": "Already on latest version.",
        }

    binary_path = _current_binary_path()
    release = fetch_latest_release()
    assets = _coerce_assets(release)
    target = str(check["platform_target"])
    archive_asset = select_archive_asset(assets, target)
    checksums_asset = select_checksums_asset(assets)

    with tempfile.TemporaryDirectory(prefix="kaist-update-") as tmp:
        tmp_dir = Path(tmp)
        archive_path = tmp_dir / archive_asset.name
        checksums_path = tmp_dir / checksums_asset.name
        _download_to_path(archive_asset.browser_download_url, archive_path)
        _download_to_path(checksums_asset.browser_download_url, checksums_path)

        checksums = parse_checksums(checksums_path.read_text(encoding="utf-8"))
        expected = checksums.get(archive_asset.name)
        if not expected:
            raise SelfUpdateError(f"checksums.txt does not include {archive_asset.name}")
        actual = _sha256(archive_path)
        if actual != expected:
            raise SelfUpdateError(
                f"Checksum mismatch for {archive_asset.name}: expected {expected}, got {actual}"
            )

        extracted_binary = _extract_binary(archive_path, tmp_dir)
        staged_binary = binary_path.parent / f".{binary_path.name}.new"
        backup_binary = binary_path.parent / f".{binary_path.name}.bak"

        if staged_binary.exists():
            staged_binary.unlink()
        if backup_binary.exists():
            backup_binary.unlink()

        shutil.copy2(extracted_binary, staged_binary)
        os.chmod(staged_binary, 0o755)

        had_existing = binary_path.exists()
        if had_existing:
            os.replace(binary_path, backup_binary)
        try:
            os.replace(staged_binary, binary_path)
        except Exception as exc:
            if had_existing and backup_binary.exists():
                os.replace(backup_binary, binary_path)
            raise SelfUpdateError(f"Failed to install updated binary: {exc}") from exc
        finally:
            if staged_binary.exists():
                staged_binary.unlink()

        if backup_binary.exists():
            backup_binary.unlink()

    return {
        "ok": True,
        "updated": True,
        **check,
        "binary_path": str(binary_path),
        "message": "Update installed. Restart the kaist command.",
    }
