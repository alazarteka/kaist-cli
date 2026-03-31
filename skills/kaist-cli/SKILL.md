---
name: kaist-cli
description: Use the installed `kaist` CLI to interact with KAIST's Learning Management System (KLMS). Reach for this skill whenever the user needs course data, assignments, notices, files, videos, or any KLMS information — even if they don't mention "kaist" or "KLMS" explicitly. Covers auth setup, daily summaries, resource drill-down, file downloads, cache management, and error recovery. Prefer this CLI over direct scraping or browser automation.
---

# KAIST CLI — Agent Integration Guide

The `kaist` CLI is the primary automation surface for KLMS. It handles authentication, data retrieval, file downloads, and cache management. Use it instead of scraping KLMS pages directly.

## Agent mode

Pass `--agent` as a **global flag before the subcommand** to get structured JSON envelopes:

- Use the pattern `kaist --agent ...`, not `kaist ... --agent`.

```
kaist --agent klms today
kaist --agent klms assignments list --course "CS101"
kaist --agent version
```

Every `--agent` response follows this shape:

```json
{
  "schema": "kaist.klms.today.v1",
  "ok": true,
  "generated_at": "2026-03-18T12:00:00Z",
  "meta": {
    "command": "klms today",
    "source": "mixed",
    "capability": "full"
  },
  "data": { ... }
}
```

Key envelope fields are `ok`, `schema`, `meta`, and `data`.

On failure, `ok` is `false` and `error` replaces `data`:

```json
{
  "ok": false,
  "error": {
    "code": "AUTH_EXPIRED",
    "message": "Login state not found",
    "retryable": true,
    "hint": "kaist klms auth login"
  }
}
```

### Trust signals in `meta`

- **`source`** — where the data came from: `cache`, `html`, `browser`, `moodle_ajax`, `mixed`, etc. Useful for judging freshness.
- **`capability`** — how complete the result is:
  - `full` — provider-backed, strong coverage
  - `partial` — valid but with fallback or provider limits
  - `degraded` — stale cache, partial refresh failure, or warnings present

Treat `degraded` results with caution even when `ok` is `true`.

## Concurrency caution

Avoid running multiple auth-using `kaist` commands in parallel. The CLI shares browser/profile state for KLMS auth, so overlapping commands can interfere with each other. Prefer one command at a time when a workflow touches auth, downloads, sync, or other live KLMS operations.

## Auth prerequisites

Auth requires a Playwright browser. This is a one-time setup, then periodic refreshes.
Install it once with `kaist klms auth install-browser`.

```bash
# 1. Install the browser engine (once)
kaist klms auth install-browser

# 2a. Interactive browser login
kaist klms auth login --base-url https://klms.kaist.ac.kr

# 2b. Non-interactive Easy Login (prints approval number for KAIST auth app)
kaist klms auth login --base-url https://klms.kaist.ac.kr --username KAIST_ID

# 3. Renew session (reuses saved username if available)
kaist klms auth refresh

# 4. Diagnostics
kaist klms auth status
kaist klms auth doctor
```

The manual browser login (no `--username`) requires an interactive terminal with a display and is not suitable for headless or non-interactive shells. The Easy Login flow (`--username`) runs headless and works in non-interactive shells — it just needs the user to approve the push notification in the KAIST auth app. Once logged in, `auth refresh` handles renewal automatically when a username was saved.
The explicit non-interactive setup command is `kaist klms auth login --base-url https://klms.kaist.ac.kr --username KAIST_ID`.

### Agent auth recovery behavior

When a KLMS task fails with `AUTH_MISSING` or `AUTH_EXPIRED`, prefer running `kaist klms auth refresh` yourself before telling the user to do it manually, as long as saved auth config exists and a saved Easy Login username is available.

During `auth refresh`:

- keep the refresh process running instead of abandoning it after the first output
- poll output continuously while the refresh is waiting
- as soon as an Easy Login approval number appears, immediately tell the user the number and ask them to approve it in the KAIST auth app
- do not wait for the command to finish before relaying the number
- after the user approves and refresh succeeds, continue the original KLMS task automatically

Only hand the auth step back to the user when:

- refresh fails or times out
- the flow requires manual browser interaction
- no saved Easy Login username exists
- the user explicitly wants to run the auth command themselves

## Common workflows

### Check what's due and new

```bash
kaist --agent klms today           # urgency-focused: near-term assignments, recent notices, materials
kaist --agent klms inbox           # chronological feed across all resource types
kaist --agent klms inbox --since 2026-03-15T00:00:00
```

For faster results on notice/file-heavy accounts, warm the cache first:
Use `kaist klms sync run` before repeated `today`/`inbox` checks when notice or file fetching is slow.

```bash
kaist --agent klms sync run        # refresh cached notice and file data
kaist --agent klms sync status     # check cache freshness
```

### Browse courses

Use `courses show` for course-level drill-down.

```bash
kaist --agent klms courses list
kaist --agent klms courses list --course "CS101"    # filter by code or title
kaist --agent klms courses list --include-past      # include previous terms
kaist --agent klms courses show COURSE_ID
```

### Check assignments

Use `assignments show` for assignment detail after listing.

```bash
kaist --agent klms assignments list
kaist --agent klms assignments list --course-id ID
kaist --agent klms assignments list --course "ML" --since 2026-03-01
kaist --agent klms assignments list --include-past
kaist --agent klms assignments show ASSIGNMENT_ID
```

### Read notices

Use `notices show` for full notice detail and attachments.

```bash
kaist --agent klms notices list
kaist --agent klms notices list --course "CS101" --since 2026-03-10
kaist --agent klms notices show NOTICE_ID
kaist --agent klms notices show NOTICE_ID --include-html   # include parsed body
```

