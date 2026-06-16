#!/usr/bin/env bash
set -euo pipefail

CHANNEL="${1:-}"
BRIDGE_HOME="$(pwd)"
BRIDGE_BIN="${BRIDGE_HOME}/.venv/bin/python"
USER_HOME="${HOME}"

usage() {
    echo "usage: ./scripts/install.sh [tg|wx|all]" >&2
    exit 1
}

[[ -z "${CHANNEL}" ]] && usage
[[ "${CHANNEL}" != "tg" && "${CHANNEL}" != "wx" && "${CHANNEL}" != "all" ]] && usage

if [[ ! -d "${BRIDGE_HOME}/synapse_core" ]]; then
    echo "error: run from repo root (synapse_core/ not found)" >&2
    exit 1
fi

if [[ ! -d "${BRIDGE_HOME}/.venv" ]]; then
    echo "creating venv..."
    python3 -m venv "${BRIDGE_HOME}/.venv"
fi

if [[ "${CHANNEL}" == "all" ]]; then
    CHANNELS=("tg" "wx")
    EXTRAS="all"
else
    CHANNELS=("${CHANNEL}")
    EXTRAS="${CHANNEL}"
fi

echo "installing deps: .[${EXTRAS}]"
"${BRIDGE_HOME}/.venv/bin/pip" install -q -e ".[${EXTRAS}]"

mkdir -p "${USER_HOME}/Library/Logs" "${USER_HOME}/Library/LaunchAgents"

for CH in "${CHANNELS[@]}"; do
    MODULE="synapse_${CH}"
    CONF_DIR="${USER_HOME}/.config/synapse-${CH}"
    TEMPLATE="${BRIDGE_HOME}/deploy/com.synapse-${CH}.bridge.plist.template"
    TARGET="${USER_HOME}/Library/LaunchAgents/com.synapse-${CH}.bridge.plist"

    if [[ ! -f "${TEMPLATE}" ]]; then
        echo "error: plist template missing at ${TEMPLATE}" >&2
        exit 1
    fi

    mkdir -p "${CONF_DIR}"

    if [[ ! -f "${CONF_DIR}/config.toml" ]]; then
        cp "${BRIDGE_HOME}/config.toml.example" "${CONF_DIR}/config.toml"
        echo "[${CH}] config created at ${CONF_DIR}/config.toml — fill in your values"
    else
        echo "[${CH}] config already exists, skipping"
    fi

    sed \
        -e "s|__BRIDGE_BIN__|${BRIDGE_BIN}|g" \
        -e "s|__BRIDGE_MODULE__|${MODULE}|g" \
        -e "s|__BRIDGE_HOME__|${BRIDGE_HOME}|g" \
        -e "s|__USER_HOME__|${USER_HOME}|g" \
        "${TEMPLATE}" > "${TARGET}"

    launchctl unload "${TARGET}" 2>/dev/null || true
    launchctl load -w "${TARGET}"

    echo "[${CH}] loaded com.synapse-${CH}.bridge — logs at ${USER_HOME}/Library/Logs/synapse-${CH}.{out,err}.log"
done
