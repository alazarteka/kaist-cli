from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .distribution import BundleManifest, DistributionInfo, RELEASE_REPO, discover_distribution_info, load_bundle_manifest
from .versioning import version_string


GITHUB_API_BASE = "https://api.github.com"
INSTALL_HINT = "Reinstall using `curl -fsSL https://raw.githubusercontent.com/alazarteka/kaist-cli/main/install.sh | bash`."


class SelfUpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    browser_download_url: str
    size: int


@dataclass(frozen=True)
class ManagedInstallContext:
    distribution: DistributionInfo
    install_root: Path
    bundle_root: Path
    manifest: BundleManifest
    versions_dir: Path
    current_link: Path
    previous_link: Path


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
        if mach_l != "x86_64":
            raise SelfUpdateError(
                "Published Linux standalone bundles support only x86_64 glibc hosts (Ubuntu/Debian-class)."
            )
        libc_kind = _linux_libc_kind()
        if libc_kind == "glibc":
            return "linux-x86_64-gnu"
        if libc_kind == "musl":
            raise SelfUpdateError(
                "Published Linux standalone bundles support only x86_64 glibc hosts (Ubuntu/Debian-class). musl/Alpine is not supported."
            )
        raise SelfUpdateError(
            "Could not detect a supported Linux libc. Published Linux standalone bundles support only x86_64 glibc hosts (Ubuntu/Debian-class)."
        )
    raise SelfUpdateError(
        f"Unsupported platform for self-update: {system_name}/{machine_name}. "
        "Supported standalone bundles are macOS arm64/x86_64 and Linux x86_64 glibc."
    )


