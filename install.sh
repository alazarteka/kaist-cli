#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '%s\n' "$*" >&2
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

normalize_tag() {
  local raw="$1"
  if [[ "$raw" == v* ]]; then
    printf '%s' "$raw"
  else
    printf 'v%s' "$raw"
  fi
}

resolve_target() {
  if [[ -n "${KAIST_PLATFORM_TARGET:-}" ]]; then
    printf '%s' "$KAIST_PLATFORM_TARGET"
    return
  fi

  local os_name arch_name
  os_name="$(uname -s)"
  arch_name="$(uname -m)"

  case "$os_name" in
    Darwin)
      case "$arch_name" in
        arm64|aarch64)
          printf 'darwin-arm64'
          ;;
        x86_64|amd64)
          printf 'darwin-x86_64'
          ;;
        *)
          die "This installer currently supports macOS arm64/x86_64 and Linux x86_64 musl."
          ;;
      esac
      ;;
    Linux)
      case "$arch_name" in
        x86_64|amd64)
          if [[ -e /lib/ld-musl-x86_64.so.1 || -e /usr/glibc-compat/lib/ld-musl-x86_64.so.1 ]]; then
            printf 'linux-x86_64-musl'
            return
          fi
          if command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; then
            printf 'linux-x86_64-musl'
            return
          fi
          die "Linux installs currently support only x86_64 musl builds. Set KAIST_PLATFORM_TARGET manually if you know the correct release target."
          ;;
        *)
          die "Linux installs currently support only x86_64 musl builds."
          ;;
      esac
      ;;
    *)
      die "This installer currently supports macOS arm64/x86_64 and Linux x86_64 musl."
      ;;
  esac
}

fetch_latest_tag() {
  local repo="$1"
  local api_url="${KAIST_RELEASE_API_URL:-https://api.github.com/repos/${repo}/releases/latest}"
  local payload tag
  payload="$(curl -fsSL "$api_url")" || die "Failed to fetch latest release metadata."
  tag="$(printf '%s' "$payload" | tr -d '\n' | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  [[ -n "$tag" ]] || die "Could not parse tag_name from release metadata."
  printf '%s' "$tag"
}

checksum_value() {
  local path="$1"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
    return
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
    return
  fi
  die "No SHA256 tool found (expected shasum or sha256sum)."
}

verify_checksum() {
  local archive_path="$1"
  local checksums_path="$2"
  local archive_name expected actual
  archive_name="$(basename "$archive_path")"
  expected="$(awk -v name="$archive_name" '$NF == name || $NF == "*"name {print $1}' "$checksums_path" | tail -n 1 | tr '[:upper:]' '[:lower:]')"
  [[ -n "$expected" ]] || die "checksums.txt does not include $archive_name"
  actual="$(checksum_value "$archive_path")"
  [[ "$actual" == "$expected" ]] || die "Checksum mismatch for $archive_name"
}

resolve_dir_link() {
  local path="$1"
  if [[ ! -e "$path" && ! -L "$path" ]]; then
    return 0
  fi
  (
    cd "$path" >/dev/null 2>&1 && pwd -P
  )
}

