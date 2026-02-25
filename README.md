# kaist-cli

CLI for KAIST systems by reverse-engineering official web interfaces.

Current scope:
- KLMS (read-only): courses, assignments, notices, files, snapshot sync, file downloads

## Quick start

```bash
uv sync
uv run playwright install chromium
uv run kaist klms config set --base-url https://klms.kaist.ac.kr
uv run kaist klms auth login
uv run kaist klms auth status
uv run kaist klms sync
```

## Command model

Stable workflows:
- `kaist klms config set|show`
- `kaist klms auth login|status`
- `kaist klms courses`
- `kaist klms assignments [--course-id <id>]`
- `kaist klms notices [--notice-board-id <id>] [--max-pages N] [--stop-post-id <id>]`
- `kaist klms files [--course-id <id>]`
- `kaist klms download <url> [--filename ...] [--subdir ...]`
- `kaist klms sync [--dry-run] [--max-notice-pages N]`

Experimental/debug flows:
- `kaist klms dev fetch-html <path-or-url>`
- `kaist klms dev extract <path-or-url> <regex>`
- `kaist klms dev courses-api [--limit N]`
- `kaist klms dev term`
- `kaist klms dev course-info <course-id>`
- `kaist klms dev discover-api [--max-courses N] [--max-notice-boards N]`
- `kaist klms dev map-api [--report-path ...]`

## Output modes

Global output flag:
- `--format auto|json|table|text`

Behavior:
- `auto` prints table/text in interactive terminals and JSON in non-interactive runs.
- Use `--format json` for scripting.

## Storage

By default, data is stored under:
- `~/.kaist-cli/private/klms/` for config/session/snapshot/cache/discovery artifacts
- `~/.kaist-cli/files/klms/` for downloads

KLMS auth is persisted in:
- `~/.kaist-cli/private/klms/profile/` (preferred, full Playwright browser profile)
- `~/.kaist-cli/private/klms/storage_state.json` (fallback/debug export)

## Performance

The CLI reuses a single authenticated browser context per command invocation and
fetches per-course work concurrently.

Optional tuning env vars:
- `KAIST_KLMS_CONCURRENCY` (default `4`, range `1..16`)
- `KAIST_KLMS_COURSE_INFO_TTL_SECONDS` (default `21600`)
- `KAIST_KLMS_NOTICE_BOARD_TTL_SECONDS` (default `1800`)
