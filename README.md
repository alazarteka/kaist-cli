# kaist-cli

CLI for KAIST systems by reverse-engineering official web interfaces.

Current scope:
- KLMS (read-only): courses, assignments, notices, files, snapshot sync, file downloads

## Spec

- Product spec and roadmap: `docs/PRODUCT_SPEC.md`
- Command migration notes: `docs/MIGRATION.md`
- Release/binary workflow: `docs/RELEASES.md`

## Quick start

```bash
uv sync
uv run playwright install chromium
uv run kaist klms config set --base-url https://klms.kaist.ac.kr
uv run kaist klms auth login
uv run kaist klms auth status
uv run kaist klms sync run
```

## Command model

Stable workflows:
- `kaist version`
- `kaist update --check`
- `kaist update`
- `kaist klms config set|show`
- `kaist klms auth login|status|refresh|doctor`
- `kaist klms list courses [--limit N]`
- `kaist klms list assignments [--course-id <id>] [--since <ISO>] [--limit N]`
- `kaist klms list notices [--notice-board-id <id>] [--max-pages N] [--stop-post-id <id>] [--since <ISO>] [--limit N]`
- `kaist klms list files [--course-id <id>] [--limit N]`
- `kaist klms inbox [--limit N] [--max-notice-pages N] [--since <ISO>]`
- `kaist klms get notice <notice-id> [--notice-board-id <id>] [--include-html]`
- `kaist klms get file <url> [--filename ...] [--subdir ...]`
- `kaist klms sync run [--dry-run] [--max-notice-pages N]`
- `kaist klms sync status|reset`

Portal scaffold:
- `kaist portal status`
- `kaist portal auth status|login`

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
- `--agent` (forces strict JSON envelopes for machine consumers)

Behavior:
- `auto` prints table/text in interactive terminals and JSON in non-interactive runs.
- Use `--format json` for scripting.
- `--agent` returns versioned JSON envelopes and machine-oriented error payloads.

Common non-zero exit codes:
- `10` auth errors
- `20` network timeout/connectivity errors
- `30` parser/API shape drift errors
- `40` config/validation errors
- `50` internal/unknown errors

## Storage

By default, data is stored under:
- `~/.kaist-cli/private/klms/` for config/session/snapshot/cache/discovery artifacts
- `~/.kaist-cli/files/klms/` for downloads

When downloading into a course folder (for example `--subdir 180871`),
the CLI also writes `COURSE.md` under `~/.kaist-cli/files/klms/<course-id>/`
with best-effort course metadata (term, title, code, professors, URL).

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

## Binary Install and Update

Planned release path uses unsigned standalone binaries published on GitHub Releases.

Manual install (example for macOS Apple Silicon):

```bash
TAG=v0.1.1
REPO=alazarteka/kaist-cli
TARGET=darwin-arm64
ASSET="kaist-${TAG}-${TARGET}.tar.gz"
curl -fL -o "${ASSET}" "https://github.com/${REPO}/releases/download/${TAG}/${ASSET}"
curl -fL -o checksums.txt "https://github.com/${REPO}/releases/download/${TAG}/checksums.txt"
shasum -a 256 "${ASSET}"
tar -xzf "${ASSET}"
mkdir -p ~/.local/bin
install -m 755 kaist ~/.local/bin/kaist
```

Check latest available release:

```bash
kaist update --check
```

Install update (standalone binary runtime only):

```bash
kaist update
```

Note:
- `kaist update` installs by replacing the running standalone binary.
- Source-based runs (`uv run`, editable installs) support `--check` but not binary replacement.
- Release source is fixed to `alazarteka/kaist-cli`.

## Tests and Smoke

Unit contract tests:

```bash
uv run --with pytest pytest -q
```

Live smoke script:

```bash
./scripts/smoke.sh
```
