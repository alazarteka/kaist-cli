from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from kaist_cli.core import updater
from kaist_cli.core.distribution import BundleManifest, DistributionInfo, discover_distribution_info


def test_platform_target_mapping() -> None:
    assert updater._platform_target("Darwin", "arm64") == "darwin-arm64"  # type: ignore[attr-defined]
    assert updater._platform_target("darwin", "x86_64") == "darwin-x86_64"  # type: ignore[attr-defined]


def test_platform_target_mapping_linux_glibc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "_linux_libc_kind", lambda: "glibc")
    assert updater._platform_target("linux", "amd64") == "linux-x86_64-gnu"  # type: ignore[attr-defined]


def test_platform_target_mapping_linux_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "_linux_libc_kind", lambda: "musl")
    with pytest.raises(updater.SelfUpdateError):
        updater._platform_target("Linux", "aarch64")  # type: ignore[attr-defined]
    with pytest.raises(updater.SelfUpdateError):
        updater._platform_target("linux", "amd64")  # type: ignore[attr-defined]


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
        updater.ReleaseAsset(name="kaist-v0.1.0-linux-x86_64-gnu.tar.gz", browser_download_url="u1", size=1),
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


def _prepare_release_dir(
    base_dir: Path,
    version: str,
    *,
    target: str = "darwin-arm64",
    bundle_mode: str = "onedir",
) -> tuple[Path, Path]:
    tag = version if version.startswith("v") else f"v{version}"
    release_dir = base_dir / tag
    release_dir.mkdir(parents=True, exist_ok=True)
    binary_path = base_dir / f"kaist-{tag}-{target}"
    if bundle_mode == "onedir":
        binary_path.mkdir(parents=True, exist_ok=True)
        _write_fake_binary(binary_path / "kaist")
        (binary_path / "_internal").mkdir(parents=True, exist_ok=True)
        (binary_path / "_internal" / "runtime.txt").write_text("runtime\n", encoding="utf-8")
    else:
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
            target,
            "--out-dir",
            str(release_dir),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert cp.returncode == 0, cp.stderr
    archive_path = release_dir / f"kaist-{tag}-{target}.tar.gz"
    checksums_path = release_dir / "checksums.txt"
    checksums_path.write_text(
        f"{updater._sha256(archive_path)}  {archive_path.name}\n",
        encoding="utf-8",
    )
    return archive_path, checksums_path


