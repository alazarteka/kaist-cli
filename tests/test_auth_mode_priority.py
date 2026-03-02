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
