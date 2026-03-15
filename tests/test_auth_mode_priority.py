from __future__ import annotations

from kaist_cli.v2.klms import auth


def test_active_auth_mode_prefers_profile_over_storage_state(monkeypatch) -> None:
    monkeypatch.setattr(auth, "has_profile_session", lambda paths: True)
    monkeypatch.setattr(auth, "has_storage_state_session", lambda paths: True)
    assert auth.active_auth_mode(object()) == "profile"


def test_active_auth_mode_uses_storage_state_when_profile_missing(monkeypatch) -> None:
    monkeypatch.setattr(auth, "has_profile_session", lambda paths: False)
    monkeypatch.setattr(auth, "has_storage_state_session", lambda paths: True)
    assert auth.active_auth_mode(object()) == "storage_state"


def test_browser_channel_candidates_respect_env_override(monkeypatch) -> None:
    monkeypatch.setenv("KAIST_KLMS_BROWSER_CHANNEL", "chromium")
    assert auth._system_browser_channel_candidates() == ["chromium"]


def test_resolve_system_chromium_executable_from_env(monkeypatch, tmp_path) -> None:
    fake_browser = tmp_path / "fake-browser"
    fake_browser.write_text("", encoding="utf-8")
    fake_browser.chmod(0o755)
    monkeypatch.setenv("KAIST_KLMS_BROWSER_EXECUTABLE", str(fake_browser))
    assert auth._resolve_system_chromium_executable() == str(fake_browser)
