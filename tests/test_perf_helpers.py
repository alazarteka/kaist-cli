from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kaist_cli.v2.klms.cache import load_cache_entry, save_cache_value
from kaist_cli.v2.klms.paths import resolve_paths
from kaist_cli.v2.klms.session import KlmsHttpResponse, KlmsHttpSession, fetch_html_batch, http_max_workers


def test_http_max_workers_defaults_and_env(monkeypatch) -> None:
    monkeypatch.delenv("KAIST_KLMS_CONCURRENCY", raising=False)
    assert http_max_workers() == 4
    assert http_max_workers(8) == 8

    monkeypatch.setenv("KAIST_KLMS_CONCURRENCY", "12")
    assert http_max_workers() == 12
    assert http_max_workers(4) == 12

    monkeypatch.setenv("KAIST_KLMS_CONCURRENCY", "0")
    assert http_max_workers() == 1

    monkeypatch.setenv("KAIST_KLMS_CONCURRENCY", "999")
    assert http_max_workers() == 32

    monkeypatch.setenv("KAIST_KLMS_CONCURRENCY", "nope")
    assert http_max_workers(6) == 6


def test_cache_reuses_in_memory_snapshot_and_writes_compact_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path))
    paths = resolve_paths()
    save_cache_value(paths, "notice-list::demo", [{"id": "1"}], ttl_seconds=60)
    raw = paths.cache_path.read_text(encoding="utf-8")
    assert "\n  " not in raw
    payload = json.loads(raw)
    assert "notice-list::demo" in payload["entries"]

    reads: list[str] = []
    original_read_text = Path.read_text

    def tracking_read_text(self: Path, *args, **kwargs):
        if self == paths.cache_path:
            reads.append(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracking_read_text)
    assert load_cache_entry(paths, "notice-list::demo") is not None
    assert load_cache_entry(paths, "notice-list::demo") is not None
    assert reads == []

    # External rewrite must invalidate the snapshot.
    payload["entries"]["notice-list::demo"]["expires_at"] = 0
    paths.cache_path.write_text(json.dumps(payload), encoding="utf-8")
    entry = load_cache_entry(paths, "notice-list::demo")
    assert entry is not None
    assert entry["stale"] is True
    assert reads == [str(paths.cache_path)]


class _FakeContext:
    def storage_state(self) -> dict:
        return {
            "cookies": [
                {
                    "name": "MoodleSession",
                    "value": "abc",
                    "domain": "klms.kaist.ac.kr",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        }


def test_http_session_reuses_thread_local_opener() -> None:
    session = KlmsHttpSession(_FakeContext(), base_url="https://klms.kaist.ac.kr")
    first = session._opener()
    second = session._opener()
    assert first is second
    rebuilt = session._build_opener()
    assert rebuilt is not first


def test_fetch_html_batch_retries_failed_paths_with_browser_context(monkeypatch) -> None:
    session = KlmsHttpSession(_FakeContext(), base_url="https://klms.kaist.ac.kr")
    context = _FakeContext()
    calls: list[tuple[str, bool]] = []

    def fake_get_html(
        url_or_path: str,
        *,
        context: Any | None = None,
        timeout_seconds: float = 20.0,  # noqa: ARG001
    ) -> KlmsHttpResponse:
        calls.append((url_or_path, context is not None))
        if context is None and url_or_path.endswith("id=2"):
            raise TimeoutError("http boom")
        via = "browser" if context is not None else "http"
        return KlmsHttpResponse(url=f"https://klms.kaist.ac.kr{url_or_path}", text=f"ok:{url_or_path}", via=via)

    monkeypatch.setattr(session, "get_html", fake_get_html)
    results = fetch_html_batch(
        session,
        ["/mod/assign/index.php?id=1", "/mod/assign/index.php?id=2"],
        max_workers=2,
        context=context,
    )
    assert set(results) == {"/mod/assign/index.php?id=1", "/mod/assign/index.php?id=2"}
    assert results["/mod/assign/index.php?id=2"].via == "browser"
    assert ("/mod/assign/index.php?id=2", False) in calls
    assert ("/mod/assign/index.php?id=2", True) in calls


def test_fetch_html_batch_without_context_still_raises_on_http_failure(monkeypatch) -> None:
    session = KlmsHttpSession(_FakeContext(), base_url="https://klms.kaist.ac.kr")

    def fake_get_html(
        url_or_path: str,
        *,
        context: Any | None = None,  # noqa: ARG001
        timeout_seconds: float = 20.0,  # noqa: ARG001
    ) -> KlmsHttpResponse:
        raise RuntimeError(f"http boom:{url_or_path}")

    monkeypatch.setattr(session, "get_html", fake_get_html)
    with pytest.raises(RuntimeError, match="http boom"):
        fetch_html_batch(session, ["/mod/assign/index.php?id=1"], max_workers=1)
