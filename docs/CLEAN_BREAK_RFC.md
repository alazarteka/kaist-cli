# Clean Break RFC

Date: 2026-03-15
Status: Historical / Completed
Branch: `codex/klms-v2`

## Purpose

This branch began as a clean-break rewrite of the KLMS CLI.

The current codebase remains valuable as:
- reverse-engineering notes
- known-working parsing logic
- auth and endpoint research
- fixture coverage

It is now the architectural foundation for the shipped KLMS CLI.

## Product Goals

The new CLI should be:
- fast for daily student use
- deterministic for agents
- explicit about degraded capability
- local-first instead of website-shaped

## Interface Direction

Task-first commands:
- `kaist klms auth login|status|refresh|doctor`
- `kaist klms today`
- `kaist klms inbox`
- `kaist klms sync run|status|reset`

Resource commands:
- `kaist klms courses list|show`
- `kaist klms assignments list|show`
- `kaist klms notices list|show`
- `kaist klms files list|get`

Engineering commands:
- `kaist klms dev plan|probe`

## Architecture Rules

The v2 tree should follow these boundaries:

```text
v2/
  parser + main
  envelope + error handling
  klms/
    contracts
    auth
    providers
    services
    storage
```

Rules:
- services own product logic
- providers own upstream integration details
- auth is a subsystem, not an incidental helper
- browser use is a fallback, not the normal read path
- the old `src/kaist_cli/klms.py` monolith has been removed

## Provider Order

Preferred data sources:
1. Moodle-standard mobile/webservice endpoints, if available
2. KLMS AJAX endpoints already known to work
3. HTML parsing
4. Browser navigation as last resort

## Outcome

Implemented in the migrated KLMS CLI:
- task-first and resource-first command surface
- durable auth with browser-profile persistence
- provider-backed courses, assignments, notices, files, and videos
- `today`, `inbox`, and `sync`
- live endpoint probing/discovery
- cache-aware provider freshness reporting

## APK Findings

The KLMS Android app appears to be a native Kotlin WebView shell, not the official Moodle app.

Confirmed app clues:
- package `com.kaist.lms`
- classes such as `KaistWebViewClient`, `KaistJsInterface`, `WebViewManager`
- KLMS-specific URLs:
  - `https://klms.kaist.ac.kr`
  - `https://klms.kaist.ac.kr/local/applogin/result_login_json.php`
  - `https://klms.kaist.ac.kr/login/ssologin.php`

Implication:
- the app is useful for auth-flow reconnaissance
- it does not justify building the CLI around a mobile-app clone
