# kaist-cli

CLI for KAIST systems, currently centered on KLMS.

Current KLMS surface:
- auth/session management
- `today` and `inbox`
- courses, assignments, notices, files, and videos
- local cache prewarm via `sync`
- file download and bulk `files pull`

## Docs

- Product spec: `docs/PRODUCT_SPEC.md`
- KLMS migration notes: `docs/MIGRATION.md`
- Release workflow: `docs/RELEASES.md`
- Historical rewrite RFC: `docs/CLEAN_BREAK_RFC.md`

## Install

Managed release install on:
- macOS arm64
- macOS x86_64
- Linux x86_64 glibc (Ubuntu/Debian-class)

```bash
curl -fsSL https://raw.githubusercontent.com/alazarteka/kaist-cli/main/install.sh | bash
kaist version
kaist --help
```

This installs:
- `kaist` at `~/.local/bin/kaist`
- managed release bundles under `~/.local/share/kaist-cli/`
- a bundled agent skill at `~/.local/share/kaist-cli/current/skills/kaist-cli`

Use `kaist update --check` and `kaist update` to manage release updates.

Linux note:
- published standalone bundles support `x86_64` glibc hosts only
- Alpine/musl is not supported
- headless Linux should use `kaist klms auth login --username ...` or `kaist klms auth refresh`
- manual browser login on a headless host is not supported

## Source Quick Start

```bash
uv sync
uv run kaist klms auth install-browser
uv run kaist klms auth login --base-url https://klms.kaist.ac.kr
uv run kaist klms auth status
uv run kaist klms today
uv run kaist klms sync run
```

## KLMS Commands

Stable workflows:
- `kaist klms auth login|install-browser|status|refresh|doctor`
- `kaist klms today [--limit N] [--window-days N] [--notice-days N]`
- `kaist klms inbox [--limit N] [--since <ISO>]`
- `kaist klms sync run|status|reset`
- `kaist klms courses list|show`
- `kaist klms assignments list|show`
- `kaist klms notices list|show`
- `kaist klms files list|get|download|pull`
- `kaist klms videos list|show`
- `kaist klms dev plan|probe|discover`

Other systems:
- `kaist version`
- `kaist update --check`
- `kaist update`

## Output Modes

Global output flags:
- `--format auto|json|table|text`
- `--agent`

Behavior:
- `auto` prints table/text in interactive terminals and JSON otherwise.
- `--format json` is good for scripts.
- `--agent` is a global flag and returns strict JSON envelopes with stable schema names such as `kaist.klms.today.v1`.
- use it like `kaist --agent klms today` or `kaist --agent version`

Common non-zero exit codes:
- `10` auth errors
- `20` network timeout/connectivity errors
- `30` parser/API shape drift
- `40` config errors
- `50` internal/unknown errors

## Storage

By default, KLMS state is stored under:
- `~/.kaist-cli/private/klms/` for config, auth, cache, and discovery artifacts
- `~/.kaist-cli/files/klms/` for downloaded files

KLMS auth is persisted in:
- `~/.kaist-cli/private/klms/profile/`
- `~/.kaist-cli/private/klms/storage_state.json`
- `~/.kaist-cli/private/klms/playwright-browsers/`

## Performance

The CLI reuses a single authenticated browser context per command and caches slow HTML-backed providers aggressively.

Operational controls:
- `KAIST_KLMS_CONCURRENCY` for concurrent course/board HTML fetches
- `KAIST_KLMS_BROWSER_CHANNEL` to prefer a system browser channel
- `KAIST_KLMS_BROWSER_EXECUTABLE` to force a browser executable path

## Release

Managed standalone bundles are published through GitHub Releases.

Current published targets:
- `darwin-arm64`
- `darwin-x86_64`
- `linux-x86_64-gnu`

Check latest release:

```bash
kaist update --check
```

Install update when running a managed standalone install:

```bash
kaist update
```

## Tests

```bash
uv run --with pytest pytest -q
```

Live smoke:

```bash
./scripts/smoke.sh
```
