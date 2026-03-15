#!/usr/bin/env bash
set -euo pipefail

uv run kaist --agent klms auth status >/dev/null
uv run kaist --agent klms courses list >/dev/null
uv run kaist --agent klms assignments list --limit 20 >/dev/null
uv run kaist --agent klms notices list --max-pages 1 --limit 20 >/dev/null
uv run kaist --agent klms files list --limit 20 >/dev/null
uv run kaist --agent klms videos list --limit 10 >/dev/null
uv run kaist --agent klms today --limit 5 >/dev/null
uv run kaist --agent klms inbox --limit 30 >/dev/null
uv run kaist --agent klms sync status >/dev/null

echo "KLMS smoke checks passed"
