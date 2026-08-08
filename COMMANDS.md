# Synapse — Commands

> Shared registry from synapse_core. Channel-unique marked [tg]/[wx].

## Slash

- /clear (alias /new) — fresh session, keeps current model
- /stop — interrupt, keep sid
- /model <id|alias> — swap model, keep sid; becomes the new default (survives restart)
- /info (alias /status, /usage) — model | effort | health / sid | uptime | ctx
- /help — render this file
- /thinking on|off — emit thinking block per turn
- /quote on|off — prepend quote block for cc quote tags
- /effort [low|medium|high|xhigh|max|auto] — set thinking-budget on next swap
- /voice cn|en — swap ack-string style. Persisted
- /cwd [N|<path>] — show/switch cwd + presets. Switch implies /clear. Persisted
- /resume [N|<sid>] — session picker, replay, cwd resolve, model restore
- /rewind <N> — truncate last N turns, respawn with --resume
- /regen — truncate + replay last user prompt
- /compact — [wx] cc protocol pipe. [tg] pending
- /diary [date] — fetch diary by date, inject as context
- /switch — [cc cli only] cross-channel session picker
- /tts off|on|auto — [tg] voice reply toggle
- /tl [hint] — record a timeline line now (marrow `tl` action=add)
- /tl- — silence this session: mute tl (action=add) nudge + stop self writes (dies with session)

## Bare commands (no /)

- mm- / mm+ — block/unblock session in marrow audit_log

## Aliases (no /)

- 5 / fable → Fable 5
- 4.6 / 4.7 / 4.8 / opus → Opus [1m]
- sonnet → Sonnet 4.6
- haiku → Haiku 4.5

## Cortex duty + circuit breaker (not bridge commands)

- cc cli slash surface only — the bridge does not mount them; `/ct-duty` sent here answers unknown.cmd.
- Stops cortex autonomous activity (fed rounds / auto wake). Bridge and normal chat unaffected.
- Persistent across restarts. State: `~/.config/marrow/breaker.json` ∪ `duty.json` (MAP.md §9.1).
- Duty: `/ct-duty cli|tg|off|all` (`cortex.ctl duty <mode>`) — that shell runs, the other is held; clears the breaker first.
- Breaker plumbing: `cortex.ctl pause|resume [--shell cli|tg]` (pause scopes merge), `cortex.ctl wake [--shell]` = clear + kick. Show: `cortex.ctl status`.
- Auto-trips after `[cortex.breaker].fuse_threshold` fuses within `window_hours` (marrow config.toml).

## Group chat [tg]

Set `[tg].group_ids` to a list of group chat ids (negative integers) to allow members of those groups to reach the bot. The bot then applies a mention gate: it responds only when the message text or caption contains one of `group_mention_keywords` (case-insensitive), the message @-mentions the bot, or the message is a direct reply to the bot's own message. Private messages from whitelisted users are unaffected. Group messages never rebind the bot's private reply target, so unsolicited output (heartbeats, idle notes) always goes to the configured private chat.

## Hold words

- 等 / 稍等 / 等等 / 先 — hold 10s instead of 5s before flush
- trailing ... / …… / 。。。/ ～ — same 10s hold
