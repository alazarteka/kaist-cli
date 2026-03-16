from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from kaist_cli.core import updater
from kaist_cli.core.distribution import BundleManifest, DistributionInfo


def test_platform_target_mapping() -> None:
    assert updater._platform_target("Darwin", "arm64") == "darwin-arm64"  # type: ignore[attr-defined]
    assert updater._platform_target("darwin", "x86_64") == "darwin-x86_64"  # type: ignore[attr-defined]
    assert updater._platform_target("Linux", "aarch64") == "linux-arm64"  # type: ignore[attr-defined]
    assert updater._platform_target("linux", "amd64") == "linux-x86_64"  # type: ignore[attr-defined]


def test_platform_target_unsupported_raises() -> None:
    with pytest.raises(updater.SelfUpdateError):
        updater._platform_target("Solaris", "sparc")  # type: ignore[attr-defined]


def test_parse_checksums_supports_star_prefix() -> None:
    parsed = updater.parse_checksums(
        "\n".join(
            [
                "a" * 64 + "  kaist-v0.2.0-darwin-arm64.tar.gz",
                "b" * 64 + " *checksums.txt",
            ]
        )
    )
    assert parsed["kaist-v0.2.0-darwin-arm64.tar.gz"] == "a" * 64
    assert parsed["checksums.txt"] == "b" * 64


def test_select_archive_asset_prefers_targeted_name() -> None:
    assets = [
        updater.ReleaseAsset(name="kaist-v0.1.0-linux-x86_64.tar.gz", browser_download_url="u1", size=1),
        updater.ReleaseAsset(name="kaist-v0.1.0-darwin-arm64.tar.gz", browser_download_url="u2", size=1),
    ]
    selected = updater.select_archive_asset(assets, "darwin-arm64")
    assert selected.name == "kaist-v0.1.0-darwin-arm64.tar.gz"


def test_release_repo_is_fixed() -> None:
    assert updater.RELEASE_REPO == "alazarteka/kaist-cli"


ROOT = Path(__file__).resolve().parents[1]


def _write_fake_binary(path: Path) -> None:
    path.write_text("#!/usr/bin/env bash\necho kaist\n", encoding="utf-8")
    os.chmod(path, 0o755)


def _prepare_release_dir(base_dir: Path, version: str) -> tuple[Path, Path]:
    tag = version if version.startswith("v") else f"v{version}"
    release_dir = base_dir / tag
    release_dir.mkdir(parents=True, exist_ok=True)
    binary_path = base_dir / f"kaist-{tag}"
    _write_fake_binary(binary_path)
    cp = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "build_release_bundle.sh"),
            "--binary",
            str(binary_path),
            "--version",
            tag,
            "--target",
            "darwin-arm64",
            "--out-dir",
            str(release_dir),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert cp.returncode == 0, cp.stderr
    archive_path = release_dir / f"kaist-{tag}-darwin-arm64.tar.gz"
    checksums_path = release_dir / "checksums.txt"
    checksums_path.write_text(
        f"{updater._sha256(archive_path)}  {archive_path.name}\n",
        encoding="utf-8",
    )
    return archive_path, checksums_path


