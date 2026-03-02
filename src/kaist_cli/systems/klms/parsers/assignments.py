from __future__ import annotations

from .... import klms as legacy


def parse_datetime_guess(raw: str) -> str | None:
    return legacy._parse_datetime_guess(raw)  # type: ignore[attr-defined]


def extract_assignment_rows_from_calendar_data(data: object, *, base_url: str) -> list[dict[str, object]]:
    return legacy._extract_assignment_rows_from_calendar_data(data, base_url=base_url)  # type: ignore[attr-defined]
