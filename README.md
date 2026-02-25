# kaist-cli

CLI for KAIST systems by reverse-engineering official web interfaces.

Current scope:
- KLMS (read-only): courses, assignments, notices, files, snapshot sync, file downloads

## Quick start

```bash
uv sync
uv run playwright install chromium
uv run kaist klms configure --base-url https://klms.kaist.ac.kr
uv run kaist klms login
uv run kaist klms status
uv run kaist klms sync
```

## Storage

By default, data is stored under:
- `~/.kaist-cli/private/klms/` for config/session/snapshot
- `~/.kaist-cli/files/klms/` for downloads

KLMS auth is persisted in:
- `~/.kaist-cli/private/klms/profile/` (preferred, full Playwright browser profile)
- `~/.kaist-cli/private/klms/storage_state.json` (fallback/debug export)

Use `uv run kaist klms status` to inspect active auth mode and cookie expiry estimates.

## Performance

The CLI reuses a single authenticated browser context per command invocation and
fetches per-course work concurrently.

Optional tuning env vars:
- `KAIST_KLMS_CONCURRENCY` (default `4`, range `1..16`)
- `KAIST_KLMS_COURSE_INFO_TTL_SECONDS` (default `21600`)
- `KAIST_KLMS_NOTICE_BOARD_TTL_SECONDS` (default `1800`)

Fast listing option:
- `uv run kaist klms courses --no-enrich`
- Experimental AJAX listing:
  - `uv run kaist klms courses-api --limit 50`

Experimental endpoint discovery:
- `uv run kaist klms discover-api --max-courses 2 --max-notice-boards 2`
- Writes a report to `~/.kaist-cli/private/klms/endpoint_discovery.json`
- Build a categorized map from that report:
  - `uv run kaist klms map-api`
  - Writes `~/.kaist-cli/private/klms/api_map.json`
