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
