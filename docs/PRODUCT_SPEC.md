# KAIST CLI Product Spec (KLMS)

## 1. Purpose
Build a terminal interface for KLMS that is:
- reliable for daily human use
- deterministic for AI agent automation
- resilient to KLMS frontend changes
- fast enough for repeated sync/inbox workflows

This spec assumes read-only access to KLMS.

## 2. User Personas

### 2.1 Human student user
Needs:
- low-friction daily checks (deadlines, notices, new files)
- clear errors and recovery steps
- fast commands with readable output

### 2.2 Agent / automation runner
Needs:
- stable, versioned JSON contracts
- strict stdout/stderr behavior
- reliable exit codes and retry semantics
- pagination/cursor support

## 3. Product Principles
1. API-first, browser-second
2. Stable contracts before new features
3. Explicit auth/session health
4. Human and agent UX both first-class
5. Fallback behavior must be visible in output

## 4. Command Information Architecture (Target)

Top-level stable groups:
- `kaist klms auth`
- `kaist klms list`
- `kaist klms get`
- `kaist klms sync`
- `kaist klms inbox`
- `kaist klms config`

Experimental group:
- `kaist klms dev`

### 4.1 Auth
- `auth login`
- `auth status`
- `auth refresh`
- `auth doctor`

### 4.2 List
- `list courses`
- `list assignments`
- `list notices`
- `list files`

Shared list options:
- `--course-id`
- `--notice-board-id`
- `--since`
- `--until`
- `--limit`
- `--cursor`
- `--sort`
- `--fields`

### 4.3 Get
- `get course <id>`
- `get assignment <id>`
- `get notice <id>`
- `get file <id-or-url>`

### 4.4 Sync
- `sync run`
- `sync status`
- `sync reset`

### 4.5 Inbox
- `inbox` (priority-sorted blended feed)

## 5. Output Contract

## 5.1 Global output modes
- `--format auto|json|table|text`
- `--agent` implies machine-safe defaults:
  - `--format json`
  - no progress noise on stdout
  - deterministic field ordering

## 5.2 Envelope (all JSON commands)
```json
{
  "schema": "kaist.klms.<resource>.v1",
  "ok": true,
  "generated_at": "2026-02-27T07:10:02Z",
  "meta": {
    "source": "api|html|mixed",
    "cursor": null,
    "next_cursor": null
  },
  "data": {}
}
```

## 5.3 Error envelope
```json
{
  "ok": false,
  "error": {
    "code": "AUTH_EXPIRED",
    "message": "Session is no longer valid.",
    "retryable": true,
    "hint": "kaist klms auth login"
  }
}
```

## 5.4 Output channel rules
- stdout: data only
- stderr: diagnostics/progress/hints
- no mixed human noise in JSON mode

## 6. Error Code Registry (v1)
- `AUTH_MISSING`
- `AUTH_EXPIRED`
- `AUTH_INVALID_ARTIFACT`
- `NETWORK_TIMEOUT`
- `NETWORK_UNAVAILABLE`
- `PARSE_DRIFT`
- `API_SHAPE_CHANGED`
- `CONFIG_INVALID`
- `NOT_FOUND`
- `INTERNAL`

Exit code mapping:
- `0` success
- `10` auth errors
- `20` network errors
- `30` parse/api-shape errors
- `40` config errors
- `50` unknown internal errors

## 7. Data Model Contracts (v1)

Entity schemas should be explicit (TypedDict or dataclass + serializer):
- `Course`
- `Assignment`
- `Notice`
- `Material`
- `InboxItem`

Required common fields:
- `id` (string where available)
- `title`
- `url`
- `source` (`api|html|fallback`)
- `confidence` (`0.0..1.0`)
- `fetched_at`

## 8. Auth and Session UX

### 8.1 Status semantics
`auth status` should report:
- active mode
- validated boolean
- final URL
- cookie expiry windows
- recommended next action

### 8.2 Refresh semantics
`auth refresh` should:
- launch login flow if expired
- regenerate storage state
- verify dashboard access
- return structured success/failure

### 8.3 Doctor checks
`auth doctor` should run:
- artifact presence/permissions
- online validation probe
- cookie stats sanity
- writeability checks under CLI home

## 9. Architecture Plan

Target module boundaries:
- `auth.py`
- `runtime.py`
- `state_store.py`
- `models.py`
- `services/` (courses, assignments, notices, files, inbox, sync)
- `discovery.py`
- `dev_commands.py`

Rules:
- browser only for login/bootstrap/discovery/fallback
- API client first for data fetch paths
- parser fallback must emit `source=fallback`

## 10. Performance Targets

SLO targets on warm session:
- `list courses`: p95 < 2.0s
- `inbox`: p95 < 4.0s
- `sync run --dry-run`: p95 < 8.0s

Operational controls:
- bounded concurrency
- per-command timeout budget
- retries with jitter for transient network errors

## 11. Testing and Reliability

Required test layers:
1. contract tests for JSON envelopes and error shapes
2. fixture parser tests from captured KLMS HTML
3. service tests with mocked API payloads
4. smoke workflow tests (`auth status`, `list`, `sync`) behind opt-in env flag

Drift handling:
- when parse/API shape mismatch occurs, write redacted debug artifact
- return `PARSE_DRIFT` or `API_SHAPE_CHANGED` with hint

## 12. Security and Safety
- keep read-only default behavior
- strict file permissions for session and state files
- never log sensitive cookie payloads
- avoid aggressive request rates

## 13. Execution Roadmap

### Phase A: Contracts and UX floor (1 week)
- implement JSON envelope + error registry + exit codes
- add `--agent` mode
- enforce stdout/stderr contract

### Phase B: Auth lifecycle (1 week)
- implement `auth refresh`
- implement `auth doctor`
- add proactive expiry warnings

### Phase C: API-first migration (2-3 weeks)
- migrate notices to API-first
- migrate assignments to API-first
- keep HTML fallback with explicit source markers

### Phase D: Daily usability (1 week)
- implement `inbox`
- standardize list filtering options
- add shell completion and command examples

### Phase E: Hardening (ongoing)
- parser fixture corpus expansion
- latency tracking and regression checks
- automated smoke checks

## 14. Definition of Done (v1)
The CLI is considered v1-ready when:
1. all stable commands emit versioned envelopes in JSON mode
2. error codes and exit codes are fully implemented and documented
3. auth refresh/doctor flows are working
4. at least notices + assignments use API-first with reliable fallback
5. inbox command is available and useful for daily workflow
6. contract tests and smoke suite are green
