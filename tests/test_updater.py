from __future__ import annotations

import pytest

from kaist_cli.core import updater


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
