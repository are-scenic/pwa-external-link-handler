#!/usr/bin/env bash
# PWA External Link Handler - Linux/macOS native-host installer.
#
# Installs the Python native-messaging host and writes a per-browser
# manifest into every supported Chromium-family browser's
# NativeMessagingHosts directory. Idempotent: re-running overwrites
# existing files. User-scope only - never requires root/sudo.
#
# Usage:
#   ./install.sh                 Install for the current user.
#   ./install.sh --uninstall     Remove all installed manifests and the host.
#                                User data (config, logs) is preserved.
#   ./install.sh --uninstall --purge
#                                Also remove the user data directory.
#   ./install.sh --help          Print this help.
#
# Environment overrides (optional):
#   CHROME_WEB_STORE_ID   Extension ID for chrome/brave/vivaldi/opera manifests.
#                         Must match Chrome's [a-p]{32} alphabet (or be a
#                         placeholder beginning with "REPLACE_WITH_").
#   EDGE_ADDONS_ID        Extension ID for the edge manifest. Same charset
#                         rule applies.
#   PWA_ELH_PREFIX        Target install directory (default:
#                         ~/.local/share/pwa-elh on Linux,
#                         ~/Library/Application Support/pwa-elh on macOS).
#
# Exit codes:
#   0  success
#   1  usage / argument error
#   2  filesystem / write error
#   3  missing template or host source
#   4  invalid input (bad extension ID, unsafe install path)

set -euo pipefail

# ---------- constants ------------------------------------------------------

readonly HOST_NAME="com.aaharonov.pwa_elh"
readonly HOST_DESC="PWA External Link Handler - opens external links in the OS default browser."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly HOST_SRC="${SCRIPT_DIR}/host.py"

# Accepted Chrome Web Store extension ID pattern (lowercase a-p, 32 chars),
# OR a placeholder beginning with "REPLACE_WITH_".
readonly EXT_ID_PATTERN='^([a-p]{32}|REPLACE_WITH_[A-Z_]+)$'

# Safe-path pattern: ASCII letters, digits, space, dot, slash, underscore,
# hyphen. No quotes, backslashes, or control characters. This keeps the
# heredoc-based JSON rendering safe without needing a JSON-escaping pass.
readonly SAFE_PATH_PATTERN='^[A-Za-z0-9 ._/-]+$'

# ---------- helpers --------------------------------------------------------

die() { printf 'install: %s\n' "$*" >&2; exit "${2:-2}"; }
log() { printf 'install: %s\n' "$*"; }

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

detect_os() {
  case "$(uname -s)" in
    Linux*)   echo linux ;;
    Darwin*)  echo macos ;;
    *)        die "unsupported OS: $(uname -s)" 1 ;;
  esac
}

# Default install directory per OS.
default_prefix() {
  case "$1" in
    linux) echo "${HOME}/.local/share/pwa-elh" ;;
    macos) echo "${HOME}/Library/Application Support/pwa-elh" ;;
  esac
}

# Validate an extension ID against the published Chrome Web Store charset
# (or a recognised placeholder). Aborts on mismatch.
validate_ext_id() {
  local name="$1" value="$2"
  if ! [[ "$value" =~ $EXT_ID_PATTERN ]]; then
    die "invalid ${name} (expected 32 lowercase a-p chars or REPLACE_WITH_*): ${value}" 4
  fi
}

# Validate that a filesystem path uses only characters that are safe to
# substitute verbatim into the JSON manifest via a heredoc.
validate_path_chars() {
  local name="$1" value="$2"
  if ! [[ "$value" =~ $SAFE_PATH_PATTERN ]]; then
    die "${name} contains unsafe characters for JSON substitution: ${value}" 4
  fi
}

# Print one path per line: the per-browser NativeMessagingHosts directory.
# Format: "<browser-key>|<directory>". Conditional rows (e.g. Snap-Chromium)
# are emitted when the relevant directory hierarchy exists. The
# uninstall pass uses ``mode=uninstall`` so the Snap row is emitted whenever
# the manifest dir itself exists — even if the user has since removed the
# Snap-Chromium app — so an orphan manifest can still be cleaned up.
target_dirs() {
  local os="$1"
  local mode="${2:-install}"
  case "$os" in
    linux)
      cat <<EOF
chrome|${HOME}/.config/google-chrome/NativeMessagingHosts
chromium|${HOME}/.config/chromium/NativeMessagingHosts
edge|${HOME}/.config/microsoft-edge/NativeMessagingHosts
brave|${HOME}/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts
vivaldi|${HOME}/.config/vivaldi/NativeMessagingHosts
opera|${HOME}/.config/com.operasoftware.Opera/NativeMessagingHosts
EOF
      # Snap-Chromium (Q4): for installs, gate on the Snap app dir (don't
      # pollute non-Snap systems). For uninstalls, gate on the
      # NativeMessagingHosts dir itself so we still clean orphan manifests
      # left behind after the user removed Snap-Chromium.
      local snap_app_dir="${HOME}/snap/chromium"
      local snap_nmh_dir="${HOME}/snap/chromium/common/chromium/NativeMessagingHosts"
      if [ "$mode" = "uninstall" ]; then
        if [ -d "$snap_nmh_dir" ]; then
          echo "chromium-snap|${snap_nmh_dir}"
        fi
      elif [ -d "$snap_app_dir" ]; then
        echo "chromium-snap|${snap_nmh_dir}"
      fi
      ;;
    macos)
      cat <<EOF