def _populate_bundle_root(bundle_root: Path, version: str) -> None:
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "bin").mkdir(parents=True, exist_ok=True)
    (bundle_root / "skills").mkdir(parents=True, exist_ok=True)
    _write_fake_binary(bundle_root / "bin" / "kaist")
    shutil.copytree(ROOT / "skills" / "kaist-cli", bundle_root / "skills" / "kaist-cli")
    (bundle_root / "bundle.json").write_text(
        json.dumps(
            {
                "version": version.lstrip("v"),
                "repo": updater.RELEASE_REPO,
                "target": "darwin-arm64",
                "binary_relpath": "bin/kaist",
                "skill_relpath": "skills/kaist-cli",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_check_for_update_includes_distribution_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "platform_target", lambda: "darwin-arm64")
    monkeypatch.setattr(updater, "version_string", lambda: "0.1.4")
    monkeypatch.setattr(
        updater,
        "fetch_latest_release",
        lambda: {
            "tag_name": "v0.1.5",
            "assets": [
                {"name": "kaist-v0.1.5-darwin-arm64.tar.gz", "browser_download_url": "archive-url", "size": 1},
                {"name": "checksums.txt", "browser_download_url": "checksums-url", "size": 1},
            ],
        },
    )
    monkeypatch.setattr(
        updater,
        "discover_distribution_info",
        lambda: DistributionInfo(
            distribution="source",
            install_root=ROOT,
            bundled_skill_path=ROOT / "skills" / "kaist-cli",
            self_update_supported=False,
            release_repo=updater.RELEASE_REPO,
        ),
    )

    payload = updater.check_for_update()

    assert payload["distribution"] == "source"
    assert payload["install_root"] == str(ROOT)
    assert payload["bundled_skill_path"] == str(ROOT / "skills" / "kaist-cli")
    assert payload["self_update_supported"] is False


def test_build_release_bundle_contains_skill_and_manifest(tmp_path: Path) -> None:
    archive_path, _ = _prepare_release_dir(tmp_path / "releases", "v0.1.4")

    with tarfile.open(archive_path, "r:gz") as tar:
        names = {name.lstrip("./") for name in tar.getnames()}
        assert "bundle.json" in names
        assert "bin/kaist" in names
        assert "skills/kaist-cli/SKILL.md" in names
        assert "skills/kaist-cli/agents/openai.yaml" in names
        manifest = json.loads(tar.extractfile("bundle.json").read().decode("utf-8"))  # type: ignore[union-attr]

    assert manifest == {
        "version": "0.1.4",
        "repo": updater.RELEASE_REPO,
        "target": "darwin-arm64",
        "binary_relpath": "bin/kaist",
        "skill_relpath": "skills/kaist-cli",
    }


def test_install_script_installs_managed_layout_and_rotates_previous(tmp_path: Path) -> None:
    downloads_dir = tmp_path / "downloads"
    _prepare_release_dir(downloads_dir, "v0.1.4")
    _prepare_release_dir(downloads_dir, "v0.1.5")
    latest_json = tmp_path / "latest.json"
    latest_json.write_text(json.dumps({"tag_name": "v0.1.4"}), encoding="utf-8")

    install_root = tmp_path / "install-root"
    bin_dir = tmp_path / "bin"
    env = os.environ.copy()
    env.update(
        {
            "KAIST_RELEASE_API_URL": latest_json.resolve().as_uri(),
            "KAIST_DOWNLOAD_BASE_URL": downloads_dir.resolve().as_uri(),
            "KAIST_INSTALL_ROOT": str(install_root),
            "KAIST_BIN_DIR": str(bin_dir),
            "KAIST_PLATFORM_TARGET": "darwin-arm64",
        }
    )

    first = subprocess.run(
        ["bash", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert (install_root / "current").resolve().name == "v0.1.4"
    assert not (install_root / "previous").exists()
    assert (install_root / "current" / "skills" / "kaist-cli" / "SKILL.md").exists()
    assert "Bundled skill:" in first.stdout

    stale = install_root / "versions" / "v0.1.0"
    _populate_bundle_root(stale, "0.1.0")
    latest_json.write_text(json.dumps({"tag_name": "v0.1.5"}), encoding="utf-8")

    second = subprocess.run(
        ["bash", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert (install_root / "current").resolve().name == "v0.1.5"
    assert (install_root / "previous").resolve().name == "v0.1.4"
    assert not stale.exists()
    assert (bin_dir / "kaist").resolve() == (install_root / "current" / "bin" / "kaist").resolve()


def test_perform_self_update_switches_current_and_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_root = tmp_path / "managed-install"
    current_version = install_root / "versions" / "v0.1.4"
    _populate_bundle_root(current_version, "0.1.4")
    (install_root / "current").symlink_to(current_version)
    stale = install_root / "versions" / "v0.1.0"
    _populate_bundle_root(stale, "0.1.0")

    archive_path, checksums_path = _prepare_release_dir(tmp_path / "releases", "v0.1.5")
    monkeypatch.setattr(updater, "platform_target", lambda: "darwin-arm64")
    monkeypatch.setattr(updater, "version_string", lambda: "0.1.4")
    monkeypatch.setattr(
        updater,
        "fetch_latest_release",
        lambda: {
            "tag_name": "v0.1.5",
            "assets": [
                {"name": archive_path.name, "browser_download_url": "archive-url", "size": archive_path.stat().st_size},
                {"name": "checksums.txt", "browser_download_url": "checksums-url", "size": checksums_path.stat().st_size},
            ],
        },
    )
    monkeypatch.setattr(updater, "_current_binary_path", lambda: install_root / "current" / "bin" / "kaist")

    def fake_download(url: str, destination: Path) -> None:
        if url == "archive-url":
            shutil.copy2(archive_path, destination)
            return
        if url == "checksums-url":
            shutil.copy2(checksums_path, destination)
            return
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(updater, "_download_to_path", fake_download)

    payload = updater.perform_self_update()

    assert payload["updated"] is True
    assert payload["bundled_skill_path"] == str(install_root / "current" / "skills" / "kaist-cli")
    assert (install_root / "current").resolve().name == "v0.1.5"
    assert (install_root / "previous").resolve().name == "v0.1.4"
    assert not stale.exists()


def test_prune_versions_returns_warning_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_root = tmp_path / "managed-install"
    current_version = install_root / "versions" / "v0.1.5"
    previous_version = install_root / "versions" / "v0.1.4"
    stale = install_root / "versions" / "v0.1.0"
    _populate_bundle_root(current_version, "0.1.5")
    _populate_bundle_root(previous_version, "0.1.4")
    _populate_bundle_root(stale, "0.1.0")

    ctx = updater.ManagedInstallContext(
        distribution=DistributionInfo(
            distribution="managed-release",
            install_root=install_root,
            bundled_skill_path=current_version / "skills" / "kaist-cli",
            self_update_supported=True,
            release_repo=updater.RELEASE_REPO,
            bundle_root=current_version,
            manifest=BundleManifest(
                version="0.1.5",
                repo=updater.RELEASE_REPO,
                target="darwin-arm64",
                binary_relpath="bin/kaist",
                skill_relpath="skills/kaist-cli",
            ),
        ),
        install_root=install_root,
        bundle_root=current_version,
        manifest=BundleManifest(
            version="0.1.5",
            repo=updater.RELEASE_REPO,
            target="darwin-arm64",
            binary_relpath="bin/kaist",
            skill_relpath="skills/kaist-cli",
        ),
        versions_dir=install_root / "versions",
        current_link=install_root / "current",
        previous_link=install_root / "previous",
    )

    original_rmtree = shutil.rmtree

    def flaky_rmtree(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        if Path(path) == stale:
            raise OSError("blocked")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(updater.shutil, "rmtree", flaky_rmtree)
    warnings = updater._prune_versions(ctx, {current_version, previous_version})  # type: ignore[attr-defined]

    assert warnings
    assert "Could not prune" in warnings[0]
    assert stale.exists()
