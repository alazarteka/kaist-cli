# CLI Migration Guide (Command IA Refactor)

This release switches KLMS command paths to grouped resource commands.

## Renamed Commands

- `kaist klms courses` -> `kaist klms list courses`
- `kaist klms assignments` -> `kaist klms list assignments`
- `kaist klms notices` -> `kaist klms list notices`
- `kaist klms files` -> `kaist klms list files`
- `kaist klms download <url>` -> `kaist klms get file <url>`
- `kaist klms sync` -> `kaist klms sync run`

## New Sync Subcommands

- `kaist klms sync status`: inspect local snapshot metadata
- `kaist klms sync reset`: remove local snapshot file

## JSON Contract Compatibility

- Existing JSON envelope fields remain unchanged (`schema`, `ok`, `generated_at`, `meta`, `data`/`error`).
- Existing KLMS resource schema names remain stable for migrated commands:
  - courses: `kaist.klms.courses.v1`
  - assignments: `kaist.klms.assignments.v1`
  - notices: `kaist.klms.notices.v1`
  - files: `kaist.klms.files.v1`
  - download/get file: `kaist.klms.download.v1`
  - sync run: `kaist.klms.sync.v1`

## New System Scaffold

A `portal` adapter scaffold is now available for future KAIST school portal expansion:

- `kaist portal status`
- `kaist portal auth status`
- `kaist portal auth login`

These commands are currently placeholders and report `implemented: false`.
