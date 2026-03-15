# KLMS Migration Status

The KLMS migration is complete.

## What Changed

The shipped `kaist klms` surface now uses the clean-break implementation directly.

Current public grammar:
- `kaist klms auth ...`
- `kaist klms today`
- `kaist klms inbox`
- `kaist klms sync ...`
- `kaist klms courses ...`
- `kaist klms assignments ...`
- `kaist klms notices ...`
- `kaist klms files ...`
- `kaist klms videos ...`
- `kaist klms dev ...`

## Removed Surface

The old KLMS grammar is intentionally gone:
- `kaist klms config ...`
- `kaist klms list ...`
- `kaist klms get ...`
- legacy debug commands such as `fetch-html`, `courses-api`, `course-info`, and `discover-api`

There is no backward-compatibility shim.

## JSON Schemas

Stable schemas now match the new resource/task surface, for example:
- `kaist.klms.auth.status.v1`
- `kaist.klms.today.v1`
- `kaist.klms.inbox.v1`
- `kaist.klms.courses.list.v1`
- `kaist.klms.assignments.show.v1`
- `kaist.klms.files.pull.v1`
- `kaist.klms.videos.show.v1`

## Codebase Result

Removed:
- the legacy `src/kaist_cli/klms.py` monolith
- legacy wrapper services/parsers under `src/kaist_cli/systems/klms/`

Retained:
- the top-level `kaist` runtime and non-KLMS systems
- the clean-break KLMS implementation under `src/kaist_cli/v2/`
