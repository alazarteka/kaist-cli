# kaist-cli

CLI for KAIST systems, currently centered on KLMS.

Current KLMS surface:
- auth/session management
- `today`, `week`, and `inbox`
- courses, assignments, notices, files, and videos
- `courses resolve` and authenticated raw `request get`
- local cache prewarm via `sync`
- file download and bulk `files pull`

## Docs

- Product spec: `docs/PRODUCT_SPEC.md`
- KLMS migration notes: `docs/MIGRATION.md`
- Release workflow: `docs/RELEASES.md`
- CLI performance notes: `docs/PERFORMANCE.md`
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

Optional agent skill install helpers:
- `kaist agent install codex`
- `kaist agent install claude`
- `kaist agent install gemini`
- `kaist agent install custom --path /target/dir`
- `kaist agent status`
- `kaist agent uninstall TARGET` removes only a managed symlink or marked copy. Use `--dry-run` to preview; legacy copies require `--confirm /exact/target/path`.

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

Email OTP setup uses a safer two-step flow by default:

```bash
# Save non-secret config
uv run kaist klms auth setup-email-otp --base-url https://klms.kaist.ac.kr --username KAIST_ID

# In a separate human-run terminal, store the password in macOS Keychain
uv run kaist klms auth store-email-otp-secret --username KAIST_ID

# Later refreshes can run headlessly and wait for the email OTP
uv run kaist klms auth refresh
```

For staged email-OTP completion, prefer `kaist klms auth complete-refresh SESSION_ID --otp-stdin` (one code line on stdin). An interactive terminal may use the hidden prompt; `--no-input` fails quickly when no code is available. The legacy `--otp CODE` flag remains for compatibility but exposes the code in shell history and process listings.

## KLMS Commands

Stable workflows:
- `kaist klms auth login|install-browser|status|refresh|doctor`
- `kaist klms auth setup-email-otp|store-email-otp-secret|clear-email-otp-secret|begin-refresh|complete-refresh|cancel-refresh`
- `kaist klms today [--limit N] [--window-days N] [--notice-days N]`
- `kaist klms week [--limit N]`
- `kaist klms inbox [--limit N] [--since <ISO>]`
- `kaist klms sync run|status|reset`
- `kaist klms courses list|resolve|show`
- `kaist klms assignments list|show`
- `kaist klms notices list|show`
- `kaist klms files list|get|download|pull`
- `kaist klms videos list|show`
- `kaist klms request get TARGET`
- `kaist klms dev plan|probe|discover`

Other systems:
- `kaist version`
- `kaist --version` (also `kaist version --json` or `kaist version --format json`)
- `kaist update --check`
- `kaist update`
- `kaist agent install|status|uninstall`

## Output Modes

Global output flags:
- `--format auto|json|table|text`
- `--agent`

Behavior:
- `auto` prints table/text in interactive terminals and JSON otherwise.
- `--format json` is good for scripts.
- `--agent` is a global flag and returns strict JSON envelopes with stable schema names such as `kaist.klms.today.v1`.
- use it like `kaist --agent klms today` or `kaist --agent version`

Structured output is a compatibility contract. Successful results and dispatched operational errors are JSON on stdout; operational errors keep a nonzero exit status. Do not move those errors to stderr in automation. Invalid `--agent` command lines (including missing operands, invalid choices, and numeric validation failures) return one `kaist.cli.parse.v1` JSON error envelope on stdout with exit status 2 and no traceback/plaintext mixed into stdout. Human-mode argparse errors remain conventional.

Numeric limits, page counts, windows, discovery counts, timeouts, and preview sizes must be greater than zero when supplied. Download `--filename` values are basename-only and cannot contain path separators or controls; remote suggested names are reduced safely to a basename. Use `--dest` or `--subdir` for placement.

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

See `docs/PERFORMANCE.md` for where commands spend time and what still dominates wall clock.

Operational controls:
- `KAIST_KLMS_CONCURRENCY` for concurrent course/board HTML fetches (default `4`, max `32`)
- `KAIST_KLMS_BROWSER_CHANNEL` to prefer a system browser channel
- `KAIST_KLMS_BROWSER_EXECUTABLE` to force a browser executable path

Warm interactive commands after a cache prewarm:

```bash
kaist klms sync run
kaist klms today
```

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
