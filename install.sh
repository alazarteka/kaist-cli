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

linux_libc_kind() {
  local output=""
  if command -v getconf >/dev/null 2>&1; then
    output="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    if [[ -n "$output" ]]; then
      printf 'glibc'
      return
    fi
  fi

  if command -v ldd >/dev/null 2>&1; then
    output="$(ldd --version 2>&1 || true)"
    if printf '%s' "$output" | grep -Eiq 'musl'; then
      printf 'musl'
      return
    fi
    if printf '%s' "$output" | grep -Eiq 'glibc|gnu libc'; then
      printf 'glibc'
      return
    fi
  fi

  printf 'unknown'
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
          die "This installer currently supports macOS arm64/x86_64 standalone bundles."
          ;;
      esac
      ;;
    Linux)
      case "$arch_name" in
        x86_64|amd64)
          case "$(linux_libc_kind)" in
            glibc)
              printf 'linux-x86_64-gnu'
              ;;
            musl)
              die "Published Linux standalone bundles support only x86_64 glibc hosts (Ubuntu/Debian-class). musl/Alpine is not supported."
              ;;
            *)
              die "Could not detect a supported Linux libc. Published Linux standalone bundles support only x86_64 glibc hosts (Ubuntu/Debian-class)."
              ;;
          esac
          ;;
        *)
          die "Published standalone bundles currently support macOS arm64/x86_64 and Linux x86_64 glibc."
          ;;
      esac
      ;;
    *)
      die "This installer currently supports macOS arm64/x86_64 and Linux x86_64 glibc standalone bundles."
      ;;
  esac
}

json_field() {
  local path="$1"
  local key="$2"
  tr -d '\n' <"$path" | sed -n "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p"
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
  local marketplace_root plugin_root skill_target skill_link plugin_json marketplace_json source_plugin_json source_marketplace_json
  marketplace_root="$install_root/mkt"
  plugin_root="$marketplace_root/plugins/kaist-cli"
  skill_target="$install_root/current/skills/kaist-cli"
  skill_link="$plugin_root/skills/kaist-cli"
  plugin_json="$plugin_root/.claude-plugin/plugin.json"
  marketplace_json="$marketplace_root/.claude-plugin/marketplace.json"
  source_plugin_json="$skill_target/.claude-plugin/plugin.json"
  source_marketplace_json="$skill_target/.claude-plugin/marketplace.json"

  mkdir -p "$plugin_root/.claude-plugin" "$(dirname "$skill_link")" "$(dirname "$marketplace_json")"
  rm -rf "$skill_link"
  ln -sfn "$skill_target" "$skill_link"

  if [[ -f "$source_plugin_json" ]]; then
    cp "$source_plugin_json" "$plugin_json"
  else
    cat >"$plugin_json" <<JSON
{
  "name": "kaist-cli",
  "description": "Operate KLMS through the installed kaist CLI.",
  "author": {
    "name": "kaist-cli"
  }
}
JSON
  fi

  if [[ -f "$source_marketplace_json" ]]; then
    cp "$source_marketplace_json" "$marketplace_json"
    sed -i.bak "s/__KAIST_VERSION__/${version}/g" "$marketplace_json"
    sed -i.bak 's#"source": "."#"source": "./plugins/kaist-cli"#' "$marketplace_json"
    rm -f "$marketplace_json.bak"
  else
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
  fi
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
log "Verifying ${ARCHIVE_NAME}"
verify_checksum "$archive_path" "$checksums_path"

mkdir -p "$extract_dir"
log "Extracting ${ARCHIVE_NAME}"
tar -xzf "$archive_path" -C "$extract_dir"

[[ -f "$extract_dir/bundle.json" ]] || die "Release archive is missing bundle.json."
binary_relpath="$(json_field "$extract_dir/bundle.json" "binary_relpath")"
skill_relpath="$(json_field "$extract_dir/bundle.json" "skill_relpath")"
[[ -n "$binary_relpath" ]] || die "Release archive bundle.json is missing binary_relpath."
[[ -n "$skill_relpath" ]] || die "Release archive bundle.json is missing skill_relpath."
binary_path="$extract_dir/$binary_relpath"
skill_path="$extract_dir/$skill_relpath"
[[ -x "$binary_path" || -f "$binary_path" ]] || die "Release archive is missing bundled executable at $binary_relpath."
[[ -f "$skill_path/SKILL.md" ]] || die "Release archive is missing bundled skill."
chmod +x "$binary_path"

mkdir -p "$INSTALL_ROOT/versions" "$BIN_DIR"
version_dir="$INSTALL_ROOT/versions/$TAG"
current_link="$INSTALL_ROOT/current"
previous_link="$INSTALL_ROOT/previous"
bin_link="$BIN_DIR/kaist"
install_metadata_path="$INSTALL_ROOT/install.json"

old_current="$(resolve_dir_link "$current_link" || true)"
rm -rf "$version_dir"
log "Installing ${TAG}"
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

ln -sfn "$INSTALL_ROOT/current/$binary_relpath" "$bin_link"
cat >"$install_metadata_path" <<JSON
{
  "launcher_path": "$bin_link"
}
JSON
sync_claude_marketplace "$INSTALL_ROOT" "${TAG#v}" || warn "Could not sync Claude plugin marketplace metadata."

skill_path="$INSTALL_ROOT/current/$skill_relpath"
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
