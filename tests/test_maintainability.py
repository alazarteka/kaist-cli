from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_main_entrypoint_stays_thin() -> None:
    path = ROOT / "src" / "kaist_cli" / "main.py"
    assert _line_count(path) <= 120


def test_cli_parser_module_stays_reasonably_sized() -> None:
    path = ROOT / "src" / "kaist_cli" / "cli" / "parser.py"
    assert _line_count(path) <= 220


def test_adapter_registry_is_explicit() -> None:
    path = ROOT / "src" / "kaist_cli" / "core" / "system_registry.py"
    text = path.read_text(encoding="utf-8")
    assert "VersionAdapter" in text
    assert "UpdateAdapter" in text
    assert "KlmsAdapter" in text
    assert "PortalAdapter" in text


def test_legacy_klms_module_is_removed() -> None:
    path = ROOT / "src" / "kaist_cli" / "klms.py"
    assert not path.exists()


def test_v2_auth_module_does_not_import_playwright_at_module_load() -> None:
    path = ROOT / "src" / "kaist_cli" / "v2" / "klms" / "auth.py"
    text = path.read_text(encoding="utf-8")
    module_header = text.split("def install_browser", maxsplit=1)[0]
    assert "from playwright.async_api import async_playwright" not in module_header
    assert "from playwright.sync_api import sync_playwright" not in module_header


def test_klms_adapter_uses_v2_dispatch_only() -> None:
    path = ROOT / "src" / "kaist_cli" / "systems" / "klms" / "adapter.py"
    text = path.read_text(encoding="utf-8")
    assert "dispatch_v2" in text
    assert "register_klms_parser" in text
    assert "legacy" not in text.lower()