def _linux_libc_kind() -> str:
    override = str(os.environ.get("KAIST_LINUX_LIBC") or "").strip().lower()
    if override:
        return override

    libc_name, _ = platform.libc_ver()
    libc_name = str(libc_name or "").strip().lower()
    if libc_name in {"glibc", "musl"}:
        return libc_name

    try:
        output = subprocess.check_output(
            ["getconf", "GNU_LIBC_VERSION"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        output = ""
    if output:
        return "glibc"

    try:
        output = subprocess.check_output(
            ["ldd", "--version"],
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        output = ""
    output_lower = output.lower()
    if "musl" in output_lower:
        return "musl"
    if "glibc" in output_lower or "gnu libc" in output_lower:
        return "glibc"
    return "unknown"


def platform_target() -> str:
    override = str(os.environ.get("KAIST_PLATFORM_TARGET") or "").strip()
    if override:
        return override
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


def _extract_bundle_root(archive_path: Path, destination_dir: Path) -> BundleManifest:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_root = destination_dir.resolve()
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            member_path = (destination_root / member.name).resolve()
            if member_path != destination_root and destination_root not in member_path.parents:
                raise SelfUpdateError(f"Archive member escapes bundle root: {member.name}")
        try:
            tar.extractall(destination_dir, filter="data")
        except TypeError:
            tar.extractall(destination_dir)

    manifest = load_bundle_manifest(destination_dir)
    if manifest is None:
        raise SelfUpdateError("Release archive does not include a valid bundle.json")
    binary_path = destination_dir / manifest.binary_relpath
    if not binary_path.exists():
        raise SelfUpdateError("Release archive does not contain bundled kaist binary")
    os.chmod(binary_path, 0o755)
    return manifest


def _current_binary_path() -> Path:
    import sys

    if not bool(getattr(sys, "frozen", False)):
        raise SelfUpdateError(
            "Self-update installation is only supported for standalone kaist binaries. "
            "Current runtime is a Python environment. "
            f"{INSTALL_HINT}"
        )
    return Path(sys.executable)


def _managed_install_context(executable_path: Path) -> ManagedInstallContext | None:
    distribution = discover_distribution_info(executable=executable_path, frozen=True)
    if distribution.distribution != "managed-release":
        return None
    if distribution.install_root is None or distribution.bundle_root is None or distribution.manifest is None:
        return None
    versions_dir = distribution.install_root / "versions"
    current_link = distribution.install_root / "current"
    previous_link = distribution.install_root / "previous"
    if not versions_dir.exists():
        return None
    return ManagedInstallContext(
        distribution=distribution,
        install_root=distribution.install_root,
        bundle_root=distribution.bundle_root,
        manifest=distribution.manifest,
        versions_dir=versions_dir,
        current_link=current_link,
        previous_link=previous_link,
    )


def _maybe_update_launcher_symlink(executable_path: Path, installed_binary_path: Path) -> str | None:
    if executable_path.is_symlink() or not executable_path.exists():
        _swap_symlink(executable_path, installed_binary_path)
        return str(executable_path)
    return None


def _resolved_symlink(path: Path) -> Path | None:
    if not path.exists():
        return None
    try:
        return path.resolve()
    except OSError:
        return None


def _swap_symlink(link_path: Path, target_path: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = link_path.parent / f".{link_path.name}.new"
    if tmp_path.exists() or tmp_path.is_symlink():
        tmp_path.unlink()
    tmp_path.symlink_to(target_path)
    os.replace(tmp_path, link_path)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)


def _prune_versions(ctx: ManagedInstallContext, keep_roots: set[Path]) -> list[str]:
    warnings: list[str] = []
    if not ctx.versions_dir.exists():
        return warnings
    resolved_keep = {root.resolve() for root in keep_roots}
    for child in sorted(ctx.versions_dir.iterdir()):
        if not child.is_dir():
            continue
        try:
            if child.resolve() in resolved_keep:
                continue
        except OSError:
            pass
        try:
            shutil.rmtree(child)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not prune {child}: {exc}")
    return warnings


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _sync_claude_plugin_metadata(install_root: Path, *, version: str) -> Path:
    marketplace_root = install_root / "mkt"
    plugin_root = marketplace_root / "plugins" / "kaist-cli"
    plugin_manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
    marketplace_manifest_path = marketplace_root / ".claude-plugin" / "marketplace.json"
    skill_target = install_root / "current" / "skills" / "kaist-cli"
    skill_link = plugin_root / "skills" / "kaist-cli"

    plugin_root.mkdir(parents=True, exist_ok=True)
    skill_link.parent.mkdir(parents=True, exist_ok=True)
    if skill_link.exists() or skill_link.is_symlink():
        _remove_path(skill_link)
    skill_link.symlink_to(skill_target, target_is_directory=True)

    _write_json(
        plugin_manifest_path,
        {
            "name": "kaist-cli",
            "description": "Operate KLMS through the installed kaist CLI.",
            "author": {"name": "kaist-cli"},
        },
    )
    _write_json(
        marketplace_manifest_path,
        {
            "name": "kaist-cli",
            "owner": {"name": "kaist-cli"},
            "plugins": [
                {
                    "name": "kaist-cli",
                    "description": "Operate KLMS through the installed kaist CLI.",
                    "version": str(version),
                    "author": {"name": "kaist-cli"},
                    "source": "./plugins/kaist-cli",
                    "category": "productivity",
                }
            ],
        },
    )
    return marketplace_manifest_path


def check_for_update() -> dict[str, Any]:
    current = version_string()
    target = platform_target()
    release = fetch_latest_release()
    latest_tag = str(release.get("tag_name") or "").strip()
    latest = normalize_version(latest_tag)
    assets = _coerce_assets(release)
    archive_asset = select_archive_asset(assets, target)
    has_checksums = any(asset.name == "checksums.txt" for asset in assets)
    payload = {
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
    payload.update(discover_distribution_info().as_payload())
    return payload


def perform_self_update() -> dict[str, Any]:
    check = check_for_update()
    if not check.get("update_available"):
        return {
            "ok": True,
            "updated": False,
            **check,
            "message": "Already on latest version.",
        }

    executable_path = _current_binary_path()
    ctx = _managed_install_context(executable_path)
    if ctx is None:
        raise SelfUpdateError(
            "Managed self-update is only supported for installs created by the bundled install.sh layout. "
            + INSTALL_HINT
        )

    release = fetch_latest_release()
    assets = _coerce_assets(release)
    target = str(check["platform_target"])
    archive_asset = select_archive_asset(assets, target)
    checksums_asset = select_checksums_asset(assets)

    with tempfile.TemporaryDirectory(prefix="kaist-update-") as tmp:
        tmp_dir = Path(tmp)
        archive_path = tmp_dir / archive_asset.name
        checksums_path = tmp_dir / checksums_asset.name
        staged_bundle = tmp_dir / "bundle"
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

        manifest = _extract_bundle_root(archive_path, staged_bundle)
        version_tag = str(check.get("latest_tag") or f"v{check['latest_version']}")
        version_dir = ctx.versions_dir / version_tag
        temp_version_dir = ctx.versions_dir / f".{version_tag}.install"

        if temp_version_dir.exists() or temp_version_dir.is_symlink():
            _remove_path(temp_version_dir)
        if version_dir.exists() or version_dir.is_symlink():
            _remove_path(version_dir)

        shutil.copytree(staged_bundle, temp_version_dir)
        os.replace(temp_version_dir, version_dir)

    previous_root = _resolved_symlink(ctx.current_link)
    _swap_symlink(ctx.current_link, version_dir)
    if previous_root is not None and previous_root != version_dir.resolve():
        _swap_symlink(ctx.previous_link, previous_root)
    elif ctx.previous_link.exists() or ctx.previous_link.is_symlink():
        _remove_path(ctx.previous_link)

    keep_roots = {version_dir}
    previous_kept = _resolved_symlink(ctx.previous_link)
    if previous_kept is not None:
        keep_roots.add(previous_kept)
    warnings = _prune_versions(ctx, keep_roots)
    claude_marketplace_path: str | None = None
    try:
        claude_marketplace_path = str(_sync_claude_plugin_metadata(ctx.install_root, version=manifest.version))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not sync Claude plugin marketplace: {exc}")

    current_bundle_root = ctx.current_link
    bundled_skill_path = current_bundle_root / manifest.skill_relpath
    installed_binary_path = current_bundle_root / manifest.binary_relpath
    launcher_path: str | None = None
    if ctx.install_root not in executable_path.parents:
        try:
            launcher_path = _maybe_update_launcher_symlink(executable_path, installed_binary_path)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not update launcher symlink {executable_path}: {exc}")

    payload = {
        "ok": True,
        "updated": True,
        **check,
        "binary_path": str(installed_binary_path),
        "launcher_path": launcher_path,
        "install_root": str(ctx.install_root),
        "bundled_skill_path": str(bundled_skill_path),
        "claude_marketplace_path": claude_marketplace_path,
        "previous_version": previous_root.name if previous_root is not None else None,
        "message": "Update installed. Restart the kaist command.",
    }
    if warnings:
        payload["warnings"] = warnings
    return payload