### Download files and attachments

Use `files get` for file metadata only before downloading.

```bash
kaist --agent klms files list
kaist --agent klms files list --course "CS101"
kaist --agent klms files get FILE_ID           # metadata only, no download
kaist --agent klms files download FILE_ID      # download one file
kaist --agent klms files download FILE_ID --dest ~/Downloads/cs101
kaist --agent klms files pull                  # bulk mirror all downloadable files
kaist --agent klms files pull --course "CS101"
kaist --agent klms files pull --course "CS101" --dest ~/Documents/course-materials/CS101

# Notice attachments
kaist --agent klms notices attachments pull
kaist --agent klms notices attachments pull --course "CS101" --since 2026-03-01
kaist --agent klms notices attachments pull --course "CS101" --dest ~/Documents/course-materials/CS101-notices
```

By default, downloads land under `~/.kaist-cli/files/klms/`. Use `--dest PATH` to write directly into a chosen directory. `--dest` and `--subdir` are mutually exclusive. Single-course pulls write directly into the chosen target; multi-course or unscoped pulls still create per-course subdirectories. Use `--if-exists skip` (default) or `--if-exists overwrite` to control re-download behavior.

### Watch lecture videos

Use `videos show` to resolve the viewer and direct stream URLs.

```bash
kaist --agent klms videos list
kaist --agent klms videos list --course "CS101" --recent
kaist --agent klms videos show VIDEO_ID
```

### Maintenance

```bash
kaist --agent version              # installed version
kaist update --check               # check for updates
kaist update                       # self-update
kaist --agent agent status         # inspect Codex/Claude/Gemini skill installs
kaist agent install codex          # install into $CODEX_HOME/skills
kaist agent install claude         # install into ~/.claude/skills
kaist agent install gemini         # install into ~/.gemini/skills
kaist agent install custom --path ~/agent-skills
kaist --agent klms sync reset      # clear cache without touching auth
```

## Filtering cheat sheet

Use `--since` to avoid reprocessing stale assignment, notice, inbox, or attachment results.

| Flag | Available on | Purpose |
|---|---|---|
| `--course QUERY` | assignments, notices, files, videos | Fuzzy filter by course code or title |
| `--course-id ID` | assignments, notices, files, videos | Exact course scope |
| `--since ISO` | inbox, assignments, notices, notice attachments | Only items after this timestamp |
| `--include-past` | courses, assignments | Include previous terms |
| `--limit N` | most list commands | Cap result count |
| `--dest PATH` | files download, files pull, notice attachments pull | Write directly to a chosen directory instead of the managed files root |

## Error handling and recovery

### Exit codes

Common exit-code buckets:

| Code | Category | Retryable | Typical recovery |
|---|---|---|---|
| 10 | Auth missing/expired | Yes | `kaist klms auth refresh` or `kaist klms auth login` |
| 20 | Network timeout/unavailable | Yes | Check connectivity and retry |
| 30 | API shape drift / parse drift | Yes | Retry, or run `kaist klms dev probe --live` / `kaist klms dev discover` |
| 40 | Config or input invalid | No | Fix args or re-run `kaist klms auth login --base-url https://klms.kaist.ac.kr` |
| 50 | Internal / unsupported / generic not found | Usually no | Inspect the error message |
| 60 | Self-update failed | No | `kaist update --check` |
| 130 | Interrupted | Yes | Retry command |

Some commands raise more specific command-level exit codes for resource misses; for example, certain `show` commands return `44` with `error.code = "NOT_FOUND"`.

### Error codes in JSON envelopes

The `error.code` field is the machine-readable classification. Common values include `AUTH_EXPIRED`, `AUTH_MISSING`, `AUTH_FAILED`, `AUTH_TIMEOUT`, `AUTH_FLOW_UNSUPPORTED`, `BROWSER_INSTALL_FAILED`, `NETWORK_TIMEOUT`, `NETWORK_UNAVAILABLE`, `CONFIG_INVALID`, `API_SHAPE_CHANGED`, `PARSE_DRIFT`, `NOT_FOUND`, `UPDATE_FAILED`, and `INTERNAL`.

Check `error.retryable` to decide whether to retry automatically. The `error.hint` field contains a suggested recovery command when available.

### Recovery patterns

- **`AUTH_MISSING` or `AUTH_EXPIRED`** — run `kaist klms auth refresh`. If that fails, fall back to `kaist klms auth login`.
- **Degraded `today`/`inbox` results** — run `kaist klms sync run` to warm the cache, then retry.
- **Stale notice/file data** — `sync run` fetches fresh data from KLMS notice boards and file surfaces.

## Resource payload fields

Key fields returned in `data` for each resource type:

- **Courses**: `id`, `title`, `course_code`, `course_code_base`, `term_label`, `professors`, `url`
- **Assignments**: `id`, `title`, `course_id`, `course_title`, `course_code`, `due_iso`, `attachments`, `detail_available`
- **Notices**: `board_id`, `id`, `title`, `posted_iso`, `author`, `attachments`, `detail_available`
- **Files**: `id`, `title`, `kind`, `downloadable`, `download_url`, `course_id`, `course_title`, `course_code`
- **Videos**: `id`, `title`, `viewer_url`, `stream_url`, `course_id`, `course_title`, `course_code`
- **Inbox items**: `kind`, `id`, `title`, `course_title`, `time_iso`, plus kind-specific fields

## State and storage

All CLI state lives under `~/.kaist-cli/`:
- Auth artifacts: `~/.kaist-cli/private/klms/` (profile, storage state, config)
- Downloaded files: `~/.kaist-cli/files/klms/`
- Cache: `~/.kaist-cli/private/klms/cache.json`