chrome|${HOME}/Library/Application Support/Google/Chrome/NativeMessagingHosts
chromium|${HOME}/Library/Application Support/Chromium/NativeMessagingHosts
edge|${HOME}/Library/Application Support/Microsoft Edge/NativeMessagingHosts
brave|${HOME}/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts
vivaldi|${HOME}/Library/Application Support/Vivaldi/NativeMessagingHosts
opera|${HOME}/Library/Application Support/com.operasoftware.Opera/NativeMessagingHosts
EOF
      ;;
  esac
}

# Write a per-browser manifest file. Inputs MUST be pre-validated via
# `validate_path_chars` and `validate_ext_id` — this function relies on the
# absence of JSON-special characters in its arguments.
write_manifest() {
  local manifest_path="$1"
  local host_path="$2"
  local ext_id="$3"

  cat > "${manifest_path}.tmp" <<EOF
{
  "name": "${HOST_NAME}",
  "description": "${HOST_DESC}",
  "path": "${host_path}",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://${ext_id}/"]
}
EOF
  mv -f "${manifest_path}.tmp" "$manifest_path"
  chmod 0644 "$manifest_path"
}

install_for_browser() {
  local browser="$1" dir="$2" host_path="$3" chrome_id="$4" edge_id="$5"
  local manifest_path="${dir}/${HOST_NAME}.json"
  local ext_id

  case "$browser" in
    edge) ext_id="$edge_id" ;;
    *)    ext_id="$chrome_id" ;;
  esac

  if ! mkdir -p "$dir" 2>/dev/null; then
    log "  skip ${browser}: cannot create ${dir}"
    return
  fi
  write_manifest "$manifest_path" "$host_path" "$ext_id"
  log "  installed ${browser}: ${manifest_path}"
}

uninstall_for_browser() {
  local browser="$1" dir="$2"
  local manifest_path="${dir}/${HOST_NAME}.json"
  if [ -f "$manifest_path" ]; then
    rm -f "$manifest_path"
    log "  removed ${browser}: ${manifest_path}"
  fi
}

# ---------- main -----------------------------------------------------------

main() {
  local mode="install" purge=0
  for arg in "$@"; do
    case "$arg" in
      --uninstall) mode="uninstall" ;;
      --purge)     purge=1 ;;
      --help|-h)   usage; exit 0 ;;
      *)           die "unknown argument: $arg" 1 ;;
    esac
  done

  local os
  os=$(detect_os)
  log "OS: ${os}"

  local prefix="${PWA_ELH_PREFIX:-$(default_prefix "$os")}"
  # Expand a leading ~/ that arrived literally (e.g. user quoted the env
  # var so bash didn't pre-expand it). Without this, the strict charset
  # check below would reject the path with a confusing "unsafe characters"
  # error.
  prefix="${prefix/#\~\//${HOME}/}"
  prefix="${prefix/#\~/${HOME}}"
  local host_dst="${prefix}/host.py"

  local chrome_id="${CHROME_WEB_STORE_ID:-REPLACE_WITH_CHROME_WEB_STORE_ID}"
  local edge_id="${EDGE_ADDONS_ID:-REPLACE_WITH_EDGE_ADDONS_ID}"

  if [ "$mode" = "install" ]; then
    [ -f "$HOST_SRC" ] || die "missing host source: ${HOST_SRC}" 3

    validate_ext_id  CHROME_WEB_STORE_ID "$chrome_id"
    validate_ext_id  EDGE_ADDONS_ID      "$edge_id"
    validate_path_chars PWA_ELH_PREFIX   "$prefix"
    validate_path_chars HOST_DST         "$host_dst"

    mkdir -p "$prefix" || die "cannot create ${prefix}" 2
    cp -f "$HOST_SRC" "$host_dst"
    chmod 0755 "$host_dst"
    log "host: ${host_dst}"

    log "browsers:"
    while IFS='|' read -r browser dir; do
      [ -n "$browser" ] || continue
      install_for_browser "$browser" "$dir" "$host_dst" "$chrome_id" "$edge_id"
    done < <(target_dirs "$os" "$mode")
  else
    log "browsers:"
    while IFS='|' read -r browser dir; do
      [ -n "$browser" ] || continue
      uninstall_for_browser "$browser" "$dir"
    done < <(target_dirs "$os" "$mode")
    if [ -d "$prefix" ]; then
      rm -f "${prefix}/host.py"
      if [ "$purge" = "1" ]; then
        rm -rf "$prefix"
        log "purged user data: ${prefix}"
      else
        # Preserve user data (config / logs / sentinel) unless --purge.
        rmdir "$prefix" 2>/dev/null || true
      fi
    fi
  fi

  log "done."
}

main "$@"
