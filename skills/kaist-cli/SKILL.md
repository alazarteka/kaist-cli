---
name: kaist-cli
description: Use when an installed `kaist` CLI should be the primary interface for KAIST systems, especially KLMS auth, daily summaries, structured JSON output, downloads, sync, and self-update. Prefer this skill for agent-driven KLMS work instead of direct site scraping when the local `kaist` command is available.
---

# KAIST CLI

Use the installed `kaist` command as the primary automation surface for KLMS work.

## When to use it

- The user wants KLMS data, downloads, or daily summaries through the CLI.
- A coding agent should interact with KLMS in a stable way instead of scraping the website directly.
- The task benefits from deterministic JSON output, local cache/sync, or the built-in update flow.

## Command style

- Prefer `kaist --agent ...` for strict JSON envelopes with stable schema names.
- Use `kaist --format json ...` when JSON is needed without the full agent envelope.
- Use plain human output only when the user is reading the result directly.

## Core workflows

- Auth:
  - `kaist klms auth login --base-url https://klms.kaist.ac.kr`
  - `kaist klms auth status`
  - `kaist klms auth refresh`
  - `kaist klms auth doctor`
- Daily use:
  - `kaist klms today`
  - `kaist klms inbox`
  - `kaist klms sync run`
  - `kaist klms sync status`
- Resources:
  - `kaist klms courses list`
  - `kaist klms assignments list`
  - `kaist klms notices list`
  - `kaist klms files list`
  - `kaist klms files download <id-or-url>`
  - `kaist klms files pull`
  - `kaist klms videos list`
- Maintenance:
  - `kaist version`
  - `kaist update --check`
  - `kaist update`

## Operational notes

- KLMS state is stored under `~/.kaist-cli/`.
- The CLI persists KLMS auth locally and reuses it across commands.
- Use the CLI’s own commands before falling back to direct browser automation or ad hoc HTTP probing.

## Discovery

- `kaist --help` should print the bundled skill location when available.
- `kaist version --agent` should report the install root, bundled skill path, and whether self-update is supported.
