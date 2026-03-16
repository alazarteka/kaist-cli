from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


RELEASE_REPO = "alazarteka/kaist-cli"
SKILL_NAME = "kaist-cli"
BUNDLE_FILENAME = "bundle.json"


@dataclass(frozen=True)
class BundleManifest:
    version: str
    repo: str
    target: str
    binary_relpath: str
    skill_relpath: str


@dataclass(frozen=True)
class DistributionInfo:
    distribution: str
    install_root: Path | None
    bundled_skill_path: Path | None
    self_update_supported: bool
    release_repo: str
    bundle_root: Path | None = None
    manifest: BundleManifest | None = None

    def as_payload(self) -> dict[str, str | bool | None]:
        return {
            "distribution": self.distribution,
            "install_root": str(self.install_root) if self.install_root else None,
            "bundled_skill_path": str(self.bundled_skill_path) if self.bundled_skill_path else None,
            "self_update_supported": self.self_update_supported,
            "release_repo": self.release_repo,
        }


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def repo_skill_path() -> Path:
    return repo_root() / "skills" / SKILL_NAME


def load_bundle_manifest(bundle_root: Path) -> BundleManifest | None:
    manifest_path = bundle_root / BUNDLE_FILENAME
    if not manifest_path.exists():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None

    version = str(raw.get("version") or "").strip()
    repo = str(raw.get("repo") or "").strip()
    target = str(raw.get("target") or "").strip()
    binary_relpath = str(raw.get("binary_relpath") or "").strip()
    skill_relpath = str(raw.get("skill_relpath") or "").strip()
    if not version or not repo or not target or not binary_relpath or not skill_relpath:
        return None
    return BundleManifest(
        version=version,
        repo=repo,
        target=target,
        binary_relpath=binary_relpath,
        skill_relpath=skill_relpath,
    )


def _install_root_from_bundle_root(bundle_root: Path) -> Path | None:
    if bundle_root.name in {"current", "previous"}:
        return bundle_root.parent
    if bundle_root.parent.name == "versions":
        return bundle_root.parent.parent
    return None


def _distribution_from_bundle_root(bundle_root: Path) -> DistributionInfo | None:
    manifest = load_bundle_manifest(bundle_root)
    if manifest is None:
        return None

    skill_path = bundle_root / manifest.skill_relpath
    install_root = _install_root_from_bundle_root(bundle_root)
    managed = bool(install_root and (install_root / "versions").exists())
    return DistributionInfo(
        distribution="managed-release" if managed else "standalone-binary",
        install_root=install_root if managed else None,
        bundled_skill_path=skill_path if skill_path.exists() else None,
        self_update_supported=managed,
        release_repo=manifest.repo or RELEASE_REPO,
        bundle_root=bundle_root,
        manifest=manifest,
    )


def _discover_from_executable(executable: Path) -> DistributionInfo | None:
    candidates: list[Path] = []
    for candidate in (executable, executable.resolve()):
        parent = candidate.parent
        candidates.extend([parent, *parent.parents])

    seen: set[Path] = set()
    for bundle_root in candidates:
        try:
            resolved_root = bundle_root.resolve()
        except OSError:
            resolved_root = bundle_root
        if resolved_root in seen:
            continue
        seen.add(resolved_root)

        if not (bundle_root / BUNDLE_FILENAME).exists():
            continue
        info = _distribution_from_bundle_root(bundle_root)
        if info is not None:
            return info
    return None


def discover_distribution_info(
    *,
    executable: str | Path | None = None,
    frozen: bool | None = None,
) -> DistributionInfo:
    frozen_flag = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    executable_path = Path(executable or sys.executable)

    if frozen_flag:
        discovered = _discover_from_executable(executable_path)
        if discovered is not None:
            return discovered
        return DistributionInfo(
            distribution="standalone-binary",
            install_root=None,
            bundled_skill_path=None,
            self_update_supported=False,
            release_repo=RELEASE_REPO,
        )

    root = repo_root()
    source_skill = repo_skill_path()
    if (root / "pyproject.toml").exists() and (root / "src" / "kaist_cli" / "main.py").exists():
        return DistributionInfo(
            distribution="source",
            install_root=root,
            bundled_skill_path=source_skill if source_skill.exists() else None,
            self_update_supported=False,
            release_repo=RELEASE_REPO,
        )
    return DistributionInfo(
        distribution="python-install",
        install_root=None,
        bundled_skill_path=None,
        self_update_supported=False,
        release_repo=RELEASE_REPO,
    )
