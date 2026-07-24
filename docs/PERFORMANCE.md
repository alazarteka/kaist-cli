# CLI Performance Notes

Investigation notes for where `kaist` spends wall time, why, and what we optimized or deferred.

## Command surface and cost profile

| Interface | Typical cost drivers | Notes |
|-----------|----------------------|-------|
| `version`, `update --check`, `agent status` | Process/import only | Intentionally lean; KLMS modules stay unloaded |
| `klms auth status` / `doctor` / `sync status\|reset` | Local disk | No Playwright when `--verify` is omitted |
| `klms today` / `inbox` / `week` | Chromium launch + dashboard auth check + provider fan-out | Warm-path product target: under 5s |
| `klms sync run` | Same browser bootstrap + forced notice/file refresh | Prewarm for later interactive commands |
| `klms assignments\|notices\|files\|videos\|courses list` | Browser bootstrap + multi-course HTTP/HTML | List commands previously re-fetched the dashboard after auth |
| `klms * show` / `download` / `pull` | Browser + detail/download I/O | Dominated by network and file size |
| `klms auth login\|refresh` | Headed/headless Chromium + SSO | Human/OTP latency, not CLI CPU |

## Why live KLMS commands feel slow

1. **Per-command Chromium launch**  
   Almost every live KLMS command opens Playwright, loads a saved profile/storage state, and navigates the dashboard to prove the session is still authenticated. That fixed cost often dominates warm-path budgets.

2. **Redundant dashboard work on list commands**  
   Auth already fetched dashboard HTML. List paths that called `run_authenticated` (without state) then rebuilt bootstrap and fetched the dashboard again over HTTP.

3. **HTML fan-out across courses**  
   Notices/files/videos (and assignment HTML fallback) need one or more pages per course/board. Missing or expired cache turns this into O(courses) network work.

4. **Uncached providers**  
   Assignments and videos are not persisted like notices/files, so aggregates always pay a live assignment refresh.

5. **Documented concurrency env was unused**  
   README advertised `KAIST_KLMS_CONCURRENCY`, but worker counts were hard-coded to 4.

## What changed in this pass

- Reuse a thread-local HTTP opener + shared SSL context in `KlmsHttpSession` instead of rebuilding them on every request.
- Honor `KAIST_KLMS_CONCURRENCY` in `fetch_html_batch` and course-batch loops.
- Keep an in-process cache snapshot so parallel providers do not re-read `cache.json` on every lookup; write compact JSON.
- Upgrade shared `run_list_authenticated` to pass auth dashboard state into list commands so bootstrap skips a second dashboard GET.
- Parallelize assignment HTML fallback across courses. HTTP fan-out stays parallel; Playwright browser fallback runs serially on the caller thread when a path fails or returns a login page.

## Intentionally not changed

- `week` still runs assignments first under a dedicated budget, then notices/files in parallel. Parallelizing all three can starve notice/file work inside the week envelope.
- `sync run` keeps notice then file refresh serial. Parallelizing them would share one Playwright context across threads when either provider falls back to browser pages.
- Standalone `notices list` / `files list` still require hard-fresh cache. Soft-stale reuse (`cache_is_fresh_enough`, ~1 hour) stays limited to dashboard `prefer_cache` paths so explicit list commands do not quietly return expired data.

## Still open (higher leverage, larger change)

1. **Cookie/HTTP-only warm path** that skips Chromium when a recent verification + stored cookies still reach the dashboard.
2. **Assignment (and optionally video) caching** with short TTLs for aggregate warm paths.
3. **Lazy `build_container()`** so offline commands like `sync status` do not import the full KLMS provider graph.
4. **Courses list** still re-navigates the dashboard inside AJAX/HTML helpers even when auth already has that HTML.
5. Optional faster HTML parser (`lxml`) if dependency cost is acceptable.

## Operational knobs

```bash
KAIST_KLMS_CONCURRENCY=8          # HTTP fan-out width (default 4, capped at 32)
KAIST_KLMS_BROWSER_CHANNEL=...    # Prefer a system browser channel
KAIST_KLMS_BROWSER_EXECUTABLE=... # Force a browser executable
```

Prewarm cache before interactive use:

```bash
kaist klms sync run
kaist klms today
```
