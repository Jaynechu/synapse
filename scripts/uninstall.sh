#!/usr/bin/env bash
set -euo pipefail

CHANNEL="${1:-}"
USER_HOME="${HOME}"

usage() {
    echo "usage: ./scripts/uninstall.sh [tg|wx|all]" >&2
    exit 1
}

[[ -z "${CHANNEL}" ]] && usage
[[ "${CHANNEL}" != "tg" && "${CHANNEL}" != "wx" && "${CHANNEL}" != "all" ]] && usage

if [[ "${CHANNEL}" == "all" ]]; then
    CHANNELS=("tg" "wx")
else
    CHANNELS=("${CHANNEL}")
fi

for CH in "${CHANNELS[@]}"; do
    TARGET="${USER_HOME}/Library/LaunchAgents/com.synapse-${CH}.bridge.plist"
    if [[ -f "${TARGET}" ]]; then
        launchctl unload "${TARGET}" 2>/dev/null || true
        rm -f "${TARGET}"
        echo "[${CH}] unloaded and removed com.synapse-${CH}.bridge"
    else
        echo "[${CH}] plist not found, skipping"
    fi
done