def _populate_bundle_root(bundle_root: Path, version: str, *, binary_relpath: str = "bin/kaist") -> None:
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "skills").mkdir(parents=True, exist_ok=True)
    binary_path = bundle_root / binary_relpath
    binary_path.parent.mkdir(parents=True, exist_ok=True)
    _write_fake_binary(binary_path)
    shutil.copytree(ROOT / "skills" / "kaist-cli", bundle_root / "skills" / "kaist-cli")
    (bundle_root / "bundle.json").write_text(
        json.dumps(
            {
                "version": version.lstrip("v"),
                "repo": updater.RELEASE_REPO,
                "target": "darwin-arm64",
                "binary_relpath": binary_relpath,
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


@pytest.mark.parametrize(
    ("target", "bundle_mode", "binary_relpath"),
    [
        ("darwin-arm64", "onedir", "bin/kaist/kaist"),
        ("darwin-x86_64", "onedir", "bin/kaist/kaist"),
        ("linux-x86_64-gnu", "onedir", "bin/kaist/kaist"),
        ("darwin-arm64", "onefile", "bin/kaist"),
    ],
)
def test_build_release_bundle_contains_skill_and_manifest(
    tmp_path: Path,
    target: str,
    bundle_mode: str,
    binary_relpath: str,
) -> None:
    archive_path, _ = _prepare_release_dir(tmp_path / "releases", "v0.1.4", target=target, bundle_mode=bundle_mode)

    with tarfile.open(archive_path, "r:gz") as tar:
        names = {name.lstrip("./") for name in tar.getnames()}
        assert "bundle.json" in names
        assert binary_relpath in names
        assert "skills/kaist-cli/SKILL.md" in names
        assert "skills/kaist-cli/agents/openai.yaml" in names
        assert "skills/kaist-cli/.claude-plugin/plugin.json" in names
        assert "skills/kaist-cli/.claude-plugin/marketplace.json" in names
        manifest = json.loads(tar.extractfile("bundle.json").read().decode("utf-8"))  # type: ignore[union-attr]
        bundled_marketplace = json.loads(tar.extractfile("skills/kaist-cli/.claude-plugin/marketplace.json").read().decode("utf-8"))  # type: ignore[union-attr]

    assert manifest == {
        "version": "0.1.4",
        "repo": updater.RELEASE_REPO,
        "target": target,
        "binary_relpath": binary_relpath,
        "skill_relpath": "skills/kaist-cli",
    }
    assert bundled_marketplace["plugins"][0]["version"] == "0.1.4"


@pytest.mark.parametrize("target", ["darwin-arm64", "darwin-x86_64"])
def test_install_script_installs_managed_layout_and_rotates_previous(tmp_path: Path, target: str) -> None:
    downloads_dir = tmp_path / "downloads"
    _prepare_release_dir(downloads_dir, "v0.1.4", target=target, bundle_mode="onedir")
    _prepare_release_dir(downloads_dir, "v0.1.5", target=target, bundle_mode="onedir")
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
            "KAIST_PLATFORM_TARGET": target,
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
    assert json.loads((install_root / "current" / "skills" / "kaist-cli" / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))["plugins"][0]["version"] == "0.1.4"
    assert json.loads((install_root / "mkt" / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))["plugins"][0]["version"] == "0.1.4"
    assert (install_root / "mkt" / "plugins" / "kaist-cli" / "skills" / "kaist-cli").is_symlink()
    assert "Bundled skill:" in first.stdout
    assert (bin_dir / "kaist").resolve() == (install_root / "current" / "bin" / "kaist" / "kaist").resolve()

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
    assert (bin_dir / "kaist").resolve() == (install_root / "current" / "bin" / "kaist" / "kaist").resolve()
    assert json.loads((install_root / "current" / "skills" / "kaist-cli" / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))["plugins"][0]["version"] == "0.1.5"
    assert json.loads((install_root / "mkt" / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))["plugins"][0]["version"] == "0.1.5"


def test_perform_self_update_switches_current_and_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_root = tmp_path / "managed-install"
    current_version = install_root / "versions" / "v0.1.4"
    _populate_bundle_root(current_version, "0.1.4")
    (install_root / "current").symlink_to(current_version)
    stale = install_root / "versions" / "v0.1.0"
    _populate_bundle_root(stale, "0.1.0")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher_link = bin_dir / "kaist"
    launcher_link.symlink_to(install_root / "current" / "bin" / "kaist")

    archive_path, checksums_path = _prepare_release_dir(tmp_path / "releases", "v0.1.5", bundle_mode="onedir")
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
    monkeypatch.setattr(updater, "_current_binary_path", lambda: launcher_link)

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
    assert payload["claude_marketplace_path"] == str(install_root / "mkt" / ".claude-plugin" / "marketplace.json")
    assert (install_root / "current").resolve().name == "v0.1.5"
    assert (install_root / "previous").resolve().name == "v0.1.4"
    assert not stale.exists()
    assert payload["binary_path"] == str(install_root / "current" / "bin" / "kaist" / "kaist")
    assert payload["launcher_path"] == str(launcher_link)
    assert launcher_link.resolve() == (install_root / "current" / "bin" / "kaist" / "kaist").resolve()
    assert json.loads((install_root / "mkt" / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))["plugins"][0]["version"] == "0.1.5"


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


def test_distribution_discovers_managed_onedir_bundle(tmp_path: Path) -> None:
    install_root = tmp_path / "managed-install"
    version_root = install_root / "versions" / "v0.1.5"
    _populate_bundle_root(version_root, "0.1.5", binary_relpath="bin/kaist/kaist")
    (install_root / "current").symlink_to(version_root)

    info = discover_distribution_info(
        executable=install_root / "current" / "bin" / "kaist" / "kaist",
        frozen=True,
    )

    assert info.distribution == "managed-release"
    assert info.install_root == install_root
    assert info.bundle_root == install_root / "current"
    assert info.manifest is not None
    assert info.manifest.binary_relpath == "bin/kaist/kaist"


def test_install_script_detects_linux_glibc_target(tmp_path: Path) -> None:
    downloads_dir = tmp_path / "downloads"
    _prepare_release_dir(downloads_dir, "v0.1.4", target="linux-x86_64-gnu", bundle_mode="onedir")
    latest_json = tmp_path / "latest.json"
    latest_json.write_text(json.dumps({"tag_name": "v0.1.4"}), encoding="utf-8")

    install_root = tmp_path / "install-root"
    bin_dir = tmp_path / "bin"
    shims_dir = tmp_path / "shims"
    shims_dir.mkdir()
    (shims_dir / "uname").write_text(
        "#!/usr/bin/env bash\nif [[ \"$1\" == \"-s\" ]]; then echo Linux; elif [[ \"$1\" == \"-m\" ]]; then echo x86_64; else /usr/bin/uname \"$@\"; fi\n",
        encoding="utf-8",
    )
    (shims_dir / "getconf").write_text(
        "#!/usr/bin/env bash\nif [[ \"$1\" == \"GNU_LIBC_VERSION\" ]]; then echo 'glibc 2.35'; else /usr/bin/getconf \"$@\"; fi\n",
        encoding="utf-8",
    )
    os.chmod(shims_dir / "uname", 0o755)
    os.chmod(shims_dir / "getconf", 0o755)

    env = os.environ.copy()
    env.update(
        {
            "KAIST_RELEASE_API_URL": latest_json.resolve().as_uri(),
            "KAIST_DOWNLOAD_BASE_URL": downloads_dir.resolve().as_uri(),
            "KAIST_INSTALL_ROOT": str(install_root),
            "KAIST_BIN_DIR": str(bin_dir),
            "PATH": f"{shims_dir}:{env['PATH']}",
        }
    )

    result = subprocess.run(
        ["bash", str(ROOT / "install.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (install_root / "current").resolve().name == "v0.1.4"
    assert (bin_dir / "kaist").resolve() == (install_root / "current" / "bin" / "kaist" / "kaist").resolve()
