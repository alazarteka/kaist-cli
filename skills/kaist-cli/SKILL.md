---
name: kaist-cli
description: Use the installed `kaist` CLI as the primary interface for KLMS. Prefer it over direct scraping when you need authenticated course data, structured JSON, downloads, sync, or self-update.
---

# KAIST CLI

Use the installed `kaist` command as the primary automation surface for KLMS.

## Agent mode

- Prefer `kaist --agent ...` for automation.
- `--agent` is a global flag. Put it before the command:
  - `kaist --agent klms today`
  - `kaist --agent klms assignments show 1210516`
  - `kaist --agent version`
- `--agent` returns a strict envelope:
  - `ok`: boolean
  - `schema`: stable schema name such as `kaist.klms.today.v1`
  - `meta`: includes `source`, `capability`, and command metadata
  - `data`: command payload
- Treat `meta.capability` as the trust/degradation signal:
  - `full`: provider-backed result with strong coverage
  - `partial`: valid result with fallback/provider limits
  - `degraded`: returned with warnings, stale cache, or partial refresh failure

## Setup and auth

- First-time browser prerequisites:
  - `kaist klms auth install-browser`
- First-time login:
  - `kaist klms auth login --base-url https://klms.kaist.ac.kr`
  - This is the interactive browser flow and is not suitable for headless or non-interactive shells.
- Non-interactive Easy Login:
  - `kaist klms auth login --base-url https://klms.kaist.ac.kr --username KAIST_ID`
  - This uses KAIST SSO Easy Login and prints the approval number for the KAIST auth app.
- Renewal:
  - `kaist klms auth refresh`
  - If a username was saved earlier, refresh reuses it automatically.
- Diagnostics:
  - `kaist klms auth status`
  - `kaist klms auth doctor`

## Core commands

- Detail/drill-down commands:
  - `courses show`
  - `assignments show`
  - `notices show`
  - `videos show`
  - `files get`
- Daily summaries:
  - `kaist klms today`
  - `kaist klms inbox`
- Sync and cache:
  - `kaist klms sync run`
  - `kaist klms sync status`
  - `kaist klms sync reset`
  - Use `sync run` before `today`/`inbox` when you want a warm cache for slower notice/file providers.
- Drill-down surfaces:
  - `kaist klms courses list`
  - `kaist klms courses show ID`
  - `kaist klms assignments list`
  - `kaist klms assignments show ID`
  - `kaist klms notices list`
  - `kaist klms notices show ID`
  - `kaist klms files list`
  - `kaist klms files get ID_OR_URL`
  - `kaist klms files download ID_OR_URL`
  - `kaist klms files pull`
  - `kaist klms videos list`
  - `kaist klms videos show ID_OR_URL`
- Maintenance:
  - `kaist --agent version`
  - `kaist update --check`
  - `kaist update`

## Useful filters

- Many list commands support `--course-id ID` for exact course scoping.
- Human-friendly course filtering uses `--course QUERY` where available.
- Use `--since` to avoid processing stale time-bearing resources.
- Time-bearing resources support `--since ISO` where available:
  - `assignments list --since ...`
  - `notices list --since ...`
- Course/assignment widening:
  - `--include-past` includes past-term data instead of the default current-term view.

## Resource payload expectations

- Courses:
  - key fields: `id`, `title`, `course_code`, `course_code_base`, `term_label`, `professors`, `url`
- Assignments:
  - key fields: `id`, `title`, `course_id`, `course_title`, `course_code`, `due_iso`, `attachments`, `detail_available`
- Notices:
  - key fields: `board_id`, `id`, `title`, `posted_iso`, `author`, `attachments`, `detail_available`
- Files:
  - key fields: `id`, `title`, `kind`, `downloadable`, `download_url`, `course_id`, `course_title`, `course_code`
- Videos:
  - key fields: `id`, `title`, `viewer_url`, `stream_url`, `course_id`, `course_title`, `course_code`

## Error handling

- A successful envelope with `ok=true` should still be treated cautiously when `meta.capability == "degraded"`.
- Invalid or missing resources should return non-zero structured errors, not placeholder data.
- If a command fails:
  - inspect stderr in human mode
  - inspect the JSON error envelope in `--agent` mode
- Common auth recovery:
  - `AUTH_MISSING` or `AUTH_EXPIRED` -> run `kaist klms auth refresh`
- Common discovery fallback:
  - if notices/files are degraded, try `kaist klms sync run` and retry

## Operational notes

- KLMS state is stored under `~/.kaist-cli/`.
- The CLI caches notices/files for faster warm-path reads.
- Installed release bundles also include Claude metadata under `skills/kaist-cli/.claude-plugin/`.
- Use the CLI’s own commands before falling back to browser automation or direct scraping.
- `kaist --help` and `kaist --agent version` should report the bundled skill path when installed from a release bundle.