prune_versions() {
  local install_root="$1"
  local keep_current="$2"
  local keep_previous="$3"
  local dir real
  for dir in "$install_root"/versions/*; do
    [[ -d "$dir" ]] || continue
    real="$(cd "$dir" >/dev/null 2>&1 && pwd -P)"
    if [[ "$real" == "$keep_current" || "$real" == "$keep_previous" ]]; then
      continue
    fi
    rm -rf "$dir" || warn "Could not prune $dir"
  done
}

sync_claude_marketplace() {
  local install_root="$1"
  local version="$2"
  local marketplace_root plugin_root skill_target skill_link plugin_json marketplace_json
  marketplace_root="$install_root/mkt"
  plugin_root="$marketplace_root/plugins/kaist-cli"
  skill_target="$install_root/current/skills/kaist-cli"
  skill_link="$plugin_root/skills/kaist-cli"
  plugin_json="$plugin_root/.claude-plugin/plugin.json"
  marketplace_json="$marketplace_root/.claude-plugin/marketplace.json"

  mkdir -p "$plugin_root/.claude-plugin" "$(dirname "$skill_link")" "$(dirname "$marketplace_json")"
  rm -rf "$skill_link"
  ln -sfn "$skill_target" "$skill_link"

  cat >"$plugin_json" <<JSON
{
  "name": "kaist-cli",
  "description": "Operate KLMS through the installed kaist CLI.",
  "author": {
    "name": "kaist-cli"
  }
}
JSON

  cat >"$marketplace_json" <<JSON
{
  "name": "kaist-cli",
  "owner": {
    "name": "kaist-cli"
  },
  "plugins": [
    {
      "name": "kaist-cli",
      "description": "Operate KLMS through the installed kaist CLI.",
      "version": "${version}",
      "author": {
        "name": "kaist-cli"
      },
      "source": "./plugins/kaist-cli",
      "category": "productivity"
    }
  ]
}
JSON
}

REPO="${KAIST_RELEASE_REPO:-alazarteka/kaist-cli}"
VERSION_REQUEST="${KAIST_VERSION:-latest}"
INSTALL_ROOT="${KAIST_INSTALL_ROOT:-$HOME/.local/share/kaist-cli}"
BIN_DIR="${KAIST_BIN_DIR:-$HOME/.local/bin}"
TARGET="$(resolve_target)"

if [[ "$VERSION_REQUEST" == "latest" ]]; then
  TAG="$(fetch_latest_tag "$REPO")"
else
  TAG="$(normalize_tag "$VERSION_REQUEST")"
fi

DOWNLOAD_BASE="${KAIST_DOWNLOAD_BASE_URL:-https://github.com/${REPO}/releases/download}"
ARCHIVE_NAME="kaist-${TAG}-${TARGET}.tar.gz"
ARCHIVE_URL="${DOWNLOAD_BASE%/}/${TAG}/${ARCHIVE_NAME}"
CHECKSUMS_URL="${DOWNLOAD_BASE%/}/${TAG}/checksums.txt"

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/kaist-install.XXXXXX")"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

archive_path="$tmp_dir/$ARCHIVE_NAME"
checksums_path="$tmp_dir/checksums.txt"
extract_dir="$tmp_dir/extracted"

log "Downloading ${ARCHIVE_NAME}"
curl -fsSL "$ARCHIVE_URL" -o "$archive_path" || die "Failed to download release archive."
curl -fsSL "$CHECKSUMS_URL" -o "$checksums_path" || die "Failed to download checksums.txt."
verify_checksum "$archive_path" "$checksums_path"

mkdir -p "$extract_dir"
tar -xzf "$archive_path" -C "$extract_dir"

[[ -f "$extract_dir/bundle.json" ]] || die "Release archive is missing bundle.json."
[[ -x "$extract_dir/bin/kaist" || -f "$extract_dir/bin/kaist" ]] || die "Release archive is missing bin/kaist."
[[ -f "$extract_dir/skills/kaist-cli/SKILL.md" ]] || die "Release archive is missing bundled skill."
chmod +x "$extract_dir/bin/kaist"

mkdir -p "$INSTALL_ROOT/versions" "$BIN_DIR"
version_dir="$INSTALL_ROOT/versions/$TAG"
current_link="$INSTALL_ROOT/current"
previous_link="$INSTALL_ROOT/previous"
bin_link="$BIN_DIR/kaist"

old_current="$(resolve_dir_link "$current_link" || true)"
rm -rf "$version_dir"
mv "$extract_dir" "$version_dir"

ln -sfn "$version_dir" "$current_link"
if [[ -n "$old_current" && "$old_current" != "$version_dir" ]]; then
  ln -sfn "$old_current" "$previous_link"
else
  rm -f "$previous_link"
fi

keep_current="$(resolve_dir_link "$current_link" || true)"
keep_previous="$(resolve_dir_link "$previous_link" || true)"
prune_versions "$INSTALL_ROOT" "$keep_current" "$keep_previous"

ln -sfn "$INSTALL_ROOT/current/bin/kaist" "$bin_link"
sync_claude_marketplace "$INSTALL_ROOT" "${TAG#v}" || warn "Could not sync Claude plugin marketplace metadata."

skill_path="$INSTALL_ROOT/current/skills/kaist-cli"
printf 'Installed kaist %s to %s\n' "${TAG#v}" "$INSTALL_ROOT/current"
printf 'Binary: %s\n' "$bin_link"
printf 'Bundled skill: %s\n' "$skill_path"
printf 'Agents can install the skill directly from that path.\n'

case ":${PATH:-}:" in
  *":$BIN_DIR:"*)
    ;;
  *)
    warn "$BIN_DIR is not on PATH. Add it before using kaist directly."
    ;;
esac
