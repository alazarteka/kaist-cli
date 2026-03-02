from __future__ import annotations

from kaist_cli import klms


def test_active_auth_mode_prefers_profile_over_storage_state(monkeypatch) -> None:
    monkeypatch.setattr(klms, "_has_profile_session", lambda: True)
    monkeypatch.setattr(klms, "_has_storage_state_session", lambda: True)
    assert klms._active_auth_mode() == "profile"


def test_active_auth_mode_uses_storage_state_when_profile_missing(monkeypatch) -> None:
    monkeypatch.setattr(klms, "_has_profile_session", lambda: False)
    monkeypatch.setattr(klms, "_has_storage_state_session", lambda: True)
    assert klms._active_auth_mode() == "storage_state"


def test_browser_channel_candidates_respect_env_override(monkeypatch) -> None:
    monkeypatch.setenv("KAIST_KLMS_BROWSER_CHANNEL", "chromium")
    assert klms._system_browser_channel_candidates() == ["chromium"]


def test_resolve_system_chromium_executable_from_env(monkeypatch, tmp_path) -> None:
    fake_browser = tmp_path / "fake-browser"
    fake_browser.write_text("", encoding="utf-8")
    fake_browser.chmod(0o755)
    monkeypatch.setenv("KAIST_KLMS_BROWSER_EXECUTABLE", str(fake_browser))
    assert klms._resolve_system_chromium_executable() == str(fake_browser)
