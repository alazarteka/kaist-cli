#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  build_release_bundle.sh --binary <path> --version <vX.Y.Z|X.Y.Z> --target <target> --out-dir <path> [--repo <owner/repo>]

`--binary` may point to either a standalone executable file or a PyInstaller onedir
directory. When a directory is provided, the bundle manifest points to the real
executable inside that runtime tree.
EOF
}

binary=""
version=""
target=""
out_dir=""
repo="alazarteka/kaist-cli"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --binary)
      binary="${2:-}"
      shift 2
      ;;
    --version)
      version="${2:-}"
      shift 2
      ;;
    --target)
      target="${2:-}"
      shift 2
      ;;
    --out-dir)
      out_dir="${2:-}"
      shift 2
      ;;
    --repo)
      repo="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$binary" || -z "$version" || -z "$target" || -z "$out_dir" ]]; then
  usage >&2
  exit 2
fi

if [[ ! -e "$binary" ]]; then
  echo "error: binary path not found: $binary" >&2
  exit 1
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
skill_dir="$repo_root/skills/kaist-cli"
if [[ ! -f "$skill_dir/SKILL.md" ]]; then
  echo "error: bundled skill missing at $skill_dir" >&2
  exit 1
fi

version_no_v="${version#v}"
tag="v${version_no_v}"
archive_name="kaist-${tag}-${target}.tar.gz"

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/kaist-bundle.XXXXXX")"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

stage_dir="$tmp_dir/stage"
mkdir -p "$stage_dir/bin" "$stage_dir/skills"
binary_relpath=""
if [[ -d "$binary" ]]; then
  mkdir -p "$stage_dir/bin/kaist"
  cp -R "$binary"/. "$stage_dir/bin/kaist/"
  binary_relpath="bin/kaist/kaist"
else
  cp "$binary" "$stage_dir/bin/kaist"
  binary_relpath="bin/kaist"
fi
if [[ ! -e "$stage_dir/$binary_relpath" ]]; then
  echo "error: bundled executable missing at $binary_relpath" >&2
  exit 1
fi
chmod +x "$stage_dir/$binary_relpath"
cp -R "$skill_dir" "$stage_dir/skills/"
if [[ -f "$stage_dir/skills/kaist-cli/.claude-plugin/marketplace.json" ]]; then
  sed -i.bak "s/__KAIST_VERSION__/${version_no_v}/g" "$stage_dir/skills/kaist-cli/.claude-plugin/marketplace.json"
  rm -f "$stage_dir/skills/kaist-cli/.claude-plugin/marketplace.json.bak"
fi

cat >"$stage_dir/bundle.json" <<EOF
{
  "version": "${version_no_v}",
  "repo": "${repo}",
  "target": "${target}",
  "binary_relpath": "${binary_relpath}",
  "skill_relpath": "skills/kaist-cli"
}
EOF

mkdir -p "$out_dir"
tar -C "$stage_dir" -czf "$out_dir/$archive_name" bundle.json bin skills
printf '%s\n' "$out_dir/$archive_name"
