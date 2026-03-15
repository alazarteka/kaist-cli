# KAIST CLI Product Spec

## Purpose

Build a KLMS CLI that is:
- fast for daily student use
- deterministic for agent use
- explicit about degraded capability and freshness
- local-first instead of website-shaped

## Current KLMS Information Architecture

Task-first:
- `kaist klms today`
- `kaist klms inbox`
- `kaist klms sync`

Resource-first:
- `kaist klms courses list|show`
- `kaist klms assignments list|show`
- `kaist klms notices list|show`
- `kaist klms files list|get|download|pull`
- `kaist klms videos list|show`

Support:
- `kaist klms auth login|install-browser|status|refresh|doctor`
- `kaist klms dev plan|probe|discover`

## Product Principles

1. API-first where KLMS exposes a usable interface.
2. HTML is an acceptable contract when the site is server-rendered.
3. Auth and degradation must be visible in output.
4. Warm-path speed matters for daily use.
5. Local cache and local files are first-class product assets.

## Output Contract

All JSON commands use a versioned envelope:

```json
{
  "schema": "kaist.klms.today.v1",
  "ok": true,
  "generated_at": "2026-03-16T00:00:00Z",
  "meta": {
    "command": "klms today",
    "source": "mixed",
    "capability": "degraded",
    "cursor": null,
    "next_cursor": null
  },
  "data": {}
}
```

Errors use:

```json
{
  "ok": false,
  "error": {
    "code": "AUTH_MISSING",
    "message": "KLMS config not found.",
    "retryable": false,
    "hint": "Run `kaist klms auth login --base-url https://klms.kaist.ac.kr` first."
  }
}
```

## Capability Model

Provider/source reporting should stay explicit:
- `moodle_ajax`
- `html`
- `browser`
- `cache`
- `mixed`

Capability levels:
- `full`
- `partial`
- `degraded`

`today` and `inbox` additionally report provider freshness:
- `freshness_mode`
- `cache_hit`
- `stale`
- `fetched_at`
- `expires_at`
- `refresh_attempted`
- warning codes such as `STALE_CACHE` and `LIVE_REFRESH_TIMEOUT`

## Auth Model

KLMS auth is persisted under `~/.kaist-cli/private/klms/` using:
- a Playwright profile
- a storage-state export
- cached browser runtime install

`auth status` and `auth doctor` must remain cheap and explicit.

## Provider Strategy

Preferred order:
1. Moodle-standard/API paths when actually usable on this KLMS instance
2. Known KLMS AJAX methods
3. HTML parsing
4. Browser navigation only for auth/bootstrap or last resort

## Performance Targets

Warm-path goals:
- `today`: under 5 seconds
- `inbox`: under 5 seconds
- resource list/show commands: low single-digit seconds where feasible

Cold-path behavior:
- bounded live refresh
- cache-first fallback for slow HTML-backed providers
- partial results with explicit warnings instead of hanging

## Current Gaps

Remaining major product work:
- broader `courseboard` surface beyond notices
- richer video actions such as open/download
- optional local mirroring flows beyond `files pull`

## Non-Goals

- backward compatibility with the removed legacy KLMS command grammar
- pretending all KLMS surfaces have a clean hidden API
- mutating workflows without an explicit product need
