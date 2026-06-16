#!/usr/bin/env bash
# finish_phase_a.sh — 30-second one-shot to close the last Phase A exit clause.
#
# Run from the synapse-wx repo root after Lumi wakes:
#   cd /Users/Gabrielle/CC-Lab/synapse-wx
#   ./scripts/finish_phase_a.sh
#
# Steps it does FOR you:
#   1. Stop the old weclaude launchd bridge (so iLink stops dual-polling).
#   2. Open ILinkClient.login() — prints QR; you scan once on your phone.
#   3. After login completes, sync the new token to ~/.config/synapse-wx/.
#   4. Run scripts/install_launchd.sh to load synapse-wx LaunchAgent.
#   5. Tail the out.log so you can watch the first contact land.
#
# After step 2 finishes you can put the phone down — steps 3-5 are unattended.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

echo "[1/5] stopping weclaude launchd bridge (if still running)"
if launchctl list | grep -q "com.weclaude.bridge"; then
    launchctl unload ~/Library/LaunchAgents/com.weclaude.bridge.plist 2>/dev/null \
        || launchctl bootout "gui/$(id -u)/com.weclaude.bridge" 2>/dev/null \
        || true
    sleep 1
    echo "    weclaude stopped"
else
    echo "    not running, skipping"
fi

echo "[2/5] iLink QR login — scan with WeChat on your phone (5 min timeout)"
"${REPO_ROOT}/.venv/bin/python" - <<'PY'
from synapse_wx.ilink import ILinkClient
c = ILinkClient()
if c.is_logged_in:
    print("    already logged in — skipping scan")
else:
    c.login()
PY

echo "[3/5] verifying token landed at ~/.config/synapse-wx/token.json"
if [[ -f ~/.config/synapse-wx/token.json ]]; then
    chmod 600 ~/.config/synapse-wx/token.json
    echo "    token OK"
else
    echo "    ERROR: token.json missing after login" >&2
    exit 1
fi

echo "[4/5] loading synapse-wx LaunchAgent"
"${REPO_ROOT}/scripts/install.sh" wx

echo "[5/5] bridge is live. Now open WeChat, send a message to your bot."
echo
echo "    Watch the live log:  tail -f ~/Library/Logs/synapse-wx.out.log"
echo "    Stop the bridge:     ./scripts/uninstall.sh wx"
echo
echo "Phase A exit-condition closure: send '/info' to the bot from WeChat;"
echo "you should see one line back with model/sid/token. That confirms the"
echo "final clause (WeChat conversation with Stellan working) and closes Phase A."
