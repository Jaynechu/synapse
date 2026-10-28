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

mkdir -p "${USER_HOME}/Library/LaunchAgents"

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
        printf '# Only the values that differ from synapse_core/config.default.toml.\n# python -m synapse_core.config --defaults\n' > "${CONF_DIR}/config.toml"
        echo "[${CH}] config created at ${CONF_DIR}/config.toml — fill in your values"
    else
        echo "[${CH}] config already exists, skipping"
    fi

    LOG_DIR="$("${BRIDGE_BIN}" -c 'import sys; from synapse_core.config import USER_CONFIG_PATHS, resolve; print(resolve(sys.argv[1], path=USER_CONFIG_PATHS[sys.argv[1]])[0]["log_file"].parent)' "${CH}")"
    mkdir -p "${LOG_DIR}"

    sed \
        -e "s|__BRIDGE_BIN__|${BRIDGE_BIN}|g" \
        -e "s|__BRIDGE_MODULE__|${MODULE}|g" \
        -e "s|__BRIDGE_HOME__|${BRIDGE_HOME}|g" \
        -e "s|__USER_HOME__|${USER_HOME}|g" \
        -e "s|__LOG_DIR__|${LOG_DIR}|g" \
        "${TEMPLATE}" > "${TARGET}"

    launchctl unload "${TARGET}" 2>/dev/null || true
    launchctl load -w "${TARGET}"

    if [[ "${CH}" == "wx" ]]; then
        "${BRIDGE_BIN}" "${BRIDGE_HOME}/scripts/register_hooks.py" "${CH}" "${BRIDGE_HOME}" "${USER_HOME}/.claude/settings.json"
    fi

    echo "[${CH}] loaded com.synapse-${CH}.bridge — logs at ${LOG_DIR}/"
done
