#!/usr/bin/env bash
# PWA External Link Handler - Linux/macOS native-host uninstaller.
#
# Thin wrapper around `install.sh --uninstall`. All arguments are passed
# through, so `./uninstall.sh --purge` removes the user data directory in
# addition to the manifests and host launcher.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/install.sh" --uninstall "$@"
