#!/usr/bin/env bash
set -euo pipefail

uv run kaist --agent klms auth status --no-validate >/dev/null
uv run kaist --agent klms list courses --no-enrich >/dev/null
uv run kaist --agent klms list assignments --limit 20 >/dev/null
uv run kaist --agent klms list notices --max-pages 1 --limit 20 >/dev/null
uv run kaist --agent klms list files --limit 20 >/dev/null
uv run kaist --agent klms inbox --limit 30 >/dev/null
uv run kaist --agent klms sync run --dry-run >/dev/null

echo "KLMS smoke checks passed"
